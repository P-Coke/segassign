"""Read-only planning and transactional execution for zonal assignment."""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyogrio
import rasterio
import shapely
from pyproj import CRS, Transformer

from ..models import Artifact, ExecutionPlan, TaskStatus, ZonalRequest, ZonalResult
from ..runtime import CancelledError, PeakMemoryTracker, ResourceScheduler, RunContext, RunWorkspace, StageTimer
from ..runtime.workspace import dataset_fingerprint, dataset_matches, file_fingerprint, publish_shapefile, stable_digest
from .counting import ClassEncoding, bincount_rows, rasterize_labels, statistics_frame_dense
from .shapefile import (
    envelope_geometries,
    merge_assignment_attributes,
    read_envelope_index,
    read_polygon_bounds,
    transform_envelopes,
    write_attributes_with_source_geometry,
)
from .spatial import balanced_batches, intersect_bounds, plan_envelope_jobs, plan_window_jobs
from .state import merge_counts, read_count_state, write_count_state


def _writable_parent(path: Path) -> bool:
    candidate = path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _resolve_crs(detected, override: str, label: str) -> CRS:
    value = override.strip() if override else detected
    if not value:
        raise ValueError(f"{label} CRS is missing; provide the corresponding CRS override")
    return CRS.from_user_input(value)


def _read_and_transform(request: ZonalRequest):
    frame = pyogrio.read_dataframe(request.vector, fid_as_index=True)
    if frame.empty:
        return frame, frame.geometry.array, None, None
    vector_crs = _resolve_crs(frame.crs, request.options.vector_crs_override, "vector")
    with rasterio.open(request.raster) as dataset:
        raster_crs = _resolve_crs(dataset.crs, request.options.raster_crs_override, "raster")
    geometries = frame.geometry.array.copy()
    if vector_crs != raster_crs:
        transformer = Transformer.from_crs(vector_crs, raster_crs, always_xy=True)
        geometries = shapely.transform(geometries, transformer.transform, interleaved=False)
    return frame, geometries, vector_crs, raster_crs


def _geometry_bounds(geometries) -> tuple[float, float, float, float] | None:
    if not len(geometries):
        return None
    bounds = shapely.bounds(geometries)
    bounds = bounds[~np.isnan(bounds).any(axis=1)]
    if not bounds.size:
        return None
    return (
        float(bounds[:, 0].min()),
        float(bounds[:, 1].min()),
        float(bounds[:, 2].max()),
        float(bounds[:, 3].max()),
    )


def _preflight(request: ZonalRequest) -> dict:
    errors: list[str] = []
    for label, path in (("vector", request.vector), ("raster", request.raster)):
        if not path.is_file():
            errors.append(f"{label} does not exist: {path}")
    if not _writable_parent(request.output_vector):
        errors.append(f"output parent is not writable: {request.output_vector.parent}")
    if request.output_vector.resolve() == request.vector.resolve():
        errors.append("output_vector must differ from the input vector")
    if errors:
        raise ValueError("; ".join(errors))
    info = pyogrio.read_info(request.vector)
    with rasterio.open(request.raster) as dataset:
        if dataset.count < 1:
            errors.append("classification raster has no bands")
        _resolve_crs(dataset.crs, request.options.raster_crs_override, "raster")
        raster = {
            "width": dataset.width,
            "height": dataset.height,
            "dtype": dataset.dtypes[0],
            "bounds": tuple(dataset.bounds),
        }
    _resolve_crs(info.get("crs"), request.options.vector_crs_override, "vector")
    if errors:
        raise ValueError("; ".join(errors))
    return {"features": int(info["features"]), "raster": raster}


def plan(request: ZonalRequest) -> ExecutionPlan:
    metadata = _preflight(request)
    return ExecutionPlan(
        tool="zonal",
        inputs=(request.vector, request.raster),
        outputs=(request.output_vector,),
        stages=("preflight", "load", "spatial-index", "count", "write", "publish"),
        metrics={
            **metadata,
            "tile_size": request.options.tile_size,
            "valid_values": request.options.valid_values,
            "input_copy_bytes": 0,
        },
    )


def _detect_values(path: Path, nodata: int, context: RunContext) -> tuple[int, ...]:
    values: set[int] = set()
    with rasterio.open(path) as dataset:
        for _, window in dataset.block_windows(1):
            context.checkpoint()
            values.update(int(value) for value in np.unique(dataset.read(1, window=window)) if int(value) != nodata)
    return tuple(sorted(values))


def _process_batch(request, geometries, batch, encoding, context):
    local = np.zeros((len(geometries), len(encoding.values)), dtype=np.int64)
    with rasterio.open(request.raster) as dataset:
        for job in batch:
            context.checkpoint()
            if not job.candidates.size:
                continue
            pixels = dataset.read(1, window=job.window)
            labels = rasterize_labels(
                geometries,
                job.candidates,
                pixels.shape,
                dataset.window_transform(job.window),
            )
            local[job.candidates] += bincount_rows(pixels, labels, job.candidates, encoding, request.options.nodata)
    return local, len(batch)


