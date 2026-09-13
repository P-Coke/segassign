"""Choose eCognition concurrency from fixed representative full-size tiles."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

import psutil
import rasterio

from segassign import RunContext, SegmentOptions, SegmentRequest, segment
from segassign.runtime import PeakMemoryTracker
from segassign.segment.engine import run_tile, wait_for_external_engines
from segassign.segment.grid import build_grid
from segassign.segment.raster_io import BlockAwareReader, SkipTile, prepare_tile
from segassign.segment.ruleset import patch


def _prepare_representatives(request: SegmentRequest, root: Path, count: int):
    execution_plan = segment.plan(request)
    with rasterio.open(request.process_raster) as process, rasterio.open(request.base_raster) as base:
        grid = build_grid(
            process.width,
            process.height,
            process.transform,
            request.options.tile_size,
            request.options.buffer_pixels,
        )
        center_row = max(tile.row for tile in grid) // 2
        candidates = sorted(
            grid,
            key=lambda tile: (abs(tile.row - center_row), abs(tile.column - max(item.column for item in grid) // 2)),
        )
        rows: dict[int, list] = {}
        for tile in candidates:
            rows.setdefault(tile.row, []).append(tile)
        selected = []
        reader = BlockAwareReader(process)
        for row in sorted(rows, key=lambda value: abs(value - center_row)):
            row_tiles = rows[row]
            data = reader.read_row(row_tiles)
            for tile in row_tiles:
                tile_dir = root / "tiles" / tile.tile_id
                try:
                    prepare_tile(tile, tile_dir, process, base, data[tile.tile_id], request.options)
                except SkipTile:
                    continue
                patch(
                    request.ruleset,
                    tile_dir / "ruleset" / "process_tile.dcp",
                    tile_dir / "ecognition_export",
                    request.options.ruleset_mode,
                    request.invalid_ruleset,
                    request.options.invalid_class_name,
                )
                selected.append(tile_dir)
                if len(selected) == count:
                    return execution_plan, selected
    raise RuntimeError(f"only {len(selected)} valid representative tiles were found")


def _measure(tile_directories, request, root, workers):
    for tile_dir in tile_directories:
        shutil.rmtree(tile_dir / "ecognition_export", ignore_errors=True)
    started = time.perf_counter()
    context = RunContext(root / "context")
    with PeakMemoryTracker() as memory:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(run_tile, tile_dir, root / f"e-{workers}", request, context)
                for tile_dir in tile_directories
            ]
            results = [future.result() for future in as_completed(futures)]
    seconds = time.perf_counter() - started
    return {
        "workers": workers,
        "seconds": seconds,
        "tiles_per_minute": len(tile_directories) * 60 / seconds,
        "peak_memory_mb": memory.peak_mb,
        "attempts": sum(len(result["attempts"]) for result in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--ecognition-bin", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tiles", type=int, default=7)
    arguments = parser.parse_args()
    request = SegmentRequest(
        arguments.process,
        arguments.base,
        arguments.work / "unused.shp",
        arguments.ecognition_bin,
        options=SegmentOptions(tile_size=3072, buffer_pixels=256, ecognition_workers=7),
    )
    context = RunContext(arguments.work / "wait")
    wait_for_external_engines(context)
    execution_plan, tiles = _prepare_representatives(request, arguments.work, arguments.tiles)
    measurements = []
    for workers in (4, 5, 6, 7):
        tuned_request = replace(request, options=replace(request.options, ecognition_workers=workers))
        measurements.append(_measure(tiles, tuned_request, arguments.work, workers))
    memory_limit = psutil.virtual_memory().total * 0.80 / 1024**2
    eligible = [
        item for item in measurements if item["peak_memory_mb"] <= memory_limit and item["attempts"] == len(tiles)
    ]
    selected = max(eligible, key=lambda item: item["tiles_per_minute"]) if eligible else None
    result = {
        "plan": dict(execution_plan.metrics),
        "representative_tiles": [path.name for path in tiles],
        "memory_limit_mb": memory_limit,
        "measurements": measurements,
        "selected_workers": selected["workers"] if selected else None,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if selected else 1


if __name__ == "__main__":
    raise SystemExit(main())
