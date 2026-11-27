"""Native, resumable orchestration for assigning one raster to many Shapefiles with tail overlap."""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import rasterio

from ..models import (
    Artifact,
    ExecutionPlan,
    TaskStatus,
    ZonalBatchRequest,
    ZonalBatchResult,
    ZonalRequest,
)
from ..runtime import CancelledError, ResourceScheduler, RunContext, RunWorkspace
from ..runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest
from .service import detect_raster_values, plan, run

_REAL_ZONAL_RUN = run


def discover_vectors(source: Path, recursive: bool = True) -> tuple[Path, ...]:
    """Discover a stable set of Shapefile entrypoints from a file or directory."""
    source = Path(source).expanduser().resolve()
    if source.is_file():
        if source.suffix.casefold() != ".shp":
            raise ValueError(f"vector input must be a .shp file: {source}")
        return (source,)
    if not source.is_dir():
        raise ValueError(f"vector input does not exist: {source}")
    iterator = source.rglob("*") if recursive else source.iterdir()
    vectors = tuple(
        sorted(
            path.resolve()
            for path in iterator
            if path.is_file()
            and path.suffix.casefold() == ".shp"
            and not any(part.casefold().startswith(".geotoolbox") for part in path.relative_to(source).parts[:-1])
        )
    )
    if not vectors:
        raise ValueError(f"no Shapefile was found in: {source}")
    return vectors


def _relative_label(path: Path, root: Path) -> str:
    if root.is_dir():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.name


def _requests(request: ZonalBatchRequest) -> tuple[ZonalRequest, ...]:
    vectors = discover_vectors(request.vector_source, request.recursive)
    if request.selected_inputs:
        allowed = {str(path).casefold() for path in request.selected_inputs}
        vectors = tuple(path for path in vectors if str(path).casefold() in allowed)
        if not vectors:
            raise ValueError("未勾选任何有效的矢量 Shapefile，无法执行批量赋值")
    source_root = request.vector_source if request.vector_source.is_dir() else request.vector_source.parent
    if request.vector_source.is_dir() and (
        request.output_directory == source_root or source_root in request.output_directory.parents
    ):
        raise ValueError("batch output directory must not be the input directory or one of its subdirectories")
    return tuple(
        ZonalRequest(
            vector,
            request.raster,
            request.output_directory / vector.relative_to(source_root),
            request.options,
        )
        for vector in vectors
    )


def plan_batch(request: ZonalBatchRequest) -> ExecutionPlan:
    if not request.raster.is_file():
        raise ValueError(f"classification raster does not exist: {request.raster}")
    with rasterio.open(request.raster) as dataset:
        if dataset.count < 1:
            raise ValueError("classification raster has no bands")
        if not (request.options.raster_crs_override.strip() or dataset.crs):
            raise ValueError("raster CRS is missing; provide the corresponding CRS override")
        raster_metadata = {
            "width": dataset.width,
            "height": dataset.height,
            "dtype": dataset.dtypes[0],
            "bounds": tuple(dataset.bounds),
        }
    item_requests = _requests(request)
    item_plans = tuple(plan(item, raster_metadata=raster_metadata) for item in item_requests)
    items_preview = tuple(
        {
            "item_id": str(item.vector),
            "name": item.vector.name,
            "relative_path": _relative_label(item.vector, request.vector_source),
            "output": str(item.output_vector),
            "output_name": _relative_label(item.output_vector, request.output_directory),
            "size_summary": f"{int(item_plan.metrics['features']):,} 个面要素",
            "auxiliary": f"分类：{request.raster.name}",
            "match_mode": "全局共用" if len(item_requests) > 1 else "单文件直连",
            "tiles_total": None,
        }
        for item, item_plan in zip(item_requests, item_plans, strict=True)
    )
    return ExecutionPlan(
        tool="zonal-batch",
        inputs=(*tuple(item.vector for item in item_requests), request.raster),
        outputs=tuple(item.output_vector for item in item_requests),
        stages=("discover", "preflight", "assign", "report"),
        metrics={
            "items": len(item_requests),
            "features": sum(int(item.metrics["features"]) for item in item_plans),
            "tile_size": request.options.tile_size,
            "valid_values": request.options.valid_values,
            "recursive": request.recursive,
            "items_preview": items_preview,
            "warnings": [],
            "input_copy_bytes": 0,
        },
    )


