"""Transactional, resumable segmentation pipeline with pipelined queue scheduling."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

from ..models import Artifact, ExecutionPlan, SegmentRequest, SegmentResult, TaskStatus
from ..runtime import (
    CancelledError,
    DynamicAutoTuner,
    EngineLease,
    PeakMemoryTracker,
    ResourceScheduler,
    RunContext,
    RunWorkspace,
    StageTimer,
    adapt_tile_size,
    configure_gdal_cache,
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
from .raster_io import BlockAwareReader, SkipTile, prepare_tile, resolve_crs
from .reading import RasterReadError, RasterWindowReader
from .ruleset import patch

BRIDGE_SHA256 = "f6bf51fdc0b711ec0588cb18a5b7b7be43a19c666fcd193885073785824aeac7"
SDK_FILES = ("Engine.dll", "DataIO.dll", "DBase.dll")


@dataclass(slots=True)
class PrefetchedRow:
    run_fingerprint: str
    prepared_ids: frozenset[str]
    skipped_ids: frozenset[str]


@dataclass(slots=True)
class _StagedSegment:
    request: SegmentRequest
    context: RunContext
    workspace: RunWorkspace
    run_fingerprint: str
    started: float
    timings: dict[str, float]
    tiles: list[Tile]
    runnable: list[Tile]
    skipped: list[Tile]
    engine_resumed: int
    engine_workers: int
    post_workers: int
    raster_shape: tuple
    tile_root: Path
    sandbox_root: Path
    peak_memory_mb: float
    overlap_post_pool: ProcessPoolExecutor | None
    bg_post_futures: dict
    precomputed_post: dict[str, Path | None]


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
    metadata["items"] = 1
    metadata["items_preview"] = (
        {
            "item_id": str(request.process_raster),
            "name": request.process_raster.name,
            "relative_path": request.process_raster.name,
            "output": str(request.output_vector),
            "output_name": request.output_vector.name,
            "size_summary": (
                f"{metadata['width']}×{metadata['height']} · {metadata['bands']}波段 · {metadata['tiles']}块"
            ),
            "auxiliary": f"基础：{request.base_raster.name}",
            "match_mode": "单景直连",
            "tiles_total": int(metadata["tiles"]),
        },
    )
    return ExecutionPlan(
        "segment",
        (request.process_raster, request.base_raster, request.ruleset),
        (request.output_vector,),
        ("preflight", "prepare", "ecognition", "postprocess", "mosaic", "publish"),
        metadata,
    )


def _run_fingerprint(request: SegmentRequest) -> str:
    return stable_digest(
        {
            "request": request,
            "process": file_fingerprint(request.process_raster),
            "base": file_fingerprint(request.base_raster),
            "ruleset": file_fingerprint(request.ruleset),
            "bridge": BRIDGE_SHA256,
        }
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
            *shapefile(tile_dir / "vector" / "valid_core.shp"),
            *shapefile(tile_dir / "vector" / "valid_buffer.shp"),
        )
    if stage == "engine":
        return shapefile(tile_dir / "ecognition_export" / "ObjectShapes.shp")
    if stage == "postprocess":
        return (tile_dir / "postprocess" / "segments.fgb",)
    if stage == "support":
        return (
            tile_dir / "tile.json",
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


def _release_engine_inputs(tile_dir: Path, sandbox_root: Path) -> None:
    """Bound disk usage once a durable export checkpoint exists."""
    for directory in (tile_dir / "input", tile_dir / "ruleset", sandbox_root / tile_dir.name):
        shutil.rmtree(directory, ignore_errors=True)


def _release_postprocess_inputs(tile_dir: Path) -> None:
    """Release raw eCognition export once postprocessed .fgb is durably checkpointed."""
    shutil.rmtree(tile_dir / "ecognition_export", ignore_errors=True)


def _effective_tile_size(width: int, height: int, request: SegmentRequest, context: RunContext) -> int:
    if request.options.tile_size != 3072:
        return request.options.tile_size
    scheduler = ResourceScheduler(context.resources)
    workers = scheduler.ecognition_workers(request.options.ecognition_workers)
    return adapt_tile_size(width, height, workers, request.options.tile_size)


def _order_rows_by_valid_density(
    process_dataset,
    rows: dict[int, list[Tile]],
    invalid_values: tuple[int, ...],
) -> list[int]:
    """Run dense interior rows first and sparse diagonal edge rows last (anti-straggler LPT).

    Samples 1 strip every 128 rows (~0.8s on a 17 GB TIFF) solely for row ordering;
    never skips any tile without exact full-resolution verification.
    """
    row_indices = sorted(rows)
    if len(row_indices) < 3 or sum(len(v) for v in rows.values()) < 16:
        return row_indices
    try:
        import numpy as np
        from rasterio.windows import Window

        row_scores: dict[int, float] = {}
        invalid_arr = np.asarray(invalid_values, dtype=np.int64) if invalid_values else None
        for row_idx in row_indices:
            row_tiles = rows[row_idx]
            top = min(int(t.core.row_off) for t in row_tiles)
            bottom = max(int(t.core.row_off + t.core.height) for t in row_tiles)
            valid_hits = 0
            for r in range(top, bottom, 128):
                strip = process_dataset.read(1, window=Window(0, r, process_dataset.width, 1))[0, ::64]
                if invalid_arr is not None and invalid_arr.size:
                    mask = np.ones(strip.shape, dtype=bool)
                    for val in invalid_arr:
                        mask &= strip != val
                    valid_hits += int(mask.sum())
                else:
                    valid_hits += int(strip.size)
            row_scores[row_idx] = float(valid_hits)
        return sorted(row_indices, key=lambda idx: (-row_scores[idx], idx))
    except Exception:
        return row_indices


def _tile_valid_score(tile_data, invalid_values: tuple[int, ...]) -> int:
    import numpy as np

    band = np.ma.getdata(tile_data)[0, ::8, ::8]
    mask = np.ones(band.shape, dtype=bool)
    for val in invalid_values:
        mask &= band != val
    return int(mask.sum())


def _postprocess_checkpoint_output(
    tile: Tile,
    tile_root: Path,
    workspace: RunWorkspace,
    run_fingerprint: str,
) -> tuple[bool, Path | None]:
    if not workspace.resume:
        return False, None
    fingerprint = _stage_fingerprint(run_fingerprint, tile, "postprocess")
    checkpoint = workspace.read_checkpoint(f"{tile.tile_id}-postprocess")
    if checkpoint and checkpoint.get("fingerprint") == fingerprint:
        output = checkpoint.get("output", "")
        required = (Path(output),) if output else ()
        if workspace.checkpoint_matches(f"{tile.tile_id}-postprocess", fingerprint, required):
            return True, (Path(output) if output else None)
    return False, None


def _run_postprocess(
    tiles: list[Tile],
    tile_root: Path,
    workspace: RunWorkspace,
    run_fingerprint: str,
    request: SegmentRequest,
    workers: int,
    context: RunContext | None = None,
    precomputed: dict[str, Path | None] | None = None,
) -> tuple[list[Path], int]:
    outputs: list[Path] = []
    pending: list[Tile] = []
    resumed = 0
    already_done = precomputed or {}
    for tile in tiles:
        if tile.tile_id in already_done:
            path = already_done[tile.tile_id]
            if path is not None:
                outputs.append(path)
            continue
        hit, output_path = _postprocess_checkpoint_output(tile, tile_root, workspace, run_fingerprint)
        if hit:
            if output_path is not None:
                outputs.append(output_path)
            resumed += 1
            continue
        pending.append(tile)
    if not pending:
        return outputs, resumed

    completed_ids: set[str] = set()
    retry_tiles: list[Tile] = []
    tile_by_id = {tile.tile_id: tile for tile in pending}
    max_batch = request.options.postprocess_max_tiles_per_worker
    chunk_size = max(1, min(max_batch, math.ceil(len(pending) / max(1, workers * 2))))
    batches = [pending[index : index + chunk_size] for index in range(0, len(pending), chunk_size)]
    try:
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
                if context is not None:
                    context.checkpoint()
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
                    _release_postprocess_inputs(tile_root / tile.tile_id)
                    completed_ids.add(tile.tile_id)
                if context is not None:
                    done_count = resumed + len(already_done) + len(completed_ids)
                    context.emit(
                        "progress",
                        "postprocess",
                        f"Postprocessed {done_count}/{len(tiles)} tiles",
                        completed=done_count,
                        total=len(tiles),
                    )
    except BrokenProcessPool:
        retry_tiles.extend(tile for tile in pending if tile.tile_id not in completed_ids)

    retry_by_id = {tile.tile_id: tile for tile in retry_tiles if tile.tile_id not in completed_ids}
    for tile in retry_by_id.values():
        if context is not None:
            context.checkpoint()
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
        _release_postprocess_inputs(tile_root / tile.tile_id)
        completed_ids.add(tile.tile_id)
    return outputs, resumed


_REAL_RUN_POSTPROCESS = _run_postprocess


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
    request, workspace, timings, memory_mb, status, detail, tiles_total=0, tiles_processed=0, read_error=None,
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
            "peak_memory_mb": memory_mb,
        },
    )
    return SegmentResult(
        status,
        (Artifact("run_report", report),),
        (detail,),
        timings,
        memory_mb,
        report,
        request.output_vector,
        tiles_total,
        tiles_processed,
        0,
    )


class SharedEnginePool:
    """Shared eCognition worker pool with one global DynamicAutoTuner across overlapping batch scenes."""

    def __init__(self, context: RunContext, requested_workers: int | None = None) -> None:
        configure_gdal_cache()
        scheduler = ResourceScheduler(context.resources)
        self.initial_workers = scheduler.ecognition_workers(requested_workers)
        self.max_workers = scheduler.ecognition_max_workers(requested_workers)
        target_pct = min(95.0, max(50.0, context.resources.memory_fraction * 100.0))
        self.tuner = DynamicAutoTuner(
            initial_workers=self.initial_workers,
            min_workers=max(1, min(self.initial_workers, max(4, self.initial_workers // 2))),
            max_workers=self.max_workers,
            target_cpu_pct=target_pct,
            target_ram_pct=target_pct,
        )
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="ecognition")
        self._cond = threading.Condition()
        self._active = 0

    def __enter__(self) -> SharedEnginePool:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._pool.shutdown(wait=True)

    @property
    def active_count(self) -> int:
        with self._cond:
            return self._active

    def active_limit(self) -> int:
        return self.tuner.recommended_concurrency()

    def spare_capacity(self) -> int:
        limit = self.tuner.recommended_concurrency()
        with self._cond:
            return max(0, limit - self._active)

    def submit(self, fn: Callable, *args, context: RunContext) -> object:
        with self._cond:
            while self._active >= self.tuner.recommended_concurrency():
                context.checkpoint()
                self._cond.wait(timeout=0.25)
            self._active += 1
        try:
            future = self._pool.submit(fn, *args)
        except Exception:
            with self._cond:
                self._active = max(0, self._active - 1)
                self._cond.notify_all()
            raise

        def _release(_future) -> None:
            with self._cond:
                self._active = max(0, self._active - 1)
                self._cond.notify_all()

        future.add_done_callback(_release)
        return future


def prefetch_first_row(request: SegmentRequest, context: RunContext) -> PrefetchedRow | None:
    """Pre-prepare Row 0 of the next scene during the current scene's engine tail."""
    try:
        context.checkpoint()
        run_fingerprint = _run_fingerprint(request)
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
            return None
        tile_root = workspace.root / "tiles"
        tile_root.mkdir(exist_ok=True)
        with (
            rasterio.open(request.process_raster) as process,
            rasterio.open(request.base_raster, NUM_THREADS="ALL_CPUS") as base,
            BlockAwareReader(process, context) as reader,
            RasterWindowReader(base, role="冬季影像", context=context) as base_reader,
        ):
            tile_size = _effective_tile_size(process.width, process.height, request, context)
            tiles = build_grid(
                process.width,
                process.height,
                process.transform,
                tile_size,
                request.options.buffer_pixels,
            )
            rows_map: dict[int, list[Tile]] = {}
            for tile in tiles:
                rows_map.setdefault(tile.row, []).append(tile)
            first_row_index = _order_rows_by_valid_density(process, rows_map, request.options.invalid_pixel_values)[0]
            first_row = rows_map.get(first_row_index, [])
            if not first_row:
                return None
            needs_prepare: list[Tile] = []
            prepared_ids: set[str] = set()
            skipped_ids: set[str] = set()
            for tile in first_row:
                context.checkpoint()
                tile_dir = tile_root / tile.tile_id
                tile_dir.mkdir(parents=True, exist_ok=True)
                engine_fp = _stage_fingerprint(run_fingerprint, tile, "engine")
                if workspace.checkpoint_matches(
                    f"{tile.tile_id}-engine",
                    engine_fp,
                    (*_tile_requirements(tile_dir, "engine"), *_tile_requirements(tile_dir, "support")),
                ):
                    continue
                prep_fp = _stage_fingerprint(run_fingerprint, tile, "prepare")
                if workspace.checkpoint_matches(
                    f"{tile.tile_id}-prepare", prep_fp, _tile_requirements(tile_dir, "prepare")
                ):
                    prepared_ids.add(tile.tile_id)
                    continue
                needs_prepare.append(tile)
            if not needs_prepare:
                return PrefetchedRow(run_fingerprint, frozenset(prepared_ids), frozenset(skipped_ids))
            row_data = reader.read_row(needs_prepare)
            for tile in needs_prepare:
                context.checkpoint()
                tile_dir = tile_root / tile.tile_id
                atomic_json(tile_dir / "tile.json", tile.serializable())
                prep_fp = _stage_fingerprint(run_fingerprint, tile, "prepare")
                try:
                    prepare_tile(
                        tile, tile_dir, process, base, row_data.pop(tile.tile_id), request.options, base_reader
                    )
                    patch(
                        request.ruleset,
                        tile_dir / "ruleset" / "process_tile.dcp",
                        tile_dir / "ecognition_export",
                        request.options.ruleset_mode,
                        request.invalid_ruleset,
                        request.options.invalid_class_name,
                    )
                    _mark_tile(workspace, tile, "prepare", prep_fp, skipped=False)
                    prepared_ids.add(tile.tile_id)
                except SkipTile as error:
                    _mark_tile(workspace, tile, "prepare", prep_fp, skipped=True, reason=str(error))
                    skipped_ids.add(tile.tile_id)
            del row_data
            context.emit(
                "progress",
                "prepare",
                f"已预准备下一景首行：{request.process_raster.name}（{len(prepared_ids)} 块就绪）",
            )
            return PrefetchedRow(run_fingerprint, frozenset(prepared_ids), frozenset(skipped_ids))
    except CancelledError:
        return None
    except Exception as error:
        context.emit("warning", "prepare", f"预准备下一景首行跳过：{type(error).__name__}: {error}")
        return None


