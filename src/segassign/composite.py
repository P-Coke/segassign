"""The composite backend sequences segment -> zonal (single item and cross-scene pipelined batch)."""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import rasterio

from . import segment, zonal
from .models import (
    Artifact,
    CompositeBatchRequest,
    CompositeBatchResult,
    CompositeRequest,
    CompositeResult,
    ExecutionPlan,
    SegmentRequest,
    SegmentResult,
    TaskStatus,
    ZonalRequest,
)
from .raster_batch import RasterMatcher, discover_rasters, output_vector_for, validate_output_directory
from .runtime import CancelledError, EngineLease, ResourceScheduler, RunContext, RunWorkspace
from .runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest
from .segment.engine import wait_for_external_engines
from .segment.service import (
    _REAL_SEGMENT_RUN,
    SharedEnginePool,
    _finalize_segment_stage,
    _run_engine_stage,
)
from .zonal.service import detect_raster_values

_MATCH_LABELS = {
    "shared": "全局共用",
    "relative_path": "相对路径",
    "stem": "同名匹配",
    "spatial_overlap": "空间重叠",
}


def _relative_label(path: Path, root: Path) -> str:
    if root.is_dir():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.name


def _remove_internal_segment(path: Path) -> None:
    if path.parent.name != ".geotoolbox-segment":
        raise RuntimeError(f"refusing to remove non-internal segment output: {path}")
    for sidecar in path.parent.glob(path.stem + ".*"):
        sidecar.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _requests(request: CompositeRequest) -> tuple[SegmentRequest, ZonalRequest]:
    intermediate = request.output_vector.parent / ".geotoolbox-segment" / (request.output_vector.stem + "-segment.shp")
    segment_request = SegmentRequest(
        request.process_raster,
        request.base_raster,
        intermediate,
        request.ecognition_bin,
        request.ruleset,
        request.invalid_ruleset,
        request.bridge,
        request.segment_options,
    )
    zonal_request = ZonalRequest(
        intermediate,
        request.classification_raster,
        request.output_vector,
        request.zonal_options,
    )
    return segment_request, zonal_request


def _composite_fingerprint(request: CompositeRequest) -> str:
    return stable_digest(
        {
            "request": request,
            "process": file_fingerprint(request.process_raster),
            "base": file_fingerprint(request.base_raster),
            "classification": file_fingerprint(request.classification_raster),
        }
    )


def plan(request: CompositeRequest) -> ExecutionPlan:
    segment_request, _ = _requests(request)
    segment_plan = segment.plan(segment_request)
    # The intermediate does not exist yet, so only classification preflight is
    # possible here; zonal.plan is called immediately after segment succeeds.
    if not request.classification_raster.is_file():
        raise ValueError(f"classification raster does not exist: {request.classification_raster}")
    with rasterio.open(request.classification_raster) as dataset:
        if dataset.count < 1:
            raise ValueError("classification raster has no bands")
        classification = {"width": dataset.width, "height": dataset.height, "dtype": dataset.dtypes[0]}
    return ExecutionPlan(
        "composite",
        (request.process_raster, request.base_raster, request.classification_raster, request.ruleset),
        (request.output_vector,),
        ("segment", "zonal", "report"),
        {
            "items": 1,
            "segment": dict(segment_plan.metrics),
            "zonal": {"classification": classification, "valid_values": request.zonal_options.valid_values},
            "items_preview": (
                {
                    "item_id": str(request.process_raster),
                    "name": request.process_raster.name,
                    "relative_path": request.process_raster.name,
                    "output": str(request.output_vector),
                    "output_name": request.output_vector.name,
                    "size_summary": (
                        f"{segment_plan.metrics['width']}×{segment_plan.metrics['height']} · "
                        f"{segment_plan.metrics['bands']}波段 · {segment_plan.metrics['tiles']}块"
                        if segment_plan.metrics.get("width") and segment_plan.metrics.get("height")
                        else f"{int(segment_plan.metrics.get('tiles', 0))} 块瓦片"
                    ),
                    "auxiliary": f"基础：{request.base_raster.name} | 分类：{request.classification_raster.name}",
                    "match_mode": "单景直连",
                    "tiles_total": int(segment_plan.metrics["tiles"]),
                },
            ),
            "warnings": [],
            "input_copy_bytes": 0,
        },
    )


