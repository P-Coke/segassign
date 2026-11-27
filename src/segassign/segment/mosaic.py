"""Deterministic supplement de-duplication and final Shapefile assembly."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from ..zonal.shapefile import write_envelope_index
from .postprocess import clean_polygonal_array, parallel_is_valid, polygonal


def _subtract_main(
    supplements: gpd.GeoDataFrame,
    main: gpd.GeoDataFrame,
    minimum_area: float,
    workers: int | None = None,
) -> gpd.GeoDataFrame:
    if supplements.empty or main.empty:
        return supplements
    # When postprocess tagged seam-band main polygons, restrict the spatial index
    # to that ~5-10% boundary subset (mathematically exact: interior main polygons
    # cannot intersect any supplement).
    if "seam_band" in main.columns:
        candidate_main = main.loc[main["seam_band"] != 0]
    else:
        candidate_main = main
    source_geometries = np.asarray(supplements.geometry.array, dtype=object)
    if candidate_main.empty:
        main_geometries = np.empty(0, dtype=object)
        main_bounds = np.empty((0, 4), dtype=np.float64)
        candidates_by_position: dict[int, list[int]] = {}
    else:
        main_geometries = np.asarray(candidate_main.geometry.array, dtype=object)
        tree = shapely.STRtree(main_geometries)
        pairs = tree.query(source_geometries, predicate="intersects")
        candidates_by_position = {}
        if pairs.size:
            for source_position, main_position in pairs.T:
                candidates_by_position.setdefault(int(source_position), []).append(int(main_position))
        main_bounds = shapely.bounds(main_geometries)
    source_bounds = shapely.bounds(source_geometries)

    def _process_one(position: int):
        geometry = source_geometries[position]
        if geometry is None or geometry.is_empty:
            return None
        candidates = candidates_by_position.get(position)
        if not candidates:
            if not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
                geometry = polygonal(shapely.buffer(geometry, 0))
            elif geometry.geom_type == "MultiPolygon":
                geometry = _drop_subpixel_parts(geometry, minimum_area)
            return geometry if (geometry is not None and geometry.area >= minimum_area) else None
        # Subtract candidates sequentially ordered by bbox overlap area descending.
        # Avoids unioning dozens of large staircase polygons and exits immediately
        # once the supplement is fully covered.
        if len(candidates) > 1:
            sb = source_bounds[position]
            cb = main_bounds[candidates]
            overlap_w = np.maximum(0.0, np.minimum(sb[2], cb[:, 2]) - np.maximum(sb[0], cb[:, 0]))
            overlap_h = np.maximum(0.0, np.minimum(sb[3], cb[:, 3]) - np.maximum(sb[1], cb[:, 1]))
            order = np.argsort(-(overlap_w * overlap_h))
            ordered = [candidates[index] for index in order]
        else:
            ordered = candidates
        current = geometry
        for main_index in ordered:
            current = shapely.difference(current, main_geometries[main_index])
            if current is None or current.is_empty:
                return None
        if not current.is_valid or current.geom_type not in ("Polygon", "MultiPolygon"):
            current = polygonal(shapely.buffer(current, 0))
        if current is not None:
            current = _drop_subpixel_parts(current, minimum_area)
        return current if (current is not None and current.area >= minimum_area) else None

    count = len(source_geometries)
    max_workers = min(workers or min(8, os.cpu_count() or 1), count)
    if count >= 16 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_process_one, range(count)))
    else:
        results = [_process_one(position) for position in range(count)]

    kept_positions = [position for position, item in enumerate(results) if item is not None]
    if not kept_positions:
        return supplements.iloc[[]].copy()
    result = supplements.iloc[kept_positions].copy()
    result.geometry = [results[position] for position in kept_positions]
    return result


def _drop_subpixel_parts(geometry, minimum_area: float):
    parts = shapely.get_parts(geometry)
    kept = parts[shapely.area(parts) >= minimum_area]
    return polygonal(shapely.union_all(kept)) if kept.size else None


def _drop_subpixel_parts_from_multipolygons(geometries, minimum_area: float):
    """Remove distant GEOS overlay debris without touching ordinary polygons."""
    result = geometries.copy()
    multipart_positions = shapely.get_num_geometries(result) > 1
    for position in multipart_positions.nonzero()[0]:
        parts = shapely.get_parts(result[position])
        subpixel = shapely.area(parts) < minimum_area
        if subpixel.any() and (~subpixel).any():
            result[position] = _drop_subpixel_parts(result[position], minimum_area)
    return result


def _repair_roundtrip_topology(
    frame: gpd.GeoDataFrame,
    minimum_area: float = 0.0,
) -> tuple[gpd.GeoDataFrame, int]:
    valid = parallel_is_valid(np.asarray(frame.geometry.array, dtype=object))
    if valid.all():
        return frame, 0
    repaired = frame.copy()
    geometries = repaired.geometry.array.copy()
    invalid_positions = (~valid).nonzero()[0]
    for position in invalid_positions:
        geometry = polygonal(shapely.buffer(geometries[position], 0))
        if geometry is not None and minimum_area > 0:
            geometry = _drop_subpixel_parts(geometry, minimum_area)
        if geometry is None:
            raise RuntimeError(f"Shapefile topology repair removed feature {position}")
        geometries[position] = shapely.orient_polygons(geometry, exterior_cw=True)
    repaired.geometry = geometries
    return repaired, len(invalid_positions)


def _read_tiles(tile_outputs: list[Path], workers: int | None = None) -> list[gpd.GeoDataFrame]:
    ordered = sorted(tile_outputs)
    if len(ordered) <= 2:
        return [pyogrio.read_dataframe(path) for path in ordered]
    max_workers = min(workers or 8, len(ordered))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(pyogrio.read_dataframe, ordered))


def assemble(tile_outputs: list[Path], destination: Path, pixel_area: float, *, workers: int | None = None) -> int:
    frames = _read_tiles(tile_outputs, workers=workers)
    if not frames:
        raise RuntimeError("no postprocessed tile contains segmentation features")
    crs = frames[0].crs
    aligned = [frame if frame.crs == crs else frame.to_crs(crs) for frame in frames]
    merged = gpd.GeoDataFrame(pd.concat(aligned, ignore_index=True), crs=crs)
    main = merged.loc[merged["gap_fill"] == 0].copy()
    # GEOS overlay can leave sub-floating-point slivers where a supplement is
    # fully covered by main geometry. A real supplement is at least one source
    # pixel, so a tiny pixel-relative cutoff removes numerical debris without
    # removing a business feature or depending on the CRS units.
    supplements = _subtract_main(
        merged.loc[merged["gap_fill"] != 0].copy(),
        main,
        minimum_area=pixel_area * 1e-4,
        workers=workers,
    )
    result = gpd.GeoDataFrame(pd.concat([main, supplements], ignore_index=True), crs=crs)
    cleaned, keep = clean_polygonal_array(result.geometry.array, workers=workers)
    if not keep.all():
        result = result.loc[keep].copy()
        result.geometry = cleaned[keep]
    elif cleaned is not result.geometry.array:
        result.geometry = cleaned
    result.geometry = _drop_subpixel_parts_from_multipolygons(
        result.geometry.array,
        minimum_area=pixel_area * 1e-4,
    )
    # ESRI Shapefile uses clockwise shells and counter-clockwise holes. Make
    # the convention explicit before GDAL serializes complex overlay results;
    # this changes ring order only, never coordinates or covered area.
    result.geometry = shapely.orient_polygons(result.geometry.array, exterior_cw=True)
    result = result.drop(columns=["tile_id", "source_fid", "gap_fill", "seam_band"], errors="ignore")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for old in destination.parent.glob(destination.stem + ".*"):
        old.unlink(missing_ok=True)
    pyogrio.write_dataframe(result, destination, driver="ESRI Shapefile", encoding="UTF-8")
    # Geometry-only roundtrip verification avoids decoding all DBF attributes
    # and runs multi-threaded GEOS validity checking on large datasets.
    written_geom = pyogrio.read_dataframe(destination, columns=[])
    if len(written_geom) != len(result) or written_geom.geometry.isna().any() or written_geom.geometry.is_empty.any():
        raise RuntimeError("final staged Shapefile failed feature/geometry validation")
    valid = parallel_is_valid(np.asarray(written_geom.geometry.array, dtype=object), workers=workers)
    if not valid.all():
        invalid_positions = (~valid).nonzero()[0]
        repaired_geoms = np.asarray(result.geometry.array, dtype=object).copy()
        written_array = written_geom.geometry.array
        for position in invalid_positions:
            geometry = polygonal(shapely.buffer(written_array[position], 0))
            if geometry is not None and pixel_area * 1e-4 > 0:
                geometry = _drop_subpixel_parts(geometry, pixel_area * 1e-4)
            if geometry is None:
                raise RuntimeError(f"Shapefile topology repair removed feature {position}")
            repaired_geoms[position] = shapely.orient_polygons(geometry, exterior_cw=True)
        result.geometry = repaired_geoms
        for old in destination.parent.glob(destination.stem + ".*"):
            old.unlink(missing_ok=True)
        pyogrio.write_dataframe(result, destination, driver="ESRI Shapefile", encoding="UTF-8")
        info = pyogrio.read_info(destination)
        checked = pyogrio.read_dataframe(destination, columns=[], fids=invalid_positions.tolist())
        if int(info["features"]) != len(result) or not shapely.is_valid(checked.geometry.array).all():
            raise RuntimeError("final staged Shapefile contains invalid geometries")
    write_envelope_index(destination, shapely.bounds(result.geometry.array))
    return len(result)
