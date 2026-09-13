"""Semantic Shapefile equivalence report using stable business keys."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pyogrio
import shapely
from pyproj import CRS


def compare(reference_path: Path, candidate_path: Path) -> dict:
    reference = pyogrio.read_dataframe(reference_path)
    candidate = pyogrio.read_dataframe(candidate_path)
    reference_crs = CRS.from_user_input(reference.crs)
    candidate_crs = CRS.from_user_input(candidate.crs)
    tolerance = 1e-9 if reference_crs.is_geographic else 0.001
    key_columns = [name for name in ("tile_id", "source_fid", "gap_fill") if name in reference.columns]
    statistic_columns = [
        name
        for name in reference.columns
        if name == "class" or (name.startswith("v") and name[1:].replace("p", "").isdigit())
    ]
    required_columns = [*key_columns, *statistic_columns]
    reference = reference.sort_values(key_columns, kind="stable").reset_index(drop=True)
    candidate = candidate.sort_values(key_columns, kind="stable").reset_index(drop=True)
    reference_keys = reference[key_columns].apply(tuple, axis=1)
    candidate_keys = candidate[key_columns].apply(tuple, axis=1)
    reference_key_set = set(reference_keys)
    candidate_key_set = set(candidate_keys)
    missing = reference_key_set - candidate_key_set
    extra = candidate_key_set - reference_key_set

    common_keys = sorted(reference_key_set & candidate_key_set)
    reference = reference.set_index(key_columns).loc[common_keys].reset_index()
    candidate = candidate.set_index(key_columns).loc[common_keys].reset_index()
    attribute_equal = (reference[statistic_columns] == candidate[statistic_columns]).all(axis=1)
    attribute_mismatches = int((~attribute_equal).sum())

    reference_geometry = shapely.normalize(reference.geometry.array)
    candidate_geometry = shapely.normalize(candidate.geometry.array)
    exact_equal = shapely.equals_exact(reference_geometry, candidate_geometry, tolerance=tolerance)
    candidate_positions = np.flatnonzero(~exact_equal)
    topological_equal = np.zeros(len(reference_geometry), dtype=bool)
    if candidate_positions.size:
        topological_equal[candidate_positions] = shapely.equals(
            reference_geometry[candidate_positions],
            candidate_geometry[candidate_positions],
        )
    unresolved = ~(exact_equal | topological_equal)
    hausdorff_distance = np.zeros(len(reference_geometry), dtype=np.float64)
    if unresolved.any():
        hausdorff_distance[unresolved] = shapely.hausdorff_distance(
            reference_geometry[unresolved],
            candidate_geometry[unresolved],
        )
    geometry_mismatches = int((hausdorff_distance > tolerance).sum())
    reference_area = float(shapely.area(reference_geometry).sum())
    candidate_area = float(shapely.area(candidate_geometry).sum())
    symmetric_difference_area = 0.0
    if candidate_positions.size:
        symmetric_difference_area = float(
            shapely.area(
                shapely.symmetric_difference(
                    reference_geometry[candidate_positions],
                    candidate_geometry[candidate_positions],
                )
            ).sum()
        )
    area_ratio = symmetric_difference_area / reference_area if reference_area else 0.0
    total_area_relative_difference = abs(reference_area - candidate_area) / reference_area if reference_area else 0.0
    result = {
        "reference": str(reference_path.resolve()),
        "candidate": str(candidate_path.resolve()),
        "crs_equal": reference_crs == candidate_crs,
        "feature_count": {"reference": len(reference), "candidate": len(candidate)},
        "required_columns": required_columns,
        "missing_columns": sorted(set(required_columns) - set(candidate.columns)),
        "missing_keys": len(missing),
        "extra_keys": len(extra),
        "attribute_mismatches": attribute_mismatches,
        "geometry_mismatches": geometry_mismatches,
        "representation_only_geometry_differences": int((~exact_equal & topological_equal).sum()),
        "geometry_tolerance": tolerance,
        "maximum_hausdorff_distance": float(hausdorff_distance.max(initial=0.0)),
        "symmetric_difference_area_ratio": area_ratio,
        "total_area_relative_difference": total_area_relative_difference,
        "candidate_empty_geometries": int(candidate.geometry.isna().sum() + candidate.geometry.is_empty.sum()),
        "candidate_invalid_geometries": int((~candidate.geometry.is_valid).sum()),
    }
    result["passed"] = (
        result["crs_equal"]
        and len(reference) == len(candidate)
        and not result["missing_columns"]
        and not result["missing_keys"]
        and not result["extra_keys"]
        and not result["attribute_mismatches"]
        and not result["geometry_mismatches"]
        and area_ratio <= 1e-8
        and total_area_relative_difference <= 1e-8
        and not result["candidate_empty_geometries"]
        and not result["candidate_invalid_geometries"]
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = compare(arguments.reference, arguments.candidate)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