def _complete_composite_from_segment(
    request: CompositeRequest,
    zonal_template: ZonalRequest,
    context: RunContext,
    workspace: RunWorkspace,
    fingerprint: str,
    started: float,
    segment_result: SegmentResult,
    *,
    predetected_values: tuple[int, ...] | None = None,
    zonal_context: RunContext | None = None,
) -> CompositeResult:
    zonal_result = None
    try:
        if segment_result.status is not TaskStatus.SUCCEEDED:
            status = segment_result.status
            warnings = ("zonal was not started because segment did not succeed", *segment_result.warnings)
        else:
            context.emit(
                "stage",
                "composite-zonal",
                f"步骤 2/2：分类赋值（{zonal_template.raster.name}）",
            )
            zonal_request = ZonalRequest(
                segment_result.primary_vector,
                zonal_template.raster,
                zonal_template.output_vector,
                zonal_template.options,
            )
            z_ctx = zonal_context or context
            if predetected_values is not None:
                zonal_result = zonal.run(zonal_request, z_ctx, predetected_values=predetected_values)
            else:
                zonal_result = zonal.run(zonal_request, z_ctx)
            status = zonal_result.status
            warnings = zonal_result.warnings
            if status is TaskStatus.SUCCEEDED:
                _remove_internal_segment(segment_result.primary_vector)
        timings = {
            "segment": float(segment_result.timings.get("total", 0)) if segment_result else 0,
            "zonal": float(zonal_result.timings.get("total", 0)) if zonal_result else 0,
            "total": round(time.perf_counter() - started, 6),
        }
        peak = max(
            segment_result.peak_memory_mb if segment_result else 0,
            zonal_result.peak_memory_mb if zonal_result else 0,
        )
        report = workspace.report(
            "run",
            {
                "tool": "composite",
                "status": status.value,
                "request_fingerprint": fingerprint,
                "request": asdict(request),
                "segment_report": str(segment_result.report_path) if segment_result else None,
                "zonal_report": str(zonal_result.report_path) if zonal_result else None,
                "output": str(request.output_vector) if status is TaskStatus.SUCCEEDED else None,
                "timings": timings,
                "peak_memory_mb": peak,
                "warnings": warnings,
            },
        )
        artifacts = [Artifact("run_report", report)]
        if status is TaskStatus.SUCCEEDED:
            artifacts.insert(0, Artifact("primary_vector", request.output_vector))
            workspace.mark(
                "complete",
                fingerprint,
                output=str(request.output_vector),
                report=str(report),
                output_fingerprint=dataset_fingerprint(request.output_vector),
            )
            cleanup_warnings = workspace.cleanup_heavy()
            if context.cleanup_on_success:
                report, purge_warnings = workspace.purge_after_success(report)
                cleanup_warnings = (*cleanup_warnings, *purge_warnings)
                artifacts = [Artifact("primary_vector", request.output_vector), Artifact("run_report", report)]
            if cleanup_warnings:
                warnings = (*warnings, *cleanup_warnings)
        context.emit(status.value, "composite", f"Composite run {status.value}")
        return CompositeResult(
            status,
            tuple(artifacts),
            tuple(warnings),
            timings,
            peak,
            report,
            request.output_vector,
            segment_result,
            zonal_result,
        )
    except Exception as error:
        timings = {"total": round(time.perf_counter() - started, 6)}
        detail = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        report = workspace.report(
            "run",
            {
                "tool": "composite",
                "status": TaskStatus.FAILED.value,
                "request": asdict(request),
                "error": detail,
                "timings": timings,
            },
        )
        context.emit("failed", "composite", f"{type(error).__name__}: {error}")
        return CompositeResult(
            TaskStatus.FAILED,
            (Artifact("run_report", report),),
            (detail,),
            timings,
            0,
            report,
            request.output_vector,
            segment_result,
            zonal_result,
        )


