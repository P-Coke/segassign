"""Raw-count sidecar enabling exact assignment across disjoint regional rasters."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

STATE_VERSION = 1


def state_path(vector: Path) -> Path:
    return vector.with_suffix(".gtz.npz")


def read_count_state(vector: Path, feature_count: int) -> tuple[np.ndarray, np.ndarray] | None:
    path = state_path(vector)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as payload:
            version = int(payload["version"][0])
            values = np.asarray(payload["values"], dtype=np.int64)
            counts = np.asarray(payload["counts"], dtype=np.int64)
    except Exception as error:
        raise ValueError(f"assignment count state is unreadable: {path}: {error}") from error
    if version != STATE_VERSION:
        raise ValueError(f"unsupported assignment count state version {version}: {path}")
    if counts.shape != (feature_count, len(values)):
        raise ValueError(f"assignment count state does not match the input feature count: {path}")
    if len(values) and (np.any(values[1:] <= values[:-1]) or np.any(counts < 0)):
        raise ValueError(f"assignment count state contains invalid values: {path}")
    return values, counts


def merge_counts(
    previous: tuple[np.ndarray, np.ndarray] | None,
    values: np.ndarray,
    counts: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if previous is None:
        return values.astype(np.int64, copy=False), counts
    previous_values, previous_counts = previous
    combined_values = np.union1d(previous_values, values).astype(np.int64, copy=False)
    combined = np.zeros((counts.shape[0], len(combined_values)), dtype=np.int64)
    if len(previous_values):
        combined[:, np.searchsorted(combined_values, previous_values)] += previous_counts
    if len(values):
        combined[:, np.searchsorted(combined_values, values)] += counts
    return combined_values, combined


def write_count_state(vector: Path, values: np.ndarray, counts: np.ndarray) -> Path:
    path = state_path(vector)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        np.savez(
            stream,
            version=np.asarray([STATE_VERSION], dtype=np.int16),
            values=np.asarray(values, dtype=np.int64),
            counts=np.asarray(counts, dtype=np.int64),
        )
    os.replace(temporary, path)
    return path