def _process_shapefile_batch(request, batch, encoding, vector_crs, raster_crs):
    if not batch:
        return np.empty(0, dtype=np.int64), np.zeros((0, len(encoding.values)), dtype=np.int64), 0
    nonempty = [job.candidates for job in batch if job.candidates.size]
    positions = np.unique(np.concatenate(nonempty)) if nonempty else np.empty(0, dtype=np.int64)
    if not positions.size:
        return positions, np.zeros((0, len(encoding.values)), dtype=np.int64), len(batch)
    frame = pyogrio.read_dataframe(
        request.vector,
        columns=[],
        fids=positions.tolist(),
        fid_as_index=True,
    )
    geometries = frame.geometry.array
    loaded_positions = frame.index.to_numpy(dtype=np.int64)
    order = np.argsort(loaded_positions)
    loaded_positions = loaded_positions[order]
    geometries = geometries[order]
    if vector_crs != raster_crs:
        transformer = Transformer.from_crs(vector_crs, raster_crs, always_xy=True)
        geometries = shapely.transform(geometries, transformer.transform, interleaved=False)
    local_counts = np.zeros((len(loaded_positions), len(encoding.values)), dtype=np.int64)
    with rasterio.open(request.raster) as dataset:
        for job in batch:
            if not job.candidates.size:
                continue
            local_positions = np.searchsorted(loaded_positions, job.candidates)
            pixels = dataset.read(1, window=job.window)
            labels = rasterize_labels(
                geometries,
                local_positions,
                pixels.shape,
                dataset.window_transform(job.window),
            )
            local_counts[local_positions] += bincount_rows(
                pixels,
                labels,
                local_positions,
                encoding,
                request.options.nodata,
            )
    return loaded_positions, local_counts, len(batch)


def _failure_result(request, workspace, timings, memory, status, error) -> ZonalResult:
    report = workspace.report(
        "run",
        {
            "tool": "zonal",
            "status": status.value,
            "request": str(request),
            "error": error,
            "timings": timings,
            "peak_memory_mb": memory.peak_mb,
        },
    )
    return ZonalResult(status, (), (error,), timings, memory.peak_mb, report, request.output_vector, 0, 0)


