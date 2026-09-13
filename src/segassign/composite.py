"""The composite backend only sequences segment -> zonal through public results."""

from __future__ import annotations

import time
import traceback
from dataclasses import asdict
from pathlib import Path

import rasterio

from . import segment, zonal
from .models import (
    Artifact,
    CompositeRequest,
    CompositeResult,
    ExecutionPlan,
    SegmentRequest,
    TaskStatus,
    ZonalRequest,
)
from .runtime import RunContext, RunWorkspace
from .runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, stable_digest


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


def run(request: CompositeRequest, context: RunContext | None = None) -> CompositeResult:
    started = time.perf_counter()
    execution_plan = plan(request)
    context = context or RunContext(request.output_vector.parent / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "process": file_fingerprint(request.process_raster),
            "base": file_fingerprint(request.base_raster),
            "classification": file_fingerprint(request.classification_raster),
        }
    )
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
    segment_result = None
    zonal_result = None
    try:
        segment_result = segment.run(segment_request, context)
        if segment_result.status is not TaskStatus.SUCCEEDED:
            status = segment_result.status
            warnings = ("zonal was not started because segment did not succeed", *segment_result.warnings)
        else:
            # The concrete primary vector is authoritative. No directory search
            # or guessed filename is permitted at this boundary.
            zonal_request = ZonalRequest(
                segment_result.primary_vector,
                zonal_template.raster,
                zonal_template.output_vector,
                zonal_template.options,
            )
            zonal_result = zonal.run(zonal_request, context)
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
                "tool": "composite", "status": TaskStatus.FAILED.value,
                "request": asdict(request), "error": detail, "timings": timings,
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


__all__ = ["plan", "run"]
