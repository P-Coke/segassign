"""Transactional, resumable segmentation pipeline."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

from ..models import Artifact, ExecutionPlan, SegmentRequest, SegmentResult, TaskStatus
from ..runtime import (
    CancelledError,
    EngineLease,
    PeakMemoryTracker,
    ResourceScheduler,
    RunContext,
    RunWorkspace,
    StageTimer,
)
from ..runtime.workspace import (
    atomic_json,
    dataset_fingerprint,
    dataset_matches,
    file_fingerprint,
    publish_shapefile,
    stable_digest,
)
from .engine import run_tile, wait_for_external_engines
from .grid import Tile, build_grid
from .mosaic import assemble
from .postprocess import process_tile, process_tile_batch
from .raster_io import BlockAwareReader, SkipTile, assemble_process_footprint, prepare_tile, resolve_crs
from .reading import RasterReadError, RasterWindowReader
from .ruleset import patch

BRIDGE_SHA256 = "f6bf51fdc0b711ec0588cb18a5b7b7be43a19c666fcd193885073785824aeac7"
SDK_FILES = ("Engine.dll", "DataIO.dll", "DBase.dll")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _writable_parent(path: Path) -> bool:
    candidate = path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _overlaps(process, process_crs, base, base_crs) -> bool:
    process_bounds = tuple(process.bounds)
    if process_crs != base_crs:
        process_bounds = transform_bounds(process_crs, base_crs, *process_bounds, densify_pts=21)
    base_bounds = tuple(base.bounds)
    return (
        process_bounds[0] < base_bounds[2]
        and process_bounds[2] > base_bounds[0]
        and process_bounds[1] < base_bounds[3]
        and process_bounds[3] > base_bounds[1]
    )


def _preflight(request: SegmentRequest) -> dict:
    errors: list[str] = []
    for label, path in (
        ("process raster", request.process_raster),
        ("base raster", request.base_raster),
        ("ruleset", request.ruleset),
        ("invalid-class ruleset", request.invalid_ruleset),
        ("bridge", request.bridge),
    ):
        if not path.is_file():
            errors.append(f"{label} does not exist: {path}")
    if not request.ecognition_bin.is_dir():
        errors.append(f"eCognition bin directory does not exist: {request.ecognition_bin}")
    else:
        errors.extend(
            f"eCognition SDK is missing {filename}: {request.ecognition_bin / filename}"
            for filename in SDK_FILES
            if not (request.ecognition_bin / filename).is_file()
        )
    if not _writable_parent(request.output_vector):
        errors.append(f"output parent is not writable: {request.output_vector.parent}")
    if errors:
        raise ValueError("; ".join(errors))
    if _sha256(request.bridge).casefold() != BRIDGE_SHA256:
        raise ValueError(f"bridge hash does not match the retained, verified adapter: {request.bridge}")
    try:
        ET.parse(request.ruleset)
        if request.options.ruleset_mode == "invalid_classify":
            ET.parse(request.invalid_ruleset)
    except ET.ParseError as error:
        raise ValueError(f"invalid eCognition ruleset XML: {error}") from error
    with rasterio.open(request.process_raster) as process, rasterio.open(request.base_raster) as base:
        if process.count < 1 or base.count < 1:
            raise ValueError("both input rasters must have at least one band")
        process_crs = resolve_crs(process, request.options.default_crs)
        base_crs = resolve_crs(base, request.options.default_crs)
        if not _overlaps(process, process_crs, base, base_crs):
            raise ValueError("process raster and base raster do not spatially overlap")
        tiles = build_grid(
            process.width,
            process.height,
            process.transform,
            request.options.tile_size,
            request.options.buffer_pixels,
        )
        return {
            "width": process.width,
            "height": process.height,
            "bands": process.count,
            "tiles": len(tiles),
            "process_layout": "full-width-striped" if BlockAwareReader(process).striped else "windowed",
            "tile_size": request.options.tile_size,
            "buffer_pixels": request.options.buffer_pixels,
            "input_copy_bytes": 0,
            "bridge_sha256": BRIDGE_SHA256,
            "ruleset_sha256": _sha256(request.ruleset),
        }


def plan(request: SegmentRequest) -> ExecutionPlan:
    metadata = _preflight(request)
    return ExecutionPlan(
        "segment",
        (request.process_raster, request.base_raster, request.ruleset),
        (request.output_vector,),
        ("preflight", "prepare", "ecognition", "postprocess", "mosaic", "publish"),
        metadata,
    )


def _stage_fingerprint(run_fingerprint: str, tile: Tile, stage: str) -> str:
    return stable_digest({"run": run_fingerprint, "tile": tile.serializable(), "stage": stage})


def _tile_requirements(tile_dir: Path, stage: str) -> tuple[Path, ...]:
    def shapefile(path: Path) -> tuple[Path, ...]:
        return tuple(path.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf"))

    if stage == "prepare":
        return (
            tile_dir / "input" / "process_tile.tif",
            tile_dir / "input" / "base_tile.tif",
            tile_dir / "ruleset" / "process_tile.dcp",
            tile_dir / "process_sample.npy",
            *shapefile(tile_dir / "vector" / "valid_core.shp"),
            *shapefile(tile_dir / "vector" / "valid_buffer.shp"),
        )
    if stage == "engine":
        return shapefile(tile_dir / "ecognition_export" / "ObjectShapes.shp")
    if stage == "postprocess":
        return shapefile(tile_dir / "postprocess" / "segments.shp")
    if stage == "support":
        return (
            tile_dir / "tile.json",
            tile_dir / "process_sample.npy",
            *shapefile(tile_dir / "vector" / "valid_core.shp"),
            *shapefile(tile_dir / "vector" / "valid_buffer.shp"),
        )
    return ()


def _mark_tile(workspace, tile, stage, fingerprint, **data):
    tile_dir = workspace.root / "tiles" / tile.tile_id
    requirements = _tile_requirements(tile_dir, stage)
    if stage == "engine":
        requirements = (*requirements, *_tile_requirements(tile_dir, "support"))
    artifacts = {
        str(path): file_fingerprint(path)
        for path in requirements
        if path.is_file() and path.stat().st_size > 0
    }
    workspace.mark(f"{tile.tile_id}-{stage}", fingerprint, artifacts=artifacts, **data)


def _cleanup_success(workspace: RunWorkspace, tile_root: Path, sandbox_root: Path, footprint: Path) -> tuple[str, ...]:
    warnings: list[str] = []
    for directory in (tile_root, sandbox_root, workspace.staging):
        try:
            shutil.rmtree(directory, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError as error:
            warnings.append(f"could not remove completed-run cache {directory}: {error}")
    for path in footprint.parent.glob(footprint.stem + ".*"):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            warnings.append(f"could not remove internal footprint {path}: {error}")
    return tuple(warnings)


def _release_engine_inputs(tile_dir: Path, sandbox_root: Path) -> None:
    """Bound disk usage once a durable export checkpoint exists."""
    for directory in (tile_dir / "input", tile_dir / "ruleset", sandbox_root / tile_dir.name):
        shutil.rmtree(directory, ignore_errors=True)


def _run_postprocess(
    tiles: list[Tile],
    tile_root: Path,
    workspace: RunWorkspace,
    run_fingerprint: str,
    request: SegmentRequest,
    workers: int,
) -> tuple[list[Path], int]:
    outputs: list[Path] = []
    pending: list[Tile] = []
    resumed = 0
    for tile in tiles:
        fingerprint = _stage_fingerprint(run_fingerprint, tile, "postprocess")
        checkpoint = workspace.read_checkpoint(f"{tile.tile_id}-postprocess")
        if workspace.resume and checkpoint and checkpoint.get("fingerprint") == fingerprint:
            output = checkpoint.get("output", "")
            required = _tile_requirements(tile_root / tile.tile_id, "postprocess") if output else ()
            if workspace.checkpoint_matches(f"{tile.tile_id}-postprocess", fingerprint, required):
                if output:
                    outputs.append(Path(output))
                resumed += 1
                continue
        pending.append(tile)
    if not pending:
        return outputs, resumed

    completed_ids: set[str] = set()
    retry_tiles: list[Tile] = []
    tile_by_id = {tile.tile_id: tile for tile in pending}
    batch_size = request.options.postprocess_max_tiles_per_worker
    for offset in range(0, len(pending), workers * batch_size):
        round_tiles = pending[offset : offset + workers * batch_size]
        batches = [round_tiles[index : index + batch_size] for index in range(0, len(round_tiles), batch_size)]
        try:
            # Rebuild the whole pool after each bounded round. On Windows,
            # max_tasks_per_child can stall while replacing all workers at once.
            with ProcessPoolExecutor(max_workers=min(workers, len(batches))) as pool:
                futures = {
                    pool.submit(
                        process_tile_batch,
                        tuple(tile_root / tile.tile_id for tile in batch),
                        request.options.invalid_class_name,
                        request.options.ruleset_mode == "invalid_classify",
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        values = future.result()
                    except Exception:
                        retry_tiles.extend(batch)
                        continue
                    for tile_id, value, error in values:
                        tile = tile_by_id[tile_id]
                        if error:
                            retry_tiles.append(tile)
                            continue
                        path = Path(value) if value else None
                        if path is not None:
                            outputs.append(path)
                        _mark_tile(
                            workspace,
                            tile,
                            "postprocess",
                            _stage_fingerprint(run_fingerprint, tile, "postprocess"),
                            output=str(path or ""),
                        )
                        completed_ids.add(tile.tile_id)
        except BrokenProcessPool:
            # A native GEOS/GDAL crash invalidates only this bounded round.
            retry_tiles.extend(tile for tile in round_tiles if tile.tile_id not in completed_ids)
    retry_by_id = {tile.tile_id: tile for tile in retry_tiles if tile.tile_id not in completed_ids}
    for tile in retry_by_id.values():
        with ProcessPoolExecutor(max_workers=1, max_tasks_per_child=1) as pool:
            value = pool.submit(
                process_tile,
                tile_root / tile.tile_id,
                request.options.invalid_class_name,
                request.options.ruleset_mode == "invalid_classify",
            ).result()
        path = Path(value) if value else None
        if path is not None:
            outputs.append(path)
        _mark_tile(
            workspace,
            tile,
            "postprocess",
            _stage_fingerprint(run_fingerprint, tile, "postprocess"),
            output=str(path or ""),
            recovered_after_worker_failure=True,
        )
    return outputs, resumed


def _retain_engine_checkpoints(futures, collect, context):
    # ThreadPoolExecutor has already joined its workers at the exception
    # boundary. Save their successful exports without masking the first error.
    for future in tuple(futures):
        if not future.done() or future.cancelled():
            continue
        try:
            collect((future,))
        except CancelledError:
            pass
        except Exception as error:
            context.emit("warning", "ecognition", f"保存分割断点时遇到错误：{type(error).__name__}: {error}")


def _report_failure(
    request, workspace, timings, memory, status, detail, tiles_total=0, tiles_processed=0, read_error=None,
):
    report = workspace.report(
        "run",
        {
            "tool": "segment",
            "status": status.value,
            "error": detail,
            "request": asdict(request),
            "read_error": read_error,
            "tiles_total": tiles_total,
            "tiles_processed": tiles_processed,
            "timings": timings,
            "peak_memory_mb": memory.peak_mb,
        },
    )
    return SegmentResult(
        status,
        (Artifact("run_report", report),),
        (detail,),
        timings,
        memory.peak_mb,
        report,
        request.output_vector,
        tiles_total,
        tiles_processed,
        0,
    )


def run(request: SegmentRequest, context: RunContext | None = None) -> SegmentResult:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    execution_plan = plan(request)  # must precede every workspace write
    context = context or RunContext(request.output_vector.parent / ".geotoolbox-work")
    run_fingerprint = stable_digest(
        {
            "request": request,
            "process": file_fingerprint(request.process_raster),
            "base": file_fingerprint(request.base_raster),
            "ruleset": file_fingerprint(request.ruleset),
            "bridge": BRIDGE_SHA256,
        }
    )
    workspace = RunWorkspace(context.work_dir, "segment", run_fingerprint, context.resume)
    workspace.ensure()
    complete = workspace.read_checkpoint("complete")
    if (
        context.resume
        and complete
        and complete.get("fingerprint") == run_fingerprint
        and dataset_matches(request.output_vector, complete.get("output_fingerprint"))
        and Path(complete.get("report", "")).is_file()
    ):
        report = Path(complete["report"])
        return SegmentResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", request.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.0},
            0.0,
            report,
            request.output_vector,
            int(complete["tiles_total"]),
            int(complete["tiles_processed"]),
            int(complete["tiles_processed"]),
        )
    tile_root = workspace.root / "tiles"
    tile_root.mkdir(exist_ok=True)
    sandbox_root = workspace.root / "e"
    context.emit("started", "segment", "Segmentation started", plan=execution_plan.metrics)
    with PeakMemoryTracker() as memory:
        engine_lease = None
        try:
            engine_lease = EngineLease(context)
            engine_lease.__enter__()
            wait_for_external_engines(context)
            with rasterio.open(request.process_raster) as process:
                process_crs = resolve_crs(process, request.options.default_crs)
                tiles = build_grid(
                    process.width,
                    process.height,
                    process.transform,
                    request.options.tile_size,
                    request.options.buffer_pixels,
                )
                raster_shape = (process.width, process.height, process.transform, process_crs)
            scheduler = ResourceScheduler(context.resources)
            engine_workers = scheduler.ecognition_workers(request.options.ecognition_workers)
            post_workers = scheduler.postprocess_workers(request.options.postprocess_workers)
            runnable: list[Tile] = []
            skipped: list[Tile] = []
            engine_resumed = 0
            engine_futures = {}
            engine_completed = 0
            prepare_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=engine_workers, thread_name_prefix="ecognition") as engine_pool:

                def collect_engine(completed_futures) -> None:
                    nonlocal engine_completed
                    for completed_future in completed_futures:
                        completed_tile = engine_futures.pop(completed_future)
                        result = completed_future.result()
                        _mark_tile(
                            workspace,
                            completed_tile,
                            "engine",
                            _stage_fingerprint(run_fingerprint, completed_tile, "engine"),
                            **result,
                        )
                        _release_engine_inputs(tile_root / completed_tile.tile_id, sandbox_root)
                        engine_completed += 1
                        context.emit(
                            "progress",
                            "ecognition",
                            f"eCognition completed {engine_resumed + engine_completed}/{len(runnable)} tiles",
                            completed=engine_resumed + engine_completed,
                        )

                def submit_engine(tile: Tile) -> None:
                    future = engine_pool.submit(run_tile, tile_root / tile.tile_id, sandbox_root, request, context)
                    engine_futures[future] = tile
                    if len(engine_futures) >= engine_workers * 2:
                        completed, _ = wait(engine_futures, return_when=FIRST_COMPLETED)
                        collect_engine(completed)

                with (
                    rasterio.open(request.process_raster) as process,
                    rasterio.open(request.base_raster) as base,
                    BlockAwareReader(process, context) as reader,
                    RasterWindowReader(base, role="冬季影像", context=context) as base_reader,
                ):
                    rows: dict[int, list[Tile]] = {}
                    for tile in tiles:
                        rows.setdefault(tile.row, []).append(tile)
                    for row_index in sorted(rows):
                        context.checkpoint()
                        row_tiles = rows[row_index]
                        needs_prepare: list[Tile] = []
                        for tile in row_tiles:
                            context.checkpoint()
                            tile_dir = tile_root / tile.tile_id
                            tile_dir.mkdir(parents=True, exist_ok=True)
                            prepare_fingerprint = _stage_fingerprint(run_fingerprint, tile, "prepare")
                            engine_fingerprint = _stage_fingerprint(run_fingerprint, tile, "engine")
                            if workspace.checkpoint_matches(
                                f"{tile.tile_id}-engine",
                                engine_fingerprint,
                                (
                                    *_tile_requirements(tile_dir, "engine"),
                                    *_tile_requirements(tile_dir, "support"),
                                ),
                            ):
                                runnable.append(tile)
                                engine_resumed += 1
                                continue
                            prepare_checkpoint = workspace.read_checkpoint(f"{tile.tile_id}-prepare")
                            if (
                                context.resume
                                and prepare_checkpoint
                                and prepare_checkpoint.get("fingerprint") == prepare_fingerprint
                                and prepare_checkpoint.get("skipped")
                                and workspace.checkpoint_matches(
                                    f"{tile.tile_id}-prepare",
                                    prepare_fingerprint,
                                    (tile_dir / "process_sample.npy",),
                                )
                            ):
                                skipped.append(tile)
                                continue
                            prepared = workspace.checkpoint_matches(
                                f"{tile.tile_id}-prepare",
                                prepare_fingerprint,
                                _tile_requirements(tile_dir, "prepare"),
                            )
                            if prepared:
                                runnable.append(tile)
                                submit_engine(tile)
                            else:
                                needs_prepare.append(tile)
                        # For a fully resumed row this deliberately performs no
                        # raster read. Only missing/invalid stages request the
                        # full-width stripe from the source TIFF.
                        row_data = reader.read_row(needs_prepare)
                        for tile in needs_prepare:
                            context.checkpoint()
                            tile_dir = tile_root / tile.tile_id
                            atomic_json(tile_dir / "tile.json", tile.serializable())
                            prepare_fingerprint = _stage_fingerprint(run_fingerprint, tile, "prepare")
                            try:
                                prepare_tile(
                                    tile, tile_dir, process, base, row_data[tile.tile_id], request.options, base_reader,
                                )
                                patch(
                                    request.ruleset,
                                    tile_dir / "ruleset" / "process_tile.dcp",
                                    tile_dir / "ecognition_export",
                                    request.options.ruleset_mode,
                                    request.invalid_ruleset,
                                    request.options.invalid_class_name,
                                )
                                _mark_tile(workspace, tile, "prepare", prepare_fingerprint, skipped=False)
                            except SkipTile as error:
                                skipped.append(tile)
                                _mark_tile(
                                    workspace,
                                    tile,
                                    "prepare",
                                    prepare_fingerprint,
                                    skipped=True,
                                    reason=str(error),
                                )
                                continue
                            runnable.append(tile)
                            submit_engine(tile)
                            context.emit(
                                "progress",
                                "prepare",
                                f"Prepared {len(runnable) + len(skipped)}/{len(tiles)} tiles",
                                completed=len(runnable) + len(skipped),
                                total=len(tiles),
                            )
                timings["prepare"] = round(time.perf_counter() - prepare_started, 6)
                engine_wait_started = time.perf_counter()
                collect_engine(list(as_completed(engine_futures)))
                timings["engine_wait_after_prepare"] = round(time.perf_counter() - engine_wait_started, 6)
            engine_lease.__exit__(None, None, None)
            engine_lease = None
            context.checkpoint()
            with StageTimer(timings, "footprint"):
                footprint = assemble_process_footprint(tiles, tile_root, *raster_shape, request.options)
            with StageTimer(timings, "postprocess"):
                outputs, post_resumed = _run_postprocess(
                    runnable, tile_root, workspace, run_fingerprint, request, post_workers
                )
            context.checkpoint()
            with StageTimer(timings, "mosaic"):
                staged = workspace.staging / request.output_vector.name
                transform = raster_shape[2]
                pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
                features = assemble(outputs, staged, pixel_area)
            context.checkpoint()
            with StageTimer(timings, "publish"):
                publish_shapefile(staged, request.output_vector)
            timings["total"] = round(time.perf_counter() - started, 6)
            report = workspace.report(
                "run",
                {
                    "tool": "segment",
                    "status": TaskStatus.SUCCEEDED.value,
                    "request_fingerprint": run_fingerprint,
                    "output": str(request.output_vector),
                    "features": features,
                    "tiles_total": len(tiles),
                    "tiles_runnable": len(runnable),
                    "tiles_skipped": len(skipped),
                    "tiles_engine_resumed": engine_resumed,
                    "tiles_postprocess_resumed": post_resumed,
                    "engine_workers": engine_workers,
                    "postprocess_workers": post_workers,
                    "timings": timings,
                    "peak_memory_mb": memory.peak_mb,
                },
            )
            workspace.mark(
                "complete",
                run_fingerprint,
                output=str(request.output_vector),
                report=str(report),
                tiles_total=len(tiles),
                tiles_processed=len(runnable),
                features=features,
                output_fingerprint=dataset_fingerprint(request.output_vector),
            )
            cleanup_warnings = _cleanup_success(workspace, tile_root, sandbox_root, footprint)
            if cleanup_warnings:
                workspace.report(
                    "run",
                    {
                        "tool": "segment",
                        "status": TaskStatus.SUCCEEDED.value,
                        "request_fingerprint": run_fingerprint,
                        "output": str(request.output_vector),
                        "features": features,
                        "tiles_total": len(tiles),
                        "tiles_runnable": len(runnable),
                        "tiles_skipped": len(skipped),
                        "tiles_engine_resumed": engine_resumed,
                        "tiles_postprocess_resumed": post_resumed,
                        "engine_workers": engine_workers,
                        "postprocess_workers": post_workers,
                        "timings": timings,
                        "peak_memory_mb": memory.peak_mb,
                        "warnings": cleanup_warnings,
                    },
                )
            context.emit("completed", "segment", "Segmentation completed", output=str(request.output_vector))
            return SegmentResult(
                TaskStatus.SUCCEEDED,
                (
                    Artifact("primary_vector", request.output_vector),
                    Artifact("run_report", report),
                ),
                cleanup_warnings,
                timings,
                memory.peak_mb,
                report,
                request.output_vector,
                len(tiles),
                len(runnable),
                max(engine_resumed, post_resumed),
            )
        except CancelledError as error:
            if "collect_engine" in locals():
                _retain_engine_checkpoints(engine_futures, collect_engine, context)
            timings["total"] = round(time.perf_counter() - started, 6)
            context.emit("cancelled", "segment", str(error))
            return _report_failure(
                request,
                workspace,
                timings,
                memory,
                TaskStatus.CANCELLED,
                str(error),
                len(tiles) if "tiles" in locals() else 0,
                engine_resumed + engine_completed if "engine_completed" in locals() else 0,
            )
        except Exception as error:
            if "collect_engine" in locals():
                _retain_engine_checkpoints(engine_futures, collect_engine, context)
            timings["total"] = round(time.perf_counter() - started, 6)
            detail = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
            context.emit("failed", "segment", f"{type(error).__name__}: {error}")
            return _report_failure(
                request,
                workspace,
                timings,
                memory,
                TaskStatus.FAILED,
                detail,
                len(tiles) if "tiles" in locals() else 0,
                engine_resumed + engine_completed if "engine_completed" in locals() else 0,
                read_error=error.diagnostic if isinstance(error, RasterReadError) else None,
            )
        finally:
            if engine_lease is not None:
                engine_lease.__exit__(None, None, None)