def _run_engine_stage(
    request: SegmentRequest,
    context: RunContext | None = None,
    *,
    hold_lease: bool = True,
    prefetched: PrefetchedRow | None = None,
    on_prepare_complete: Callable[[], None] | None = None,
    shared_pool: SharedEnginePool | None = None,
    on_tail_ready: Callable[[], None] | None = None,
) -> _StagedSegment | SegmentResult:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    tail_notified = False

    def notify_tail_ready() -> None:
        nonlocal tail_notified
        if not tail_notified and on_tail_ready is not None:
            tail_notified = True
            try:
                on_tail_ready()
            except Exception:
                pass

    execution_plan = plan(request)  # must precede every workspace write
    context = context or RunContext(request.output_vector.parent / ".geotoolbox-work")
    run_fingerprint = _run_fingerprint(request)
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
        notify_tail_ready()
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
    prefetched_prepared = (
        prefetched.prepared_ids if prefetched and prefetched.run_fingerprint == run_fingerprint else frozenset()
    )
    prefetched_skipped = (
        prefetched.skipped_ids if prefetched and prefetched.run_fingerprint == run_fingerprint else frozenset()
    )
    context.emit("started", "segment", "Segmentation started", plan=execution_plan.metrics)
    with PeakMemoryTracker() as memory:
        engine_lease = None
        overlap_post_pool: ProcessPoolExecutor | None = None
        prefetch_thread: threading.Thread | None = None
        staged_ready = False
        try:
            if hold_lease:
                engine_lease = EngineLease(context)
                engine_lease.__enter__()
                wait_for_external_engines(context)
            with rasterio.open(request.process_raster) as process:
                process_crs = resolve_crs(process, request.options.default_crs)
                effective_tile_size = _effective_tile_size(process.width, process.height, request, context)
                tiles = build_grid(
                    process.width,
                    process.height,
                    process.transform,
                    effective_tile_size,
                    request.options.buffer_pixels,
                )
                raster_shape = (process.width, process.height, process.transform, process_crs)
                context.emit(
                    "grid",
                    "tile-map",
                    f"已生成 {len(tiles)} 块切片网格",
                    width=process.width,
                    height=process.height,
                    crs=str(process_crs),
                    bounds=[float(v) for v in process.bounds],
                    tiles=[
                        (
                            t.tile_id,
                            int(t.core.col_off),
                            int(t.core.row_off),
                            int(t.core.width),
                            int(t.core.height),
                        )
                        for t in tiles
                    ],
                )
            configure_gdal_cache()
            scheduler = ResourceScheduler(context.resources)
            engine_workers = scheduler.ecognition_workers(request.options.ecognition_workers)
            engine_max_workers = scheduler.ecognition_max_workers(request.options.ecognition_workers)
            post_workers = scheduler.postprocess_workers(request.options.postprocess_workers)
            prepare_workers = scheduler.prepare_workers()
            target_pct = min(95.0, max(50.0, context.resources.memory_fraction * 100.0))
            tuner = DynamicAutoTuner(
                initial_workers=engine_workers,
                min_workers=max(1, min(engine_workers, max(4, engine_workers // 2))),
                max_workers=engine_max_workers,
                target_cpu_pct=target_pct,
                target_ram_pct=target_pct,
            )
            runnable: list[Tile] = []
            skipped: list[Tile] = []
            engine_resumed = 0
            engine_futures = {}
            engine_completed = 0

            # Pipeline postprocessing alongside eCognition for larger grids so
            # postprocessing finishes on spare CPU cores before the last tile ends.
            overlap_postprocess = len(tiles) > 4 and _run_postprocess is _REAL_RUN_POSTPROCESS
            bg_post_workers = scheduler.background_postprocess_workers() if overlap_postprocess else 0
            bg_post_futures: dict = {}
            precomputed_post: dict[str, Path | None] = {}
            bg_post_broken = False

            def drain_bg_post(*, block: bool = False) -> None:
                nonlocal bg_post_broken
                if not bg_post_futures:
                    return
                done = list(as_completed(bg_post_futures)) if block else [f for f in list(bg_post_futures) if f.done()]
                for future in done:
                    tile = bg_post_futures.pop(future)
                    try:
                        value = future.result()
                    except BrokenProcessPool:
                        bg_post_broken = True
                        bg_post_futures.clear()
                        return
                    except Exception:
                        continue
                    path = Path(value) if value else None
                    precomputed_post[tile.tile_id] = path
                    _mark_tile(
                        workspace,
                        tile,
                        "postprocess",
                        _stage_fingerprint(run_fingerprint, tile, "postprocess"),
                        output=str(path or ""),
                    )
                    _release_postprocess_inputs(tile_root / tile.tile_id)

            def submit_bg_post(tile: Tile) -> None:
                if not overlap_postprocess or bg_post_broken or overlap_post_pool is None:
                    return
                hit, output_path = _postprocess_checkpoint_output(tile, tile_root, workspace, run_fingerprint)
                if hit:
                    precomputed_post[tile.tile_id] = output_path
                    return
                try:
                    future = overlap_post_pool.submit(
                        process_tile,
                        tile_root / tile.tile_id,
                        request.options.invalid_class_name,
                        request.options.ruleset_mode == "invalid_classify",
                    )
                    bg_post_futures[future] = tile
                except BrokenProcessPool:
                    pass

            if overlap_postprocess:
                overlap_post_pool = ProcessPoolExecutor(max_workers=bg_post_workers)

            prepare_started = time.perf_counter()
            owned_pool: ThreadPoolExecutor | None = (
                None
                if shared_pool is not None
                else ThreadPoolExecutor(max_workers=engine_workers, thread_name_prefix="ecognition")
            )
            try:

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
                        estimated_runnable = max(len(runnable), len(tiles) - len(skipped))
                        context.emit("tile", "tile-map", "", tile_id=completed_tile.tile_id, state="done")
                        context.emit(
                            "progress",
                            "ecognition",
                            f"eCognition completed {engine_resumed + engine_completed}/{estimated_runnable} tiles",
                            completed=engine_resumed + engine_completed,
                            total=estimated_runnable,
                        )
                        submit_bg_post(completed_tile)
                    drain_bg_post(block=False)

                def submit_engine(tile: Tile) -> None:
                    context.emit("tile", "tile-map", "", tile_id=tile.tile_id, state="running")
                    if shared_pool is not None:
                        future = shared_pool.submit(
                            run_tile,
                            tile_root / tile.tile_id,
                            sandbox_root,
                            request,
                            context,
                            context=context,
                        )
                    else:
                        # Never block inside the row-prepare loop while row_data (~1.3 GB) is in RAM;
                        # owned_pool bounds concurrent execution cleanly, and between-row backpressure
                        # keeps a 1-row lookahead buffer ready so workers never starve between rows.
                        assert owned_pool is not None
                        future = owned_pool.submit(run_tile, tile_root / tile.tile_id, sandbox_root, request, context)
                    engine_futures[future] = tile
                    done = [f for f in list(engine_futures) if f.done()]
                    if done:
                        collect_engine(done)

                def apply_between_row_backpressure() -> None:
                    active_limit = (
                        shared_pool.active_limit() if shared_pool is not None else tuner.recommended_concurrency()
                    )
                    # Keep ~1.5x active_limit queued so eCognition workers never sit idle during row reads.
                    queue_ceiling = max(active_limit + 4, int(active_limit * 1.5))
                    while len(engine_futures) >= queue_ceiling:
                        context.checkpoint()
                        completed, _ = wait(engine_futures, return_when=FIRST_COMPLETED)
                        collect_engine(completed)
                        active_limit = (
                            shared_pool.active_limit() if shared_pool is not None else tuner.recommended_concurrency()
                        )
                        queue_ceiling = max(active_limit + 4, int(active_limit * 1.5))

                def _prepare_one(tile: Tile, tile_data, process_ds, base_ds, base_rdr):
                    context.checkpoint()
                    tile_dir = tile_root / tile.tile_id
                    atomic_json(tile_dir / "tile.json", tile.serializable())
                    prepare_fingerprint = _stage_fingerprint(run_fingerprint, tile, "prepare")
                    try:
                        prepare_tile(tile, tile_dir, process_ds, base_ds, tile_data, request.options, base_rdr)
                        patch(
                            request.ruleset,
                            tile_dir / "ruleset" / "process_tile.dcp",
                            tile_dir / "ecognition_export",
                            request.options.ruleset_mode,
                            request.invalid_ruleset,
                            request.options.invalid_class_name,
                        )
                        _mark_tile(workspace, tile, "prepare", prepare_fingerprint, skipped=False)
                        return tile, False, ""
                    except SkipTile as error:
                        _mark_tile(
                            workspace,
                            tile,
                            "prepare",
                            prepare_fingerprint,
                            skipped=True,
                            reason=str(error),
                        )
                        return tile, True, str(error)

                with (
                    rasterio.open(request.process_raster) as process,
                    rasterio.open(request.base_raster, NUM_THREADS="ALL_CPUS") as base,
                    BlockAwareReader(process, context) as reader,
                    RasterWindowReader(base, role="冬季影像", context=context) as base_reader,
                ):
                    rows: dict[int, list[Tile]] = {}
                    for tile in tiles:
                        rows.setdefault(tile.row, []).append(tile)
                    if len(rows) > 1:
                        context.emit(
                            "stage",
                            "scan",
                            f"正在预扫描有效区域（共 {len(tiles)} 块瓦片）",
                            total=len(tiles),
                        )
                    ordered_row_indices = _order_rows_by_valid_density(
                        process, rows, request.options.invalid_pixel_values
                    )
                    for row_index in ordered_row_indices:
                        context.checkpoint()
                        apply_between_row_backpressure()
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
                                estimated_runnable = max(len(runnable), len(tiles) - len(skipped))
                                done_engine = engine_resumed + engine_completed
                                context.emit("tile", "tile-map", "", tile_id=tile.tile_id, state="done")
                                context.emit(
                                    "progress",
                                    "ecognition",
                                    f"eCognition completed {done_engine}/{estimated_runnable} tiles",
                                    completed=done_engine,
                                    total=estimated_runnable,
                                )
                                submit_bg_post(tile)
                                continue
                            prepare_checkpoint = workspace.read_checkpoint(f"{tile.tile_id}-prepare")
                            if tile.tile_id in prefetched_skipped or (
                                context.resume
                                and prepare_checkpoint
                                and prepare_checkpoint.get("fingerprint") == prepare_fingerprint
                                and prepare_checkpoint.get("skipped")
                                and workspace.checkpoint_matches(f"{tile.tile_id}-prepare", prepare_fingerprint)
                            ):
                                skipped.append(tile)
                                context.emit("tile", "tile-map", "", tile_id=tile.tile_id, state="skipped")
                                continue
                            prep_reqs = _tile_requirements(tile_dir, "prepare")
                            prepared_ok = (
                                tile.tile_id in prefetched_prepared
                                and all(p.is_file() and p.stat().st_size > 0 for p in prep_reqs)
                            ) or workspace.checkpoint_matches(
                                f"{tile.tile_id}-prepare",
                                prepare_fingerprint,
                                prep_reqs,
                            )
                            if prepared_ok:
                                runnable.append(tile)
                                submit_engine(tile)
                            else:
                                needs_prepare.append(tile)
                        if not needs_prepare:
                            continue
                        row_data = reader.read_row(needs_prepare)
                        # Longest-Processing-Time ordering within a row: dense valid
                        # tiles start eCognition before sparse diagonal edge slivers.
                        if len(needs_prepare) > 2:
                            scores = {
                                item.tile_id: _tile_valid_score(
                                    row_data[item.tile_id], request.options.invalid_pixel_values
                                )
                                for item in needs_prepare
                            }
                            ordered_prepare = sorted(
                                needs_prepare,
                                key=lambda item: (-scores[item.tile_id], item.column),
                            )
                        else:
                            ordered_prepare = needs_prepare
                        if len(ordered_prepare) > 2:
                            with ThreadPoolExecutor(
                                max_workers=min(prepare_workers, len(ordered_prepare)),
                                thread_name_prefix="prepare",
                            ) as prep_pool:
                                prep_futures = [
                                    prep_pool.submit(
                                        _prepare_one,
                                        tile,
                                        row_data[tile.tile_id],
                                        process,
                                        base,
                                        base_reader,
                                    )
                                    for tile in ordered_prepare
                                ]
                                for prep_future in as_completed(prep_futures):
                                    tile, was_skipped, _ = prep_future.result()
                                    row_data.pop(tile.tile_id, None)
                                    if was_skipped:
                                        skipped.append(tile)
                                        context.emit("tile", "tile-map", "", tile_id=tile.tile_id, state="skipped")
                                    else:
                                        runnable.append(tile)
                                        submit_engine(tile)
                                    context.emit(
                                        "progress",
                                        "prepare",
                                        f"Prepared {len(runnable) + len(skipped)}/{len(tiles)} tiles",
                                        completed=len(runnable) + len(skipped),
                                        total=len(tiles),
                                    )
                        else:
                            for tile in ordered_prepare:
                                tile, was_skipped, _ = _prepare_one(
                                    tile, row_data.pop(tile.tile_id), process, base, base_reader
                                )
                                if was_skipped:
                                    skipped.append(tile)
                                    context.emit("tile", "tile-map", "", tile_id=tile.tile_id, state="skipped")
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
                        del row_data
                timings["prepare"] = round(time.perf_counter() - prepare_started, 6)
                # While draining the tail eCognition tiles, let batch callers
                # pre-prepare the next scene's Row 0 in parallel on idle I/O.
                if on_prepare_complete is not None and engine_futures:
                    prefetch_thread = threading.Thread(
                        target=on_prepare_complete, name="next-scene-prefetch", daemon=True
                    )
                    prefetch_thread.start()
                engine_wait_started = time.perf_counter()
                while engine_futures:
                    context.checkpoint()
                    active_limit = (
                        shared_pool.active_limit() if shared_pool is not None else tuner.recommended_concurrency()
                    )
                    tail_threshold = max(2, active_limit // 2)
                    spare = shared_pool.spare_capacity() if shared_pool is not None else 0
                    if len(engine_futures) <= tail_threshold or spare >= max(2, active_limit // 3):
                        notify_tail_ready()
                    completed, _ = wait(engine_futures, timeout=0.5, return_when=FIRST_COMPLETED)
                    if completed:
                        collect_engine(completed)
                notify_tail_ready()
                timings["engine_wait_after_prepare"] = round(time.perf_counter() - engine_wait_started, 6)
            finally:
                if owned_pool is not None:
                    owned_pool.shutdown(wait=True)
            if prefetch_thread is not None:
                prefetch_thread.join()
                prefetch_thread = None
            if engine_lease is not None:
                engine_lease.__exit__(None, None, None)
                engine_lease = None
            context.checkpoint()
            staged_ready = True
            return _StagedSegment(
                request=request,
                context=context,
                workspace=workspace,
                run_fingerprint=run_fingerprint,
                started=started,
                timings=timings,
                tiles=tiles,
                runnable=runnable,
                skipped=skipped,
                engine_resumed=engine_resumed,
                engine_workers=engine_workers,
                post_workers=post_workers,
                raster_shape=raster_shape,
                tile_root=tile_root,
                sandbox_root=sandbox_root,
                peak_memory_mb=memory.peak_mb,
                overlap_post_pool=overlap_post_pool,
                bg_post_futures=bg_post_futures,
                precomputed_post=precomputed_post,
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
                memory.peak_mb,
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
                memory.peak_mb,
                TaskStatus.FAILED,
                detail,
                len(tiles) if "tiles" in locals() else 0,
                engine_resumed + engine_completed if "engine_completed" in locals() else 0,
                read_error=error.diagnostic if isinstance(error, RasterReadError) else None,
            )
        finally:
            notify_tail_ready()
            if prefetch_thread is not None:
                prefetch_thread.join()
            if not staged_ready and overlap_post_pool is not None:
                overlap_post_pool.shutdown(wait=False, cancel_futures=True)
            if engine_lease is not None:
                engine_lease.__exit__(None, None, None)


def _finalize_segment_stage(staged: _StagedSegment, *, mosaic_workers: int | None = None) -> SegmentResult:
    request = staged.request
    context = staged.context
    workspace = staged.workspace
    timings = staged.timings
    overlap_post_pool = staged.overlap_post_pool
    with PeakMemoryTracker() as memory:
        try:
            with StageTimer(timings, "postprocess"):
                if overlap_post_pool is not None:
                    for future in list(as_completed(staged.bg_post_futures)):
                        tile = staged.bg_post_futures.pop(future)
                        try:
                            value = future.result()
                        except Exception:
                            continue
                        path = Path(value) if value else None
                        staged.precomputed_post[tile.tile_id] = path
                        _mark_tile(
                            workspace,
                            tile,
                            "postprocess",
                            _stage_fingerprint(staged.run_fingerprint, tile, "postprocess"),
                            output=str(path or ""),
                        )
                        _release_postprocess_inputs(staged.tile_root / tile.tile_id)
                    overlap_post_pool.shutdown(wait=True)
                    overlap_post_pool = None
                try:
                    outputs, post_resumed = _run_postprocess(
                        staged.runnable,
                        staged.tile_root,
                        workspace,
                        staged.run_fingerprint,
                        request,
                        mosaic_workers or staged.post_workers,
                        context,
                        precomputed=staged.precomputed_post,
                    )
                except TypeError:
                    outputs, post_resumed = _run_postprocess(
                        staged.runnable,
                        staged.tile_root,
                        workspace,
                        staged.run_fingerprint,
                        request,
                        mosaic_workers or staged.post_workers,
                    )
            context.checkpoint()
            with StageTimer(timings, "mosaic"):
                context.emit(
                    "stage",
                    "mosaic",
                    f"正在拼接 {len(outputs)} 块瓦片矢量…",
                    total=len(outputs),
                )
                staged_path = workspace.staging / request.output_vector.name
                transform = staged.raster_shape[2]
                pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
                try:
                    features = assemble(outputs, staged_path, pixel_area, workers=mosaic_workers or staged.post_workers)
                except TypeError:
                    features = assemble(outputs, staged_path, pixel_area)
            context.checkpoint()
            with StageTimer(timings, "publish"):
                context.emit(
                    "stage",
                    "publish",
                    f"正在发布矢量成果：{request.output_vector.name}",
                )
                publish_shapefile(staged_path, request.output_vector)
            timings["total"] = round(time.perf_counter() - staged.started, 6)
            peak_mb = max(staged.peak_memory_mb, memory.peak_mb)
            report_payload = {
                "tool": "segment",
                "status": TaskStatus.SUCCEEDED.value,
                "request_fingerprint": staged.run_fingerprint,
                "output": str(request.output_vector),
                "features": features,
                "tiles_total": len(staged.tiles),
                "tiles_runnable": len(staged.runnable),
                "tiles_skipped": len(staged.skipped),
                "tiles_engine_resumed": staged.engine_resumed,
                "tiles_postprocess_resumed": post_resumed,
                "tiles_postprocess_overlapped": len(staged.precomputed_post),
                "engine_workers": staged.engine_workers,
                "postprocess_workers": staged.post_workers,
                "timings": timings,
                "peak_memory_mb": peak_mb,
            }
            report = workspace.report("run", report_payload)
            workspace.mark(
                "complete",
                staged.run_fingerprint,
                output=str(request.output_vector),
                report=str(report),
                tiles_total=len(staged.tiles),
                tiles_processed=len(staged.runnable),
                features=features,
                output_fingerprint=dataset_fingerprint(request.output_vector),
            )
            cleanup_warnings = workspace.cleanup_heavy((staged.tile_root, staged.sandbox_root))
            if context.cleanup_on_success:
                report, purge_warnings = workspace.purge_after_success(report)
                cleanup_warnings = (*cleanup_warnings, *purge_warnings)
            elif cleanup_warnings:
                workspace.report("run", {**report_payload, "warnings": cleanup_warnings})
            context.emit("completed", "segment", "Segmentation completed", output=str(request.output_vector))
            return SegmentResult(
                TaskStatus.SUCCEEDED,
                (
                    Artifact("primary_vector", request.output_vector),
                    Artifact("run_report", report),
                ),
                cleanup_warnings,
                timings,
                peak_mb,
                report,
                request.output_vector,
                len(staged.tiles),
                len(staged.runnable),
                max(staged.engine_resumed, post_resumed),
            )
        except CancelledError as error:
            timings["total"] = round(time.perf_counter() - staged.started, 6)
            context.emit("cancelled", "segment", str(error))
            return _report_failure(
                request,
                workspace,
                timings,
                max(staged.peak_memory_mb, memory.peak_mb),
                TaskStatus.CANCELLED,
                str(error),
                len(staged.tiles),
                len(staged.runnable),
            )
        except Exception as error:
            timings["total"] = round(time.perf_counter() - staged.started, 6)
            detail = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
            context.emit("failed", "segment", f"{type(error).__name__}: {error}")
            return _report_failure(
                request,
                workspace,
                timings,
                max(staged.peak_memory_mb, memory.peak_mb),
                TaskStatus.FAILED,
                detail,
                len(staged.tiles),
                len(staged.runnable),
            )
        finally:
            if overlap_post_pool is not None:
                overlap_post_pool.shutdown(wait=False, cancel_futures=True)


def run(request: SegmentRequest, context: RunContext | None = None) -> SegmentResult:
    staged = _run_engine_stage(request, context, hold_lease=True)
    if isinstance(staged, SegmentResult):
        return staged
    return _finalize_segment_stage(staged)


_REAL_SEGMENT_RUN = run
