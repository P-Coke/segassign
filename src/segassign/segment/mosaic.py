"""Deterministic supplement de-duplication and final Shapefile assembly."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from ..zonal.shapefile import write_envelope_index
from .postprocess import polygonal


def _subtract_main(
    supplements: gpd.GeoDataFrame,
    main: gpd.GeoDataFrame,
    minimum_area: float,
) -> gpd.GeoDataFrame:
    if supplements.empty or main.empty:
        return supplements
    tree = shapely.STRtree(main.geometry.array)
    source_geometries = supplements.geometry.array
    pairs = tree.query(source_geometries, predicate="intersects")
    candidates_by_position: dict[int, list[int]] = {}
    if pairs.size:
        for source_position, main_position in pairs.T:
            candidates_by_position.setdefault(int(source_position), []).append(int(main_position))
    kept_positions = []
    geometries = []
    for position, source_geometry in enumerate(source_geometries):
        geometry = source_geometry
        candidates = candidates_by_position.get(position, ())
        if candidates:
            geometry = shapely.difference(geometry, shapely.union_all(main.geometry.array[np.asarray(candidates)]))
        # A zero-distance polygon buffer collapses touching/overlapping parts
        # that GEOS can represent as a valid collection but Shapefile cannot
        # serialize as unambiguous shells and holes. It does not move ordinary
        # polygon coordinates and is limited to seam supplements.
        geometry = polygonal(shapely.buffer(geometry, 0))
        if geometry is not None:
            geometry = _drop_subpixel_parts(geometry, minimum_area)
        if geometry is not None and geometry.area >= minimum_area:
            kept_positions.append(position)
            geometries.append(geometry)
    if not kept_positions:
        return supplements.iloc[[]].copy()
    result = supplements.iloc[kept_positions].copy()
    result.geometry = geometries
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
    valid = shapely.is_valid(frame.geometry.array)
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


def assemble(tile_outputs: list[Path], destination: Path, pixel_area: float) -> int:
    frames = [pyogrio.read_dataframe(path) for path in sorted(tile_outputs)]
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
    )
    result = gpd.GeoDataFrame(pd.concat([main, supplements], ignore_index=True), crs=crs)
    result.geometry = [polygonal(geometry) for geometry in result.geometry]
    result = result.loc[result.geometry.notna() & ~result.geometry.is_empty].copy()
    result.geometry = _drop_subpixel_parts_from_multipolygons(
        result.geometry.array,
        minimum_area=pixel_area * 1e-4,
    )
    # ESRI Shapefile uses clockwise shells and counter-clockwise holes. Make
    # the convention explicit before GDAL serializes complex overlay results;
    # this changes ring order only, never coordinates or covered area.
    result.geometry = shapely.orient_polygons(result.geometry.array, exterior_cw=True)
    result = result.drop(columns=["tile_id", "source_fid", "gap_fill"], errors="ignore")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for old in destination.parent.glob(destination.stem + ".*"):
        old.unlink(missing_ok=True)
    pyogrio.write_dataframe(result, destination, driver="ESRI Shapefile", encoding="UTF-8")
    written = pyogrio.read_dataframe(destination)
    if len(written) != len(result) or written.geometry.isna().any() or written.geometry.is_empty.any():
        raise RuntimeError("final staged Shapefile failed feature/geometry validation")
    written, repaired_count = _repair_roundtrip_topology(written, minimum_area=pixel_area * 1e-4)
    if repaired_count:
        for old in destination.parent.glob(destination.stem + ".*"):
            old.unlink(missing_ok=True)
        pyogrio.write_dataframe(written, destination, driver="ESRI Shapefile", encoding="UTF-8")
        written = pyogrio.read_dataframe(destination)
    if len(written) != len(result) or not written.geometry.is_valid.all():
        raise RuntimeError("final staged Shapefile contains invalid geometries")
    write_envelope_index(destination, shapely.bounds(result.geometry.array))
    return len(result)