def run(request: ZonalRequest, context: RunContext | None = None) -> ZonalResult:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    # This validation is deliberately before RunWorkspace.ensure(): invalid requests write nothing.
    execution_plan = plan(request)
    context = context or RunContext(request.output_vector.parent / ".geotoolbox-work")
    fingerprint = stable_digest(
        {
            "request": request,
            "vector": dataset_fingerprint(request.vector),
            "raster": file_fingerprint(request.raster),
        }
    )
    workspace = RunWorkspace(context.work_dir, "zonal", fingerprint, context.resume)
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
        return ZonalResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", request.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.0},
            0.0,
            report,
            request.output_vector,
            int(complete["features"]),
            int(complete["features_with_statistics"]),
        )
    context.emit("started", "zonal", "Zonal assignment started", plan=execution_plan.metrics)
    with PeakMemoryTracker() as memory:
        try:
            context.checkpoint()
            with StageTimer(timings, "load"):
                values = request.options.valid_values or _detect_values(request.raster, request.options.nodata, context)
                encoding = ClassEncoding.create(values) if values else None
                fast_shapefile = request.vector.suffix.casefold() == ".shp"
                if fast_shapefile:
                    vector_info = pyogrio.read_info(request.vector)
                    feature_count = int(vector_info["features"])
                    vector_crs = _resolve_crs(vector_info.get("crs"), request.options.vector_crs_override, "vector")
                    with rasterio.open(request.raster) as dataset:
                        raster_crs = _resolve_crs(dataset.crs, request.options.raster_crs_override, "raster")
                    cached_bounds = read_envelope_index(request.vector)
                    spatial_index_cache_hit = cached_bounds is not None
                    source_bounds = cached_bounds if cached_bounds is not None else read_polygon_bounds(request.vector)
                    envelope_bounds = transform_envelopes(source_bounds, vector_crs, raster_crs)
                    geometries = envelope_geometries(envelope_bounds)
                    frame = None
                else:
                    frame, geometries, vector_crs, raster_crs = _read_and_transform(request)
                    feature_count = len(frame)
                    spatial_index_cache_hit = False
            with StageTimer(timings, "spatial_index"):
                with rasterio.open(request.raster) as dataset:
                    geometry_bounds = _geometry_bounds(geometries)
                    intersection = (
                        intersect_bounds(geometry_bounds, tuple(dataset.bounds))
                        if geometry_bounds is not None
                        else None
                    )
                    if intersection is None or encoding is None:
                        jobs = []
                    elif fast_shapefile:
                        jobs = plan_envelope_jobs(
                            geometries,
                            intersection,
                            dataset.transform,
                            dataset.width,
                            dataset.height,
                            request.options.tile_size,
                        )
                    else:
                        jobs = plan_window_jobs(
                            geometries,
                            intersection,
                            dataset.transform,
                            dataset.width,
                            dataset.height,
                            request.options.tile_size,
                        )
            candidate_queries = sum(job.candidates.size for job in jobs)
            scheduler = ResourceScheduler(context.resources)
            bytes_per_worker = request.options.tile_size**2 * 16
            requested = request.options.workers or context.resources.raster_workers
            budget = context.resources
            if requested is not None:
                from dataclasses import replace

                scheduler = ResourceScheduler(replace(budget, raster_workers=requested))
            workers = scheduler.zonal_workers(max(1, len(jobs)), bytes_per_worker)
            counts = np.zeros((feature_count, len(values)), dtype=np.int64)
            completed = 0
            with StageTimer(timings, "count"):
                batches = balanced_batches(jobs, workers) if jobs else []
                executor_class = ProcessPoolExecutor if fast_shapefile else ThreadPoolExecutor
                with executor_class(max_workers=workers) as executor:
                    if fast_shapefile:
                        futures = [
                            executor.submit(
                                _process_shapefile_batch,
                                request,
                                batch,
                                encoding,
                                vector_crs,
                                raster_crs,
                            )
                            for batch in batches
                        ]
                    else:
                        futures = [
                            executor.submit(_process_batch, request, geometries, batch, encoding, context)
                            for batch in batches
                        ]
                    for future in as_completed(futures):
                        if fast_shapefile:
                            positions, local, window_count = future.result()
                            counts[positions] += local
                        else:
                            local, window_count = future.result()
                            counts += local
                        completed += window_count
                        context.emit(
                            "progress",
                            "count",
                            f"Counted {completed}/{len(jobs)} raster windows",
                            completed=completed,
                            total=len(jobs),
                        )
            context.checkpoint()
            with StageTimer(timings, "write"):
                combined_values, combined_counts = merge_counts(
                    read_count_state(request.vector, feature_count),
                    np.asarray(values, dtype=np.int64),
                    counts,
                )
                statistics = statistics_frame_dense(
                    combined_counts,
                    combined_values,
                    request.options.class_threshold,
                )
                update_mask = combined_counts.sum(axis=1) > 0
                staged = workspace.staging / request.output_vector.name
                for old in staged.parent.glob(staged.stem + ".*"):
                    old.unlink(missing_ok=True)
                if fast_shapefile:
                    write_attributes_with_source_geometry(
                        request.vector,
                        statistics,
                        update_mask,
                        staged,
                        request.options.output_encoding,
                    )
                else:
                    attributes = merge_assignment_attributes(
                        frame.drop(columns=frame.geometry.name),
                        statistics,
                        update_mask,
                    )
                    output = frame[[frame.geometry.name]].copy()
                    for column in attributes.columns:
                        output[column] = attributes[column].to_numpy()
                    pyogrio.write_dataframe(
                        output,
                        staged,
                        driver="ESRI Shapefile",
                        encoding=request.options.output_encoding,
                    )
                validation = pyogrio.read_info(staged)
                if int(validation["features"]) != feature_count:
                    raise RuntimeError("staged Shapefile feature count changed during write")
                write_count_state(staged, combined_values, combined_counts)
            context.checkpoint()
            with StageTimer(timings, "publish"):
                publish_shapefile(staged, request.output_vector)
            timings["total"] = round(time.perf_counter() - started, 6)
            report = workspace.report(
                "run",
                {
                    "tool": "zonal",
                    "status": TaskStatus.SUCCEEDED.value,
                    "request_fingerprint": fingerprint,
                    "output": str(request.output_vector),
                    "features": feature_count,
                    "features_with_statistics": int(np.count_nonzero(combined_counts.sum(axis=1))),
                    "values": [int(value) for value in combined_values],
                    "windows": len(jobs),
                    "candidate_queries": int(candidate_queries),
                    "spatial_index_cache_hit": spatial_index_cache_hit,
                    "workers": workers,
                    "timings": timings,
                    "peak_memory_mb": memory.peak_mb,
                },
            )
            workspace.mark(
                "complete",
                fingerprint,
                output=str(request.output_vector),
                report=str(report),
                features=feature_count,
                features_with_statistics=int(np.count_nonzero(combined_counts.sum(axis=1))),
                output_fingerprint=dataset_fingerprint(request.output_vector),
            )
            context.emit("completed", "zonal", "Zonal assignment completed", output=str(request.output_vector))
            return ZonalResult(
                TaskStatus.SUCCEEDED,
                (Artifact("primary_vector", request.output_vector), Artifact("run_report", report)),
                (),
                timings,
                memory.peak_mb,
                report,
                request.output_vector,
                feature_count,
                int(np.count_nonzero(combined_counts.sum(axis=1))),
            )
        except CancelledError as error:
            timings["total"] = round(time.perf_counter() - started, 6)
            context.emit("cancelled", "zonal", str(error))
            return _failure_result(request, workspace, timings, memory, TaskStatus.CANCELLED, str(error))
        except Exception as error:  # API boundary: diagnostics belong in the result/report.
            timings["total"] = round(time.perf_counter() - started, 6)
            detail = f"{type(error).__name__}: {error}"
            context.emit("failed", "zonal", detail)
            result = _failure_result(
                request,
                workspace,
                timings,
                memory,
                TaskStatus.FAILED,
                detail + "\n" + traceback.format_exc(),
            )
            return result
