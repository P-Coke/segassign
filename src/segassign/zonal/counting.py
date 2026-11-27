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


def bincount_rows(
    pixels: np.ndarray,
    labels: np.ndarray,
    candidates: np.ndarray,
    encoding: ClassEncoding,
    nodata: int,
) -> np.ndarray:
    """Return one dense class-count row per local candidate."""
    class_count = int(encoding.values.size)
    candidate_count = int(candidates.size)
    empty = np.zeros((candidate_count, class_count), dtype=np.int64)
    if candidate_count == 0 or class_count == 0:
        return empty
    total_bins = (candidate_count + 1) * class_count
    if encoding.contiguous_start is not None:
        start = encoding.contiguous_start
        stop = start + class_count
        # Skip the nodata comparison when nodata is already outside [start, stop).
        valid = (labels > 0) & (pixels >= start) & (pixels < stop)
        if start <= nodata < stop:
            valid &= pixels != nodata
        if not valid.any():
            return empty
        # Use 32-bit bin indices when safe to halve memory bandwidth on large tiles.
        if total_bins < 2**31:
            idx_dtype = np.int32
        else:
            idx_dtype = np.int64
        selected_labels = labels[valid].astype(idx_dtype, copy=False)
        selected_classes = (pixels[valid] - start).astype(idx_dtype, copy=False)
        flat = selected_labels * idx_dtype(class_count) + selected_classes
        counts = np.bincount(flat, minlength=total_bins).reshape(-1, class_count)
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
    idx_dtype = np.int32 if total_bins < 2**31 else np.int64
    feature_labels = selected_labels[valid].astype(idx_dtype, copy=False)
    class_ids = class_ids[valid].astype(idx_dtype, copy=False)
    flat = feature_labels * idx_dtype(class_count) + class_ids
    counts = np.bincount(flat, minlength=total_bins).reshape(-1, class_count)
    return counts[1:]


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
    has_total = totals > 0
    inv_totals = np.zeros(counts.shape[0], dtype=np.float64)
    inv_totals[has_total] = 100.0 / totals[has_total]
    for rank in range(ranks):
        present = ordered_counts[:, rank] > 0
        matrix[present, 1 + rank * 2] = values[order[present, rank]].astype(np.int16)
        if has_total.any():
            matrix[has_total, 2 + rank * 2] = np.rint(
                ordered_counts[has_total, rank] * inv_totals[has_total]
            ).astype(np.int16)
    accepted = matrix[:, 2] >= threshold
    matrix[accepted, 0] = matrix[accepted, 1]
    return pd.DataFrame(matrix, columns=columns)