def run(
    request: CompositeRequest,
    context: RunContext | None = None,
    *,
    predetected_values: tuple[int, ...] | None = None,
) -> CompositeResult:
    started = time.perf_counter()
    execution_plan = plan(request)
    context = context or RunContext(request.output_vector.parent / ".geotoolbox-work")
    fingerprint = _composite_fingerprint(request)
    workspace = RunWorkspace(context.work_dir, "composite", fingerprint, context.resume)
    workspace.ensure()
    complete = workspace.read_checkpoint("complete")
    if (
        context.resume
        and complete
        and complete.get("fingerprint") == fingerprint
        and dataset_matches(request.output_vector, complete.get("output_fingerprint"))
        and Path(complete.get("report", "")).is_file()
    ):
        report = Path(complete["report"])
        return CompositeResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", request.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.0},
            0.0,
            report,
            request.output_vector,
            None,
            None,
        )
    context.emit("started", "composite", "Composite run started", plan=execution_plan.metrics)
    context.emit(
        "stage",
        "composite-segment",
        f"步骤 1/2：影像分割（{request.process_raster.name}）",
    )
    segment_request, zonal_template = _requests(request)
    try:
        segment_result = segment.run(segment_request, context)
    except Exception as error:
        timings = {"total": round(time.perf_counter() - started, 6)}
        detail = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
        report = workspace.report(
            "run",
            {
                "tool": "composite",
                "status": TaskStatus.FAILED.value,
                "request": asdict(request),
                "error": detail,
                "timings": timings,
            },
        )
        context.emit("failed", "composite", f"{type(error).__name__}: {error}")
        return CompositeResult(
            TaskStatus.FAILED,
            (Artifact("run_report", report),),
            (detail,),
            timings,
            0,
            report,
            request.output_vector,
            None,
            None,
        )
    return _complete_composite_from_segment(
        request,
        zonal_template,
        context,
        workspace,
        fingerprint,
        started,
        segment_result,
        predetected_values=predetected_values,
    )


_REAL_COMPOSITE_RUN = run


def _resolve_batch_items(
    request: CompositeBatchRequest,
) -> tuple[tuple[CompositeRequest, ...], list[dict], list[str]]:
    validate_output_directory(
        request.output_directory,
        (request.process_source, request.base_source, request.classification_source),
    )
    process_rasters = discover_rasters(request.process_source, recursive=request.recursive, label="亚米级影像")
    if request.selected_inputs:
        allowed = {str(path).casefold() for path in request.selected_inputs}
        process_rasters = tuple(path for path in process_rasters if str(path).casefold() in allowed)
        if not process_rasters:
            raise ValueError("未勾选任何有效的亚米级影像，无法执行批量分割并赋值")
    base_matcher = RasterMatcher(
        request.base_source,
        recursive=request.recursive,
        label="2 米基础影像",
        default_crs=request.segment_options.default_crs,
    )
    class_matcher = RasterMatcher(
        request.classification_source,
        recursive=request.recursive,
        label="分类影像",
        default_crs=request.segment_options.default_crs,
    )
    items: list[CompositeRequest] = []
    pairs: list[dict] = []
    warnings: list[str] = []
    for process_path in process_rasters:
        base_match = base_matcher.match(process_path, request.process_source)
        class_match = class_matcher.match(process_path, request.process_source)
        output_vector = output_vector_for(process_path, request.process_source, request.output_directory)
        items.append(
            CompositeRequest(
                process_raster=process_path,
                base_raster=base_match.path,
                classification_raster=class_match.path,
                output_vector=output_vector,
                ecognition_bin=request.ecognition_bin,
                ruleset=request.ruleset,
                invalid_ruleset=request.invalid_ruleset,
                bridge=request.bridge,
                segment_options=request.segment_options,
                zonal_options=request.zonal_options,
            )
        )
        pairs.append(
            {
                "process": str(process_path),
                "base": str(base_match.path),
                "base_match": base_match.method,
                "classification": str(class_match.path),
                "classification_match": class_match.method,
                "output": str(output_vector),
            }
        )
        if base_match.warning:
            warnings.append(base_match.warning)
        if class_match.warning:
            warnings.append(class_match.warning)
    return tuple(items), pairs, warnings


