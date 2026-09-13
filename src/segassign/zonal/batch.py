"""Native, resumable orchestration for assigning one raster to many Shapefiles."""

from __future__ import annotations

import time
from pathlib import Path

from ..models import (
    Artifact,
    ExecutionPlan,
    TaskStatus,
    ZonalBatchRequest,
    ZonalBatchResult,
    ZonalRequest,
)
from ..runtime import CancelledError, RunContext, RunWorkspace
from ..runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest
from .service import plan, run


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


def _requests(request: ZonalBatchRequest) -> tuple[ZonalRequest, ...]:
    vectors = discover_vectors(request.vector_source, request.recursive)
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
    item_requests = _requests(request)
    item_plans = tuple(plan(item) for item in item_requests)
    return ExecutionPlan(
        tool="zonal-batch",
        inputs=(*tuple(item.vector for item in item_requests), request.raster),
        outputs=tuple(item.output_vector for item in item_requests),
        stages=("discover", "preflight", "assign", "report"),
        metrics={
            "items": len(item_requests),
            "features": sum(int(item.metrics["features"]) for item in item_plans),
            "recursive": request.recursive,
            "valid_values": request.options.valid_values,
            "input_copy_bytes": 0,
        },
    )


def _batch_result(
    status: TaskStatus,
    request: ZonalBatchRequest,
    workspace: RunWorkspace,
    started: float,
    results,
    warnings,
    items_total: int,
) -> ZonalBatchResult:
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
            "input": str(request.vector_source),
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


def run_batch(request: ZonalBatchRequest, context: RunContext | None = None) -> ZonalBatchResult:
    """Run the discovered items sequentially; each item uses all assigned raster workers."""
    started = time.perf_counter()
    execution_plan = plan_batch(request)
    item_requests = _requests(request)
    context = context or RunContext(request.output_directory / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "vectors": [dataset_fingerprint(item.vector) for item in item_requests],
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
    results = []
    warnings: list[str] = []
    cancelled = False
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
            f"正在处理 {index}/{len(item_requests)}：{item.vector.name}",
            completed=index - 1,
            total=len(item_requests),
            input=str(item.vector),
        )
        result = run(item, context)
        results.append(result)
        if result.status is TaskStatus.CANCELLED:
            cancelled = True
            warnings.extend(result.warnings)
            break
        if result.status is not TaskStatus.SUCCEEDED:
            warnings.append(f"{item.vector}: {result.status.value}")
            warnings.extend(result.warnings)
        context.emit(
            "progress",
            "batch",
            f"已完成 {index}/{len(item_requests)}：{item.vector.name}",
            completed=index,
            total=len(item_requests),
            output=str(item.output_vector),
        )

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
    context.emit(status.value, "zonal-batch", f"批量分类赋值结束：{status.value}")
    return result
