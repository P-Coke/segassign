"""Raster windows and one-time spatial candidate planning."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from affine import Affine
from rasterio.windows import Window
from rasterio.windows import bounds as window_bounds
from shapely import STRtree, box, is_empty


@dataclass(frozen=True, slots=True)
class WindowJob:
    window: Window
    candidates: np.ndarray


def intersect_bounds(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> tuple[float, float, float, float] | None:
    result = (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )
    return result if result[0] < result[2] and result[1] < result[3] else None


def raster_window_for_bounds(
    bounds: tuple[float, float, float, float], transform: Affine, width: int, height: int
) -> Window:
    minx, miny, maxx, maxy = bounds
    inverse = ~transform
    col_a, row_a = inverse * (minx, maxy)
    col_b, row_b = inverse * (maxx, miny)
    col_start = max(0, int(math.floor(min(col_a, col_b))))
    row_start = max(0, int(math.floor(min(row_a, row_b))))
    col_stop = min(width, int(math.ceil(max(col_a, col_b))))
    row_stop = min(height, int(math.ceil(max(row_a, row_b))))
    return Window(col_start, row_start, max(0, col_stop - col_start), max(0, row_stop - row_start))


def iter_windows(base: Window, tile_size: int):
    row_stop = int(base.row_off + base.height)
    col_stop = int(base.col_off + base.width)
    for row in range(int(base.row_off), row_stop, tile_size):
        for col in range(int(base.col_off), col_stop, tile_size):
            yield Window(col, row, min(tile_size, col_stop - col), min(tile_size, row_stop - row))


class CandidateIndex:
    """STRtree with stable mappings back to original feature positions."""

    def __init__(self, geometries) -> None:
        source_positions = np.asarray(
            [index for index, geometry in enumerate(geometries) if geometry is not None and not is_empty(geometry)],
            dtype=np.int64,
        )
        self._source_positions = source_positions
        self._tree = STRtree([geometries[index] for index in source_positions]) if source_positions.size else None

    def query(self, bounds: tuple[float, float, float, float]) -> np.ndarray:
        if self._tree is None:
            return np.empty(0, dtype=np.int64)
        tree_positions = np.asarray(self._tree.query(box(*bounds)), dtype=np.int64)
        if tree_positions.size == 0:
            return tree_positions
        # Sorting is intentional: rasterize's overwrite order must remain the input feature order.
        return np.sort(self._source_positions[tree_positions])


class EnvelopeIndex:
    """STRtree for already-materialized envelope boxes."""

    def __init__(self, envelopes) -> None:
        valid = np.asarray([item is not None for item in envelopes])
        self._positions = np.flatnonzero(valid)
        self._tree = STRtree(envelopes[valid]) if self._positions.size else None

    def query(self, bounds: tuple[float, float, float, float]) -> np.ndarray:
        if self._tree is None:
            return np.empty(0, dtype=np.int64)
        positions = np.asarray(self._tree.query(box(*bounds)), dtype=np.int64)
        return np.sort(self._positions[positions]) if positions.size else positions


def plan_envelope_jobs(envelopes, intersection, transform, width, height, tile_size) -> list[WindowJob]:
    index = EnvelopeIndex(envelopes)
    base = raster_window_for_bounds(intersection, transform, width, height)
    return [
        WindowJob(window, index.query(window_bounds(window, transform))) for window in iter_windows(base, tile_size)
    ]


def plan_window_jobs(
    geometries,
    intersection: tuple[float, float, float, float],
    transform: Affine,
    width: int,
    height: int,
    tile_size: int,
) -> list[WindowJob]:
    index = CandidateIndex(geometries)
    base = raster_window_for_bounds(intersection, transform, width, height)
    return [
        WindowJob(window, index.query(window_bounds(window, transform))) for window in iter_windows(base, tile_size)
    ]


def balanced_batches(jobs: list[WindowJob], workers: int) -> list[list[WindowJob]]:
    batches: list[list[WindowJob]] = [[] for _ in range(workers)]
    loads = [0] * workers
    for job in sorted(jobs, key=lambda item: item.candidates.size, reverse=True):
        target = min(range(workers), key=loads.__getitem__)
        batches[target].append(job)
        loads[target] += max(1, int(job.candidates.size))
    return [batch for batch in batches if batch]
