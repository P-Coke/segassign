"""The composite backend sequences segment -> zonal (single item and cross-scene pipelined batch)."""

from __future__ import annotations

import time
import traceback
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
from .runtime import CancelledError, EngineLease, RunContext, RunWorkspace
from .runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest
from .segment.engine import wait_for_external_engines
from .segment.service import (
    _REAL_SEGMENT_RUN,
    PrefetchedRow,
    _finalize_segment_stage,
    _run_engine_stage,
    prefetch_first_row,
)
from .zonal.service import detect_raster_values


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
            "segment": dict(segment_plan.metrics),
            "zonal": {"classification": classification, "valid_values": request.zonal_options.valid_values},
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


def plan_batch(request: CompositeBatchRequest) -> ExecutionPlan:
    item_requests, pairs, warnings = _resolve_batch_items(request)
    item_plans = tuple(plan(item) for item in item_requests)
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
            context.emit(
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
                    result = run(item, context, predetected_values=cached_values)
                except TypeError:
                    result = run(item, context)
            else:
                result = run(item, context)
            results.append(result)
            if result.status is TaskStatus.CANCELLED:
                cancelled = True
                warnings.extend(result.warnings)
                break
            if result.status is not TaskStatus.SUCCEEDED:
                warnings.append(f"{item.process_raster}: {result.status.value}")
                warnings.extend(result.warnings)
            context.emit(
                "progress",
                "batch",
                f"已完成 {index}/{len(item_requests)}：{item.process_raster.name}",
                completed=index,
                total=len(item_requests),
                output=str(item.output_vector),
            )
    else:
        ordered_results: list[CompositeResult | None] = [None] * len(item_requests)
        completed_count = 0

        def record_result(idx: int, item: CompositeRequest, result: CompositeResult) -> None:
            nonlocal completed_count, cancelled
            ordered_results[idx] = result
            completed_count += 1
            if result.status is TaskStatus.CANCELLED:
                cancelled = True
                warnings.extend(result.warnings)
            elif result.status is not TaskStatus.SUCCEEDED:
                warnings.append(f"{item.process_raster}: {result.status.value}")
                warnings.extend(result.warnings)
            context.emit(
                "progress",
                "batch",
                f"已完成 {completed_count}/{len(item_requests)}：{item.process_raster.name}",
                completed=completed_count,
                total=len(item_requests),
                output=str(item.output_vector),
            )

        bg_zonal_workers = max(1, min(4, context.resources.logical_cpus // 4))
        bg_zonal_context = RunContext(
            work_dir=context.work_dir,
            cancellation=context.cancellation,
            event_sink=context.event_sink,
            resources=replace(context.resources, raster_workers=bg_zonal_workers),
            resume=context.resume,
            cleanup_on_success=context.cleanup_on_success,
            task_id=context.task_id,
        )

        def _finalize_composite(
            item: CompositeRequest,
            zonal_template: ZonalRequest,
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
                m_workers = min(4, staged.post_workers) if concurrent_with_engine else staged.post_workers
                seg_result = _finalize_segment_stage(staged, mosaic_workers=m_workers)
            return _complete_composite_from_segment(
                item,
                zonal_template,
                context,
                item_ws,
                item_fp,
                item_started,
                seg_result,
                predetected_values=detected_cache.get(item.classification_raster),
                zonal_context=bg_zonal_context if concurrent_with_engine else context,
            )

        segment_requests = [_requests(item) for item in item_requests]
        prefetched_by_index: dict[int, PrefetchedRow | None] = {}
        pending_finalize: tuple[int, CompositeRequest, Future] | None = None
        engine_lease = EngineLease(context)
        try:
            engine_lease.__enter__()
            wait_for_external_engines(context)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="comp-finalize") as finalize_pool:
                for idx, item in enumerate(item_requests):
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
                    context.emit(
                        "progress",
                        "batch",
                        f"正在处理 {idx + 1}/{len(item_requests)}：{item.process_raster.name}",
                        completed=completed_count,
                        total=len(item_requests),
                        input=str(item.process_raster),
                    )
                    seg_req, zonal_tpl = segment_requests[idx]
                    next_idx = idx + 1

                    def _make_prefetch(target_idx: int):
                        def _do_prefetch() -> None:
                            prefetched_by_index[target_idx] = prefetch_first_row(
                                segment_requests[target_idx][0], context
                            )

                        return _do_prefetch

                    on_prep_done = _make_prefetch(next_idx) if next_idx < len(item_requests) else None
                    staged = _run_engine_stage(
                        seg_req,
                        context,
                        hold_lease=False,
                        prefetched=prefetched_by_index.pop(idx, None),
                        on_prepare_complete=on_prep_done,
                    )
                    if pending_finalize is not None:
                        prev_idx, prev_item, prev_future = pending_finalize
                        pending_finalize = None
                        record_result(prev_idx, prev_item, prev_future.result())
                    if isinstance(staged, SegmentResult) and staged.status is TaskStatus.CANCELLED:
                        record_result(
                            idx,
                            item,
                            _complete_composite_from_segment(
                                item, zonal_tpl, context, item_ws, item_fp, item_started, staged
                            ),
                        )
                        break
                    pending_finalize = (
                        idx,
                        item,
                        finalize_pool.submit(
                            _finalize_composite,
                            item,
                            zonal_tpl,
                            item_ws,
                            item_fp,
                            item_started,
                            staged,
                            concurrent_with_engine=next_idx < len(item_requests),
                        ),
                    )
                if pending_finalize is not None:
                    prev_idx, prev_item, prev_future = pending_finalize
                    record_result(prev_idx, prev_item, prev_future.result())
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