def _batch_result(status, request, workspace, started, results, warnings, items_total):
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
            "tool": "zonal-batch",
            "status": status.value,
            "vector_source": str(request.vector_source),
            "raster": str(request.raster),
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
    return ZonalBatchResult(
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


def _pool_workers(request: ZonalBatchRequest, context: RunContext) -> int:
    scheduler = ResourceScheduler(context.resources)
    requested = request.options.workers or context.resources.raster_workers
    if requested is not None:
        scheduler = ResourceScheduler(replace(context.resources, raster_workers=requested))
    bytes_per_worker = request.options.tile_size**2 * 16
    return scheduler.zonal_workers(1024, bytes_per_worker)


def run_batch(request: ZonalBatchRequest, context: RunContext | None = None) -> ZonalBatchResult:
    """Assign one classification raster to every discovered Shapefile with tail-overlap execution."""
    started = time.perf_counter()
    execution_plan = plan_batch(request)
    item_requests = _requests(request)
    context = context or RunContext(request.output_directory / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "items": [
                {"vector": dataset_fingerprint(item.vector), "output": str(item.output_vector)}
                for item in item_requests
            ],
            "raster": file_fingerprint(request.raster),
        }
    )
    workspace = RunWorkspace(context.work_dir, "zonal-batch", fingerprint, context.resume)
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
            return ZonalBatchResult(
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

    context.emit("started", "zonal-batch", "批量分类赋值开始", plan=execution_plan.metrics)
    warnings: list[str] = []
    cancelled = False
    predetected_values: tuple[int, ...] | None = request.options.valid_values or None
    if predetected_values is None:
        try:
            context.emit(
                "stage",
                "scan",
                f"正在识别分类影像有效像元值：{request.raster.name}",
            )
            predetected_values = detect_raster_values(request.raster, request.options.nodata, context)
        except CancelledError as error:
            warnings.append(str(error))
            cancelled = True

    use_shared_pool = (
        not cancelled
        and run is _REAL_ZONAL_RUN
        and len(item_requests) > 1
        and all(item.vector.suffix.casefold() == ".shp" for item in item_requests)
    )
    pool_workers = _pool_workers(request, context) if use_shared_pool else 1
    shared_executor = ProcessPoolExecutor(max_workers=pool_workers) if (use_shared_pool and pool_workers > 1) else None
    results = []
    try:
        if not cancelled and shared_executor is not None:
            ordered_results: list = [None] * len(item_requests)
            completed_count = 0

            def record_item(idx: int, item: ZonalRequest, item_ctx: RunContext, item_result) -> None:
                nonlocal completed_count, cancelled
                ordered_results[idx] = item_result
                completed_count += 1
                if item_result.status is TaskStatus.CANCELLED:
                    cancelled = True
                    warnings.extend(item_result.warnings)
                elif item_result.status is not TaskStatus.SUCCEEDED:
                    warnings.append(f"{item.vector}: {item_result.status.value}")
                    warnings.extend(item_result.warnings)
                item_ctx.emit(
                    "progress",
                    "batch",
                    f"已完成 {completed_count}/{len(item_requests)}：{item.vector.name}",
                    completed=completed_count,
                    total=len(item_requests),
                    output=str(item.output_vector),
                    item_status=item_result.status.value,
                )

            pending: deque[tuple[int, ZonalRequest, RunContext, Future, threading.Event]] = deque()
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="zonal-item") as item_pool:
                for idx, item in enumerate(item_requests):
                    if len(pending) >= 2:
                        old_idx, old_item, old_ctx, old_fut, _ = pending.popleft()
                        record_item(old_idx, old_item, old_ctx, old_fut.result())
                        if cancelled:
                            break
                    if pending:
                        prev_tail = pending[-1][4]
                        while not prev_tail.wait(timeout=0.2):
                            context.checkpoint()
                        if pending and pending[0][3].done():
                            old_idx, old_item, old_ctx, old_fut, _ = pending.popleft()
                            record_item(old_idx, old_item, old_ctx, old_fut.result())
                            if cancelled:
                                break
                    try:
                        context.checkpoint()
                    except CancelledError as error:
                        warnings.append(str(error))
                        cancelled = True
                        break
                    item_ctx = context.scoped(
                        item_id=str(item.vector),
                        item_index=idx + 1,
                        item_name=item.vector.name,
                    )
                    item_ctx.emit(
                        "progress",
                        "batch",
                        f"正在处理 {idx + 1}/{len(item_requests)}：{item.vector.name}",
                        completed=completed_count,
                        total=len(item_requests),
                        input=str(item.vector),
                    )
                    tail_event = threading.Event()
                    fut = item_pool.submit(
                        run,
                        item,
                        item_ctx,
                        shared_executor=shared_executor,
                        predetected_values=predetected_values,
                        on_tail_ready=tail_event.set,
                    )
                    pending.append((idx, item, item_ctx, fut, tail_event))
                while pending:
                    old_idx, old_item, old_ctx, old_fut, _ = pending.popleft()
                    record_item(old_idx, old_item, old_ctx, old_fut.result())
            results = [res for res in ordered_results if res is not None]
        elif not cancelled:
            for index, item in enumerate(item_requests, 1):
                try:
                    context.checkpoint()
                except CancelledError as error:
                    warnings.append(str(error))
                    cancelled = True
                    break
                item_ctx = context.scoped(
                    item_id=str(item.vector),
                    item_index=index,
                    item_name=item.vector.name,
                )
                item_ctx.emit(
                    "progress",
                    "batch",
                    f"正在处理 {index}/{len(item_requests)}：{item.vector.name}",
                    completed=index - 1,
                    total=len(item_requests),
                    input=str(item.vector),
                )
                if predetected_values is not None:
                    try:
                        item_result = run(
                            item,
                            item_ctx,
                            predetected_values=predetected_values,
                        )
                    except TypeError:
                        item_result = run(item, item_ctx)
                else:
                    item_result = run(item, item_ctx)
                results.append(item_result)
                if item_result.status is TaskStatus.CANCELLED:
                    cancelled = True
                    warnings.extend(item_result.warnings)
                    break
                if item_result.status is not TaskStatus.SUCCEEDED:
                    warnings.append(f"{item.vector}: {item_result.status.value}")
                    warnings.extend(item_result.warnings)
                item_ctx.emit(
                    "progress",
                    "batch",
                    f"已完成 {index}/{len(item_requests)}：{item.vector.name}",
                    completed=index,
                    total=len(item_requests),
                    output=str(item.output_vector),
                    item_status=item_result.status.value,
                )
    except CancelledError as error:
        warnings.append(str(error))
        cancelled = True
    finally:
        if shared_executor is not None:
            shared_executor.shutdown(wait=True)

    succeeded = sum(result.status is TaskStatus.SUCCEEDED for result in results)
    if cancelled:
        status = TaskStatus.CANCELLED
    elif succeeded == len(item_requests):
        status = TaskStatus.SUCCEEDED
    elif succeeded:
        status = TaskStatus.PARTIAL
    else:
        status = TaskStatus.FAILED
    result = _batch_result(status, request, workspace, started, results, warnings, len(item_requests))
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
            result = ZonalBatchResult(
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
    context.emit(status.value, "zonal-batch", f"批量分类赋值结束：{status.value}")
    return result
