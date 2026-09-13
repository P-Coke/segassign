"""Categorical pixel counting with compact class IDs and ``numpy.bincount``."""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import rasterize


@dataclass(frozen=True, slots=True)
class ClassEncoding:
    values: np.ndarray
    lookup: np.ndarray | None
    contiguous_start: int | None

    @classmethod
    def create(cls, values) -> ClassEncoding:
        normalized = np.asarray(sorted({int(value) for value in values}), dtype=np.int64)
        if not normalized.size:
            raise ValueError("valid_values must contain at least one category")
        use_lookup = normalized[0] >= 0 and normalized[-1] <= max(1_000_000, normalized.size * 4096)
        lookup = None
        if use_lookup:
            lookup = np.full(int(normalized[-1]) + 1, -1, dtype=np.int32)
            lookup[normalized] = np.arange(normalized.size, dtype=np.int32)
        contiguous_start = (
            int(normalized[0])
            if np.array_equal(normalized, np.arange(normalized[0], normalized[0] + normalized.size))
            else None
        )
        return cls(normalized, lookup, contiguous_start)

    def encode(self, pixels: np.ndarray) -> np.ndarray:
        encoded = np.full(pixels.shape, -1, dtype=np.int32)
        if self.lookup is not None:
            in_range = (pixels >= 0) & (pixels < self.lookup.size)
            encoded[in_range] = self.lookup[pixels[in_range].astype(np.int64, copy=False)]
            return encoded
        positions = np.searchsorted(self.values, pixels)
        in_range = positions < self.values.size
        matched = np.zeros(pixels.shape, dtype=bool)
        matched[in_range] = self.values[positions[in_range]] == pixels[in_range]
        encoded[matched] = positions[matched]
        return encoded


def rasterize_labels(geometries, candidates: np.ndarray, shape, transform) -> np.ndarray:
    shapes = ((geometries[int(position)], label) for label, position in enumerate(candidates, 1))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
        return rasterize(
            shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype="int32",
            all_touched=False,
        )


def bincount_window(
    pixels: np.ndarray,
    labels: np.ndarray,
    candidates: np.ndarray,
    encoding: ClassEncoding,
    nodata: int,
) -> dict[int, dict[int, int]]:
    counts = bincount_rows(pixels, labels, candidates, encoding, nodata)
    result: dict[int, dict[int, int]] = {}
    for local_position in np.flatnonzero(counts.sum(axis=1)):
        row = counts[local_position]
        result[int(candidates[local_position])] = {
            int(encoding.values[class_index]): int(row[class_index]) for class_index in np.flatnonzero(row)
        }
    return result


def bincount_rows(
    pixels: np.ndarray,
    labels: np.ndarray,
    candidates: np.ndarray,
    encoding: ClassEncoding,
    nodata: int,
) -> np.ndarray:
    """Return one dense class-count row per local candidate.

    Keeping the hot path numeric avoids converting every raster window to and
    from nested Python dictionaries. The compatibility wrapper above exists
    only for small reference tests.
    """
    class_count = int(encoding.values.size)
    empty = np.zeros((int(candidates.size), class_count), dtype=np.int64)
    if encoding.contiguous_start is not None:
        start = encoding.contiguous_start
        # One vector expression is materially cheaper than compacting pixel and
        # label arrays independently. Invalid pixels map to bin zero and are
        # discarded with the background label row.
        valid = (labels > 0) & (pixels != nodata) & (pixels >= start) & (pixels < start + class_count)
        flat = np.where(valid, labels * class_count + (pixels - start), 0).ravel()
        counts = np.bincount(flat, minlength=(int(candidates.size) + 1) * class_count).reshape(-1, class_count)
        return counts[1:]
    mask = (labels > 0) & (pixels != nodata)
    if not mask.any():
        return empty
    selected_pixels = pixels[mask]
    selected_labels = labels[mask]
    if encoding.lookup is not None:
        in_range = (selected_pixels >= 0) & (selected_pixels < encoding.lookup.size)
        if not in_range.any():
            return empty
        selected_pixels = selected_pixels[in_range]
        selected_labels = selected_labels[in_range]
        class_ids = encoding.lookup[selected_pixels.astype(np.int64, copy=False)]
    else:
        class_ids = encoding.encode(selected_pixels)
    valid = class_ids >= 0
    if not valid.any():
        return empty
    feature_labels = selected_labels[valid].astype(np.int64, copy=False)
    class_ids = class_ids[valid].astype(np.int64, copy=False)
    flat = feature_labels * class_count + class_ids
    counts = np.bincount(flat, minlength=(int(candidates.size) + 1) * class_count).reshape(-1, class_count)
    return counts[1:]


def merge_counts(target: dict[int, dict[int, int]], source: dict[int, dict[int, int]]) -> None:
    for position, values in source.items():
        row = target.setdefault(position, {})
        for value, count in values.items():
            row[value] = row.get(value, 0) + count


def statistic_row(counts: dict[int, int], ranks: int, threshold: int) -> dict[str, int]:
    result = {"class": 0}
    for rank in range(1, ranks + 1):
        result[f"v{rank}"] = 0
        result[f"v{rank}p"] = 0
    if not counts:
        return result
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    for rank, (value, count) in enumerate(ordered[:ranks], 1):
        result[f"v{rank}"] = value
        result[f"v{rank}p"] = int(round(count * 100.0 / total))
    result["class"] = result["v1"] if result["v1p"] >= threshold else 0
    return result


def statistics_frame(counts: dict[int, dict[int, int]], feature_count: int, ranks: int, threshold: int) -> pd.DataFrame:
    columns = ["class", *(name for rank in range(1, ranks + 1) for name in (f"v{rank}", f"v{rank}p"))]
    matrix = np.zeros((feature_count, len(columns)), dtype=np.int16)
    column_positions = {name: position for position, name in enumerate(columns)}
    for feature_position, value_counts in counts.items():
        for name, value in statistic_row(value_counts, ranks, threshold).items():
            matrix[feature_position, column_positions[name]] = np.int16(value)
    return pd.DataFrame(matrix, columns=columns)


def statistics_frame_dense(counts: np.ndarray, values: np.ndarray, threshold: int) -> pd.DataFrame:
    """Vectorized rank table; stable sorting preserves the smaller-value tie rule."""
    ranks = int(values.size)
    if ranks == 0:
        return pd.DataFrame(index=np.arange(counts.shape[0]))
    columns = ["class", *(name for rank in range(1, ranks + 1) for name in (f"v{rank}", f"v{rank}p"))]
    matrix = np.zeros((counts.shape[0], len(columns)), dtype=np.int16)
    if not counts.size:
        return pd.DataFrame(matrix, columns=columns)
    order = np.argsort(-counts, axis=1, kind="stable")
    ordered_counts = np.take_along_axis(counts, order, axis=1)
    totals = counts.sum(axis=1)
    for rank in range(ranks):
        present = ordered_counts[:, rank] > 0
        matrix[present, 1 + rank * 2] = values[order[present, rank]].astype(np.int16)
        percentages = np.zeros(counts.shape[0], dtype=np.int16)
        has_total = totals > 0
        percentages[has_total] = np.rint(ordered_counts[has_total, rank] * 100.0 / totals[has_total]).astype(np.int16)
        matrix[:, 2 + rank * 2] = percentages
    accepted = matrix[:, 2] >= threshold
    matrix[accepted, 0] = matrix[accepted, 1]
    return pd.DataFrame(matrix, columns=columns)