def _segment_size_summary(seg_metrics: dict) -> str:
    tiles = int(seg_metrics.get("tiles", 0))
    width, height, bands = seg_metrics.get("width"), seg_metrics.get("height"), seg_metrics.get("bands")
    if width and height:
        band_text = f" · {bands}波段" if bands else ""
        return f"{width}×{height}{band_text} · {tiles}块"
    return f"{tiles} 块瓦片"


def plan_batch(request: CompositeBatchRequest) -> ExecutionPlan:
    item_requests, pairs, warnings = _resolve_batch_items(request)
    item_plans = tuple(plan(item) for item in item_requests)
    items_preview = tuple(
        {
            "item_id": str(item.process_raster),
            "name": item.process_raster.name,
            "relative_path": _relative_label(item.process_raster, request.process_source),
            "output": str(item.output_vector),
            "output_name": _relative_label(item.output_vector, request.output_directory),
            "size_summary": _segment_size_summary(dict(item_plan.metrics.get("segment", {}))),
            "auxiliary": f"基础：{item.base_raster.name} | 分类：{item.classification_raster.name}",
            "match_mode": (
                f"基础:{_MATCH_LABELS.get(pair['base_match'], pair['base_match'])} / "
                f"分类:{_MATCH_LABELS.get(pair['classification_match'], pair['classification_match'])}"
            ),
            "tiles_total": int(item_plan.metrics.get("segment", {}).get("tiles", 0)),
        }
        for item, item_plan, pair in zip(item_requests, item_plans, pairs, strict=True)
    )
    inputs = tuple(
        dict.fromkeys(
            [
                *(item.process_raster for item in item_requests),
                *(item.base_raster for item in item_requests),
                *(item.classification_raster for item in item_requests),
                request.ruleset,
            ]
        )
    )
    return ExecutionPlan(
        tool="composite-batch",
        inputs=inputs,
        outputs=tuple(item.output_vector for item in item_requests),
        stages=("discover", "preflight", "segment", "zonal", "report"),
        metrics={
            "items": len(item_requests),
            "tiles_total": sum(int(item.metrics["segment"]["tiles"]) for item in item_plans),
            "recursive": request.recursive,
            "valid_values": request.zonal_options.valid_values,
            "pairs": pairs,
            "items_preview": items_preview,
            "warnings": warnings,
            "input_copy_bytes": 0,
        },
    )


def _batch_result(
    status: TaskStatus,
    request: CompositeBatchRequest,
    workspace: RunWorkspace,
    started: float,
    results,
    warnings,
    items_total: int,
    *,
    pipelined: bool = False,
) -> CompositeBatchResult:
    succeeded = tuple(result for result in results if result.status is TaskStatus.SUCCEEDED)
    outputs = tuple(result.primary_vector for result in succeeded)
    timings = {
        "items": round(sum(float(result.timings.get("total", 0.0)) for result in results), 6),
        "total": round(time.perf_counter() - started, 6),
    }
    peak = max((result.peak_memory_mb for result in results), default=0.0)
    report = workspace.report(
        "run",
        {
            "tool": "composite-batch",
            "status": status.value,
            "pipelined": pipelined,
            "process_source": str(request.process_source),
            "base_source": str(request.base_source),
            "classification_source": str(request.classification_source),
            "output_directory": str(request.output_directory),
            "items_total": items_total,
            "items_succeeded": len(succeeded),
            "outputs": [str(path) for path in outputs],
            "item_reports": [str(result.report_path) for result in results],
            "warnings": list(warnings),
            "timings": timings,
            "peak_memory_mb": peak,
        },
    )
    artifacts = tuple([*(Artifact("primary_vector", path) for path in outputs), Artifact("run_report", report)])
    return CompositeBatchResult(
        status,
        artifacts,
        tuple(warnings),
        timings,
        peak,
        report,
        outputs,
        tuple(results),
        items_total,
        len(succeeded),
    )


