"""Native, resumable folder batch orchestration for segmentation (01) with cross-scene pipelining."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..models import (
    Artifact,
    ExecutionPlan,
    SegmentBatchRequest,
    SegmentBatchResult,
    SegmentRequest,
    SegmentResult,
    TaskStatus,
)
from ..raster_batch import RasterMatcher, discover_rasters, output_vector_for, validate_output_directory
from ..runtime import CancelledError, EngineLease, RunContext, RunWorkspace
from ..runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest
from .engine import wait_for_external_engines
from .service import (
    _REAL_SEGMENT_RUN,
    PrefetchedRow,
    _finalize_segment_stage,
    _run_engine_stage,
    plan,
    prefetch_first_row,
    run,
)


def _resolve_items(request: SegmentBatchRequest) -> tuple[tuple[SegmentRequest, ...], list[dict], list[str]]:
    validate_output_directory(request.output_directory, (request.process_source, request.base_source))
    process_rasters = discover_rasters(request.process_source, recursive=request.recursive, label="亚米级影像")
    matcher = RasterMatcher(
        request.base_source,
        recursive=request.recursive,
        label="2 米基础影像",
        default_crs=request.options.default_crs,
    )
    items: list[SegmentRequest] = []
    pairs: list[dict] = []
    warnings: list[str] = []
    for process_path in process_rasters:
        matched = matcher.match(process_path, request.process_source)
        output_vector = output_vector_for(process_path, request.process_source, request.output_directory)
        items.append(
            SegmentRequest(
                process_raster=process_path,
                base_raster=matched.path,
                output_vector=output_vector,
                ecognition_bin=request.ecognition_bin,
                ruleset=request.ruleset,
                invalid_ruleset=request.invalid_ruleset,
                bridge=request.bridge,
                options=request.options,
            )
        )
        pairs.append(
            {
                "process": str(process_path),
                "base": str(matched.path),
                "base_match": matched.method,
                "output": str(output_vector),
            }
        )
        if matched.warning:
            warnings.append(matched.warning)
    return tuple(items), pairs, warnings


def plan_batch(request: SegmentBatchRequest) -> ExecutionPlan:
    item_requests, pairs, warnings = _resolve_items(request)
    item_plans = tuple(plan(item) for item in item_requests)
    inputs = tuple(
        dict.fromkeys(
            [
                *(item.process_raster for item in item_requests),
                *(item.base_raster for item in item_requests),
                request.ruleset,
            ]
        )
    )
    return ExecutionPlan(
        tool="segment-batch",
        inputs=inputs,
        outputs=tuple(item.output_vector for item in item_requests),
        stages=("discover", "preflight", "segment", "report"),
        metrics={
            "items": len(item_requests),
            "tiles_total": sum(int(item.metrics["tiles"]) for item in item_plans),
            "recursive": request.recursive,
            "pairs": pairs,
            "warnings": warnings,
            "input_copy_bytes": 0,
        },
    )


def _batch_result(
    status: TaskStatus,
    request: SegmentBatchRequest,
    workspace: RunWorkspace,
    started: float,
    results,
    warnings,
    items_total: int,
    *,
    pipelined: bool = False,
) -> SegmentBatchResult:
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
            "tool": "segment-batch",
            "status": status.value,
            "pipelined": pipelined,
            "process_source": str(request.process_source),
            "base_source": str(request.base_source),
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
    return SegmentBatchResult(
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


def run_batch(request: SegmentBatchRequest, context: RunContext | None = None) -> SegmentBatchResult:
    """Run discovered segmentation items with cross-scene prefetch and overlapped finalization."""
    started = time.perf_counter()
    execution_plan = plan_batch(request)
    item_requests, _, pairing_warnings = _resolve_items(request)
    context = context or RunContext(request.output_directory / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "items": [
                {
                    "process": file_fingerprint(item.process_raster),
                    "base": file_fingerprint(item.base_raster),
                    "output": str(item.output_vector),
                }
                for item in item_requests
            ],
            "ruleset": file_fingerprint(request.ruleset),
        }
    )
    workspace = RunWorkspace(context.work_dir, "segment-batch", fingerprint, context.resume)
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
            return SegmentBatchResult(
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

    context.emit("started", "segment-batch", "批量单独分割开始", plan=execution_plan.metrics)
    warnings: list[str] = list(pairing_warnings)
    cancelled = False
    use_pipeline = run is _REAL_SEGMENT_RUN and len(item_requests) > 1

    if not use_pipeline:
        results: list[SegmentResult] = []
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
                f"正在分割 {index}/{len(item_requests)}：{item.process_raster.name}",
                completed=index - 1,
                total=len(item_requests),
                input=str(item.process_raster),
            )
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
                f"已完成分割 {index}/{len(item_requests)}：{item.process_raster.name}",
                completed=index,
                total=len(item_requests),
                output=str(item.output_vector),
            )
    else:
        ordered_results: list[SegmentResult | None] = [None] * len(item_requests)
        completed_count = 0

        def record_result(idx: int, item: SegmentRequest, result: SegmentResult) -> None:
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
                f"已完成分割 {completed_count}/{len(item_requests)}：{item.process_raster.name}",
                completed=completed_count,
                total=len(item_requests),
                output=str(item.output_vector),
            )

        prefetched_by_index: dict[int, PrefetchedRow | None] = {}
        pending_finalize: tuple[int, SegmentRequest, Future] | None = None
        engine_lease = EngineLease(context)
        try:
            engine_lease.__enter__()
            wait_for_external_engines(context)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="seg-finalize") as finalize_pool:
                for idx, item in enumerate(item_requests):
                    try:
                        context.checkpoint()
                    except CancelledError as error:
                        warnings.append(str(error))
                        cancelled = True
                        break
                    context.emit(
                        "progress",
                        "batch",
                        f"正在分割 {idx + 1}/{len(item_requests)}：{item.process_raster.name}",
                        completed=completed_count,
                        total=len(item_requests),
                        input=str(item.process_raster),
                    )
                    next_idx = idx + 1

                    def _make_prefetch(target_idx: int):
                        def _do_prefetch() -> None:
                            prefetched_by_index[target_idx] = prefetch_first_row(item_requests[target_idx], context)

                        return _do_prefetch

                    on_prep_done = _make_prefetch(next_idx) if next_idx < len(item_requests) else None
                    staged = _run_engine_stage(
                        item,
                        context,
                        hold_lease=False,
                        prefetched=prefetched_by_index.pop(idx, None),
                        on_prepare_complete=on_prep_done,
                    )
                    # Bound background finalization to 1 scene at a time before queueing the next.
                    if pending_finalize is not None:
                        prev_idx, prev_item, prev_future = pending_finalize
                        pending_finalize = None
                        record_result(prev_idx, prev_item, prev_future.result())
                    if isinstance(staged, SegmentResult):
                        record_result(idx, item, staged)
                        if staged.status is TaskStatus.CANCELLED:
                            break
                        continue
                    # Cap mosaic/postprocess threads slightly while the next scene uses eCognition.
                    bg_workers = min(4, staged.post_workers) if next_idx < len(item_requests) else staged.post_workers
                    pending_finalize = (
                        idx,
                        item,
                        finalize_pool.submit(_finalize_segment_stage, staged, mosaic_workers=bg_workers),
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
            result = SegmentBatchResult(
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
    context.emit(status.value, "segment-batch", f"批量单独分割结束：{status.value}")
    return result
