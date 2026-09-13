"""Five-repeat micro gates for the two algorithmic hot paths."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np
from shapely.geometry import box

from segassign.zonal.counting import ClassEncoding, bincount_rows
from segassign.zonal.spatial import CandidateIndex


def _median(operation, repeats: int) -> tuple[float, object]:
    durations = []
    output = None
    for _ in range(repeats):
        started = time.perf_counter()
        output = operation()
        durations.append(time.perf_counter() - started)
    return statistics.median(durations), output


def _reference_unique(pixels, labels, candidates, values, nodata):
    result = np.zeros((len(candidates), len(values)), dtype=np.int64)
    lookup = np.zeros(max(values) + 1, dtype=bool)
    lookup[np.asarray(values)] = True
    mask = (labels > 0) & (pixels != nodata) & lookup[pixels]
    selected_pixels = pixels[mask].astype(np.int64)
    selected_labels = labels[mask].astype(np.int64)
    multiplier = max(values) + 1
    keys, counts = np.unique(selected_labels * multiplier + selected_pixels, return_counts=True)
    value_positions = {value: position for position, value in enumerate(values)}
    for key, count in zip(keys, counts, strict=True):
        label = int(key // multiplier)
        value = int(key % multiplier)
        result[label - 1, value_positions[value]] = int(count)
    return result


def _count_gate(repeats: int) -> dict:
    rng = np.random.default_rng(20260813)
    size = 2048
    values = tuple(range(1, 14))
    pixels = rng.integers(0, 14, size=(size, size), dtype=np.int16)
    labels = rng.integers(0, 1025, size=(size, size), dtype=np.int32)
    candidates = np.arange(1024, dtype=np.int64)
    encoding = ClassEncoding.create(values)
    old_seconds, expected = _median(lambda: _reference_unique(pixels, labels, candidates, values, 0), repeats)
    new_seconds, actual = _median(lambda: bincount_rows(pixels, labels, candidates, encoding, 0), repeats)
    if not np.array_equal(actual, expected):
        raise AssertionError("bincount result differs from the reference unique implementation")
    return {"reference_seconds": old_seconds, "v2_seconds": new_seconds, "speedup": old_seconds / new_seconds}


def _spatial_gate(repeats: int) -> dict:
    rng = np.random.default_rng(20260813)
    origins = rng.uniform(0, 10000, size=(60000, 2))
    geometries = np.asarray([box(x, y, x + 5, y + 5) for x, y in origins], dtype=object)
    bounds = np.asarray([geometry.bounds for geometry in geometries])
    queries = [tuple((x, y, x + 300, y + 300)) for x, y in rng.uniform(0, 9700, size=(200, 2))]

    def brute():
        return [
            np.flatnonzero(
                (bounds[:, 0] <= query[2])
                & (bounds[:, 2] >= query[0])
                & (bounds[:, 1] <= query[3])
                & (bounds[:, 3] >= query[1])
            )
            for query in queries
        ]

    index = CandidateIndex(geometries)
    reference_seconds, expected = _median(brute, repeats)
    v2_seconds, actual = _median(lambda: [index.query(query) for query in queries], repeats)
    for left, right in zip(expected, actual, strict=True):
        np.testing.assert_array_equal(left, right)
    return {
        "reference_seconds": reference_seconds,
        "v2_seconds": v2_seconds,
        "speedup": reference_seconds / v2_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce", action="store_true")
    arguments = parser.parse_args()
    result = {
        "repeats": arguments.repeats,
        "counting": _count_gate(arguments.repeats),
        "spatial": _spatial_gate(arguments.repeats),
    }
    result["passed"] = result["counting"]["speedup"] >= 3 and result["spatial"]["speedup"] >= 3
    text = json.dumps(result, indent=2)
    print(text)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    return 0 if result["passed"] or not arguments.enforce else 1


if __name__ == "__main__":
    raise SystemExit(main())