def run_batch(request: CompositeBatchRequest, context: RunContext | None = None) -> CompositeBatchResult:
    """Run discovered segment -> zonal items with cross-scene prefetch and overlapped finalize+zonal."""
    started = time.perf_counter()
    execution_plan = plan_batch(request)
    item_requests, _, pairing_warnings = _resolve_batch_items(request)
    context = context or RunContext(request.output_directory / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "items": [
                {
                    "process": file_fingerprint(item.process_raster),
                    "base": file_fingerprint(item.base_raster),
                    "classification": file_fingerprint(item.classification_raster),
                    "output": str(item.output_vector),
                }
                for item in item_requests
            ],
            "ruleset": file_fingerprint(request.ruleset),
        }
    )
    workspace = RunWorkspace(context.work_dir, "composite-batch", fingerprint, context.resume)
    workspace.ensure()
    complete = workspace.read_checkpoint("complete")
    if (
        context.resume
        and complete
        and complete.get("fingerprint") == fingerprint
        and Path(complete.get("report", "")).is_file()
    ):
        expected = complete.get("outputs", {})
        if expected and all(dataset_matches(Path(path), value) for path, value in expected.items()):
            report = Path(complete["report"])
            outputs = tuple(Path(path) for path in expected)
            return CompositeBatchResult(
                TaskStatus.SUCCEEDED,
                tuple([*(Artifact("primary_vector", path) for path in outputs), Artifact("run_report", report)]),
                (),
                {"total": 0.0},
                0.0,
                report,
                outputs,
                (),
                len(outputs),
                len(outputs),
            )

    context.emit("started", "composite-batch", "批量分割并赋值开始", plan=execution_plan.metrics)
    warnings: list[str] = list(pairing_warnings)
    cancelled = False
    detected_cache: dict[Path, tuple[int, ...]] = {}
    if not request.zonal_options.valid_values and request.classification_source.is_file():
        try:
            context.emit(
                "stage",
                "scan",
                f"正在识别分类影像有效像元值：{request.classification_source.name}",
            )
            detected_cache[request.classification_source] = detect_raster_values(
                request.classification_source,
                request.zonal_options.nodata,
                context,
            )
        except CancelledError as error:
            warnings.append(str(error))
            cancelled = True

    use_pipeline = (
        not cancelled
        and run is _REAL_COMPOSITE_RUN
        and segment.run is _REAL_SEGMENT_RUN
        and len(item_requests) > 1
    )

    if cancelled:
        results: list[CompositeResult] = []
    elif not use_pipeline:
        results = []
        for index, item in enumerate(item_requests, 1):
            try:
                context.checkpoint()
            except CancelledError as error:
                warnings.append(str(error))
                cancelled = True
                break
            item_ctx = context.scoped(
                item_id=str(item.process_raster),
                item_index=index,
                item_name=item.process_raster.name,
            )
            item_ctx.emit(
                "progress",
                "batch",
                f"正在处理 {index}/{len(item_requests)}：{item.process_raster.name}",
                completed=index - 1,
                total=len(item_requests),
                input=str(item.process_raster),
            )
            cached_values = detected_cache.get(item.classification_raster)
            if cached_values is not None:
                try:
                    result = run(item, item_ctx, predetected_values=cached_values)
                except TypeError:
                    result = run(item, item_ctx)
            else:
                result = run(item, item_ctx)
            results.append(result)
            if result.status is TaskStatus.CANCELLED:
                cancelled = True
                warnings.extend(result.warnings)
                break
            if result.status is not TaskStatus.SUCCEEDED:
                warnings.append(f"{item.process_raster}: {result.status.value}")
                warnings.extend(result.warnings)
            item_ctx.emit(
                "progress",
                "batch",
                f"已完成 {index}/{len(item_requests)}：{item.process_raster.name}",
                completed=index,
                total=len(item_requests),
                output=str(item.output_vector),
                item_status=result.status.value,
            )
    else:
        ordered_results: list[CompositeResult | None] = [None] * len(item_requests)
        completed_count = 0

        def record_result(idx: int, item: CompositeRequest, item_ctx: RunContext, result: CompositeResult) -> None:
            nonlocal completed_count, cancelled
            ordered_results[idx] = result
            completed_count += 1
            if result.status is TaskStatus.CANCELLED:
                cancelled = True
                warnings.extend(result.warnings)
            elif result.status is not TaskStatus.SUCCEEDED:
                warnings.append(f"{item.process_raster}: {result.status.value}")
                warnings.extend(result.warnings)
            item_ctx.emit(
                "progress",
                "batch",
                f"已完成 {completed_count}/{len(item_requests)}：{item.process_raster.name}",
                completed=completed_count,
                total=len(item_requests),
                output=str(item.output_vector),
                item_status=result.status.value,
            )

        bg_zonal_workers = ResourceScheduler(context.resources).background_zonal_workers()

        def _finalize_composite(
            item: CompositeRequest,
            zonal_template: ZonalRequest,
            item_ctx: RunContext,
            item_ws: RunWorkspace,
            item_fp: str,
            item_started: float,
            staged,
            *,
            concurrent_with_engine: bool,
        ) -> CompositeResult:
            if isinstance(staged, SegmentResult):
                seg_result = staged
            else:
                m_workers = min(6, staged.post_workers) if concurrent_with_engine else staged.post_workers
                seg_result = _finalize_segment_stage(staged, mosaic_workers=m_workers)
            zonal_ctx = (
                item_ctx.scoped(resources=replace(item_ctx.resources, raster_workers=bg_zonal_workers))
                if concurrent_with_engine
                else item_ctx
            )
            return _complete_composite_from_segment(
                item,
                zonal_template,
                item_ctx,
                item_ws,
                item_fp,
                item_started,
                seg_result,
                predetected_values=detected_cache.get(item.classification_raster),
                zonal_context=zonal_ctx,
            )

        segment_requests = [_requests(item) for item in item_requests]
        pending_scenes: deque[
            tuple[int, CompositeRequest, ZonalRequest, RunContext, RunWorkspace, str, float, Future, threading.Event]
        ] = deque()
        pending_finalize: tuple[int, CompositeRequest, RunContext, Future] | None = None
        engine_lease = EngineLease(context)
        try:
            engine_lease.__enter__()
            wait_for_external_engines(context)
            with (
                SharedEnginePool(context, request.segment_options.ecognition_workers) as shared_pool,
                ThreadPoolExecutor(max_workers=2, thread_name_prefix="comp-scene") as scene_pool,
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="comp-finalize") as finalize_pool,
            ):

                def drain_oldest_scene() -> None:
                    nonlocal pending_finalize, cancelled
                    (
                        old_idx,
                        old_item,
                        old_zonal_tpl,
                        old_ctx,
                        old_ws,
                        old_fp,
                        old_started,
                        old_future,
                        _,
                    ) = pending_scenes.popleft()
                    staged = old_future.result()
                    if pending_finalize is not None:
                        prev_idx, prev_item, prev_ctx, prev_future = pending_finalize
                        pending_finalize = None
                        record_result(prev_idx, prev_item, prev_ctx, prev_future.result())
                    if isinstance(staged, SegmentResult) and staged.status is TaskStatus.CANCELLED:
                        record_result(
                            old_idx,
                            old_item,
                            old_ctx,
                            _complete_composite_from_segment(
                                old_item, old_zonal_tpl, old_ctx, old_ws, old_fp, old_started, staged
                            ),
                        )
                        cancelled = True
                        return
                    pending_finalize = (
                        old_idx,
                        old_item,
                        old_ctx,
                        finalize_pool.submit(
                            _finalize_composite,
                            old_item,
                            old_zonal_tpl,
                            old_ctx,
                            old_ws,
                            old_fp,
                            old_started,
                            staged,
                            concurrent_with_engine=bool(pending_scenes),
                        ),
                    )

                for idx, item in enumerate(item_requests):
                    if len(pending_scenes) >= 2:
                        drain_oldest_scene()
                        if cancelled:
                            break
                    if pending_scenes:
                        prev_tail_event = pending_scenes[-1][8]
                        while not prev_tail_event.wait(timeout=0.2):
                            context.checkpoint()
                        if pending_scenes and pending_scenes[0][7].done():
                            drain_oldest_scene()
                            if cancelled:
                                break
                    try:
                        context.checkpoint()
                    except CancelledError as error:
                        warnings.append(str(error))
                        cancelled = True
                        break
                    item_started = time.perf_counter()
                    item_fp = _composite_fingerprint(item)
                    item_ws = RunWorkspace(context.work_dir, "composite", item_fp, context.resume)
                    item_ws.ensure()
                    item_ctx = context.scoped(
                        item_id=str(item.process_raster),
                        item_index=idx + 1,
                        item_name=item.process_raster.name,
                    )
                    item_complete = item_ws.read_checkpoint("complete")
                    if (
                        context.resume
                        and item_complete
                        and item_complete.get("fingerprint") == item_fp
                        and dataset_matches(item.output_vector, item_complete.get("output_fingerprint"))
                        and Path(item_complete.get("report", "")).is_file()
                    ):
                        report = Path(item_complete["report"])
                        record_result(
                            idx,
                            item,
                            item_ctx,
                            CompositeResult(
                                TaskStatus.SUCCEEDED,
                                (Artifact("primary_vector", item.output_vector), Artifact("run_report", report)),
                                (),
                                {"total": 0.0},
                                0.0,
                                report,
                                item.output_vector,
                                None,
                                None,
                            ),
                        )
                        continue
                    item_ctx.emit(
                        "progress",
                        "batch",
                        f"正在处理 {idx + 1}/{len(item_requests)}：{item.process_raster.name}",
                        completed=completed_count,
                        total=len(item_requests),
                        input=str(item.process_raster),
                    )
                    seg_req, zonal_tpl = segment_requests[idx]
                    tail_event = threading.Event()
                    scene_future = scene_pool.submit(
                        _run_engine_stage,
                        seg_req,
                        item_ctx,
                        hold_lease=False,
                        shared_pool=shared_pool,
                        on_tail_ready=tail_event.set,
                    )
                    pending_scenes.append(
                        (idx, item, zonal_tpl, item_ctx, item_ws, item_fp, item_started, scene_future, tail_event)
                    )

                while pending_scenes:
                    drain_oldest_scene()
                if pending_finalize is not None:
                    prev_idx, prev_item, prev_ctx, prev_future = pending_finalize
                    record_result(prev_idx, prev_item, prev_ctx, prev_future.result())
        except CancelledError as error:
            warnings.append(str(error))
            cancelled = True
        finally:
            engine_lease.__exit__(None, None, None)
        results = [res for res in ordered_results if res is not None]

    succeeded = sum(result.status is TaskStatus.SUCCEEDED for result in results)
    if cancelled:
        status = TaskStatus.CANCELLED
    elif succeeded == len(item_requests):
        status = TaskStatus.SUCCEEDED
    elif succeeded:
        status = TaskStatus.PARTIAL
    else:
        status = TaskStatus.FAILED
    result = _batch_result(
        status,
        request,
        workspace,
        started,
        results,
        warnings,
        len(item_requests),
        pipelined=use_pipeline,
    )
    if status is TaskStatus.SUCCEEDED:
        workspace.mark(
            "complete",
            fingerprint,
            report=str(result.report_path),
            outputs={str(path): dataset_fingerprint(path) for path in result.primary_vectors},
        )
        workspace.cleanup_heavy()
        if context.cleanup_on_success:
            archived_report, purge_warnings = workspace.purge_after_success(result.report_path)
            result = CompositeBatchResult(
                result.status,
                tuple(
                    [
                        *(Artifact("primary_vector", path) for path in result.primary_vectors),
                        Artifact("run_report", archived_report),
                    ]
                ),
                (*result.warnings, *purge_warnings),
                result.timings,
                result.peak_memory_mb,
                archived_report,
                result.primary_vectors,
                result.item_results,
                result.items_total,
                result.items_succeeded,
            )
    context.emit(status.value, "composite-batch", f"批量分割并赋值结束：{status.value}")
    return result


__all__ = ["plan", "plan_batch", "run", "run_batch"]
