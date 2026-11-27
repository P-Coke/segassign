"""Crash-isolated geometry ownership and clipping for one eCognition tile."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

_POLYGON_TYPE_IDS = (3, 6)  # Polygon, MultiPolygon


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry if geometry.area > 0 else None
    if isinstance(geometry, GeometryCollection):
        parts = [polygonal(part) for part in geometry.geoms]
        parts = [part for part in parts if part is not None]
        return shapely.union_all(parts) if parts else None
    return None


def parallel_is_valid(geometries: np.ndarray, workers: int | None = None) -> np.ndarray:
    """Multi-threaded GEOS validity check (shapely releases the GIL)."""
    count = len(geometries)
    max_workers = min(workers or min(8, os.cpu_count() or 1), max(1, count // 256))
    if count < 1000 or max_workers <= 1:
        return shapely.is_valid(geometries)
    chunks = np.array_split(geometries, max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return np.concatenate(list(pool.map(shapely.is_valid, chunks)))


def clean_polygonal_array(geometries, workers: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized polygonal cleanup; only invokes Python fallback on invalid/collection items."""
    array = np.asarray(geometries, dtype=object)
    count = len(array)
    if count == 0:
        return array, np.zeros(0, dtype=bool)
    present = ~shapely.is_missing(array)
    if not present.any():
        return array, np.zeros(count, dtype=bool)
    present_idx = np.flatnonzero(present)
    non_null = array[present_idx]
    non_empty = ~shapely.is_empty(non_null)
    type_ids = shapely.get_type_id(non_null)
    is_poly = (type_ids == _POLYGON_TYPE_IDS[0]) | (type_ids == _POLYGON_TYPE_IDS[1])
    fast_ok = non_empty & is_poly & parallel_is_valid(non_null, workers=workers) & (shapely.area(non_null) > 0)
    if fast_ok.all() and present.all():
        return array, np.ones(count, dtype=bool)
    result = array.copy()
    keep = np.zeros(count, dtype=bool)
    keep[present_idx[fast_ok]] = True
    for index in present_idx[~fast_ok]:
        fixed = polygonal(result[index])
        result[index] = fixed
        if fixed is not None and not fixed.is_empty:
            keep[index] = True
    return result, keep


def _clean(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.empty:
        return frame
    cleaned, keep = clean_polygonal_array(frame.geometry.array)
    if keep.all() and cleaned is frame.geometry.array:
        return frame
    result = frame.loc[keep].copy()
    result.geometry = cleaned[keep]
    return result


def _mask_frame(path: Path, target_crs):
    frame = pyogrio.read_dataframe(path)
    if frame.empty:
        return frame, None
    if target_crs is not None and frame.crs is not None and frame.crs != target_crs:
        frame = frame.to_crs(target_crs)
    mask = frame.geometry.iloc[0] if len(frame) == 1 else shapely.union_all(frame.geometry.array)
    shapely.prepare(mask)
    return frame, mask


def _intersecting_positions(geometries, mask) -> np.ndarray:
    """Return exact intersections in STRtree traversal order.

    eCognition 9 postprocessing historically used GeoPandas' spatial-index
    traversal order before clipping. That order is also the overlap priority
    later consumed by rasterize, so it is an observable part of the result.
    """
    if len(geometries) == 0:
        return np.empty(0, dtype=np.int64)
    shapely.prepare(mask)
    positions = np.asarray(shapely.STRtree(geometries).query(mask), dtype=np.int64)
    if not positions.size:
        return positions
    return positions[shapely.intersects(geometries[positions], mask)]


def _clip_to_mask(frame: gpd.GeoDataFrame, mask) -> gpd.GeoDataFrame:
    """Skip expensive GEOS overlay for polygons already covered by the mask."""
    geometries = np.asarray(frame.geometry.array, dtype=object)
    inside = shapely.covered_by(geometries, mask)
    if inside.all():
        return frame
    clipped = geometries.copy()
    clipped[~inside] = shapely.intersection(geometries[~inside], mask)
    result = frame.copy()
    result.geometry = clipped
    return _clean(result)


def _compute_seam_band(
    main_geometries,
    supplement_geometries,
    core_bounds: tuple[float, float, float, float],
) -> np.ndarray:
    """Mark main polygons that can possibly intersect any tile's supplement.

    A main polygon strictly inside its tile's core_bounds cannot intersect any
    other tile's supplement (which is clipped to that other tile's core_bounds).
    Checking both the tile's core boundary and any same-tile supplement bboxes
    is mathematically exact with zero false negatives.
    """
    count = len(main_geometries)
    if count == 0:
        return np.empty(0, dtype=np.int8)
    minx, miny, maxx, maxy = core_bounds
    main_bounds = shapely.bounds(main_geometries)
    on_seam = (
        (main_bounds[:, 0] <= minx)
        | (main_bounds[:, 2] >= maxx)
        | (main_bounds[:, 1] <= miny)
        | (main_bounds[:, 3] >= maxy)
    )
    if len(supplement_geometries):
        supp_bounds = shapely.bounds(supplement_geometries)
        supp_boxes = shapely.box(supp_bounds[:, 0], supp_bounds[:, 1], supp_bounds[:, 2], supp_bounds[:, 3])
        interior_idx = np.flatnonzero(~on_seam)
        if interior_idx.size:
            ib = main_bounds[interior_idx]
            interior_boxes = shapely.box(ib[:, 0], ib[:, 1], ib[:, 2], ib[:, 3])
            pairs = shapely.STRtree(supp_boxes).query(interior_boxes, predicate="intersects")
            if pairs.size:
                on_seam[interior_idx[np.unique(pairs[0])]] = True
    return on_seam.astype(np.int8)


def process_tile(tile_dir: Path, invalid_class_name: str, filter_invalid_class: bool) -> str:
    """Top-level/pickleable worker. Returning paths keeps large frames out of IPC."""
    metadata = json.loads((tile_dir / "tile.json").read_text(encoding="utf-8"))
    source = tile_dir / "ecognition_export" / "ObjectShapes.shp"
    core_bounds = tuple(metadata["core_bounds"])
    frame = pyogrio.read_dataframe(source, bbox=core_bounds, fid_as_index=True)
    if frame.empty:
        return ""
    if filter_invalid_class and "Class_name" in frame.columns:
        frame = frame.loc[frame["Class_name"].fillna("").astype(str) != invalid_class_name].copy()
    frame = _clean(frame)
    if frame.empty:
        return ""
    buffer_frame, buffer_mask = _mask_frame(tile_dir / "vector" / "valid_buffer.shp", frame.crs)
    if buffer_mask is None:
        return ""
    if frame.crs is None and buffer_frame.crs is not None:
        # ObjectShapes from eCognition 9 omits CRS metadata; coordinates are
        # those of the source image, so inherit rather than transform.
        frame = frame.set_crs(buffer_frame.crs, allow_override=True)
    positions = _intersecting_positions(frame.geometry.array, buffer_mask)
    frame = frame.iloc[positions].copy()
    if frame.empty:
        return ""
    frame = _clip_to_mask(frame, buffer_mask)
    if frame.empty:
        return ""
    _, core_mask = _mask_frame(tile_dir / "vector" / "valid_core.shp", frame.crs)
    if core_mask is None:
        return ""
    core_mask = shapely.intersection(core_mask, box(*core_bounds))
    shapely.prepare(core_mask)
    points = shapely.point_on_surface(frame.geometry.array)
    owned = shapely.within(points, core_mask)
    main = frame.loc[owned].copy()
    supplement = frame.loc[~owned].copy()
    if not supplement.empty:
        positions = _intersecting_positions(supplement.geometry.array, core_mask)
        supplement = supplement.iloc[positions].copy()
        if not supplement.empty:
            supplement = _clip_to_mask(supplement, core_mask)
    if main.empty and supplement.empty:
        return ""
    if not main.empty:
        main["gap_fill"] = np.int8(0)
        main["seam_band"] = _compute_seam_band(
            main.geometry.array,
            supplement.geometry.array if not supplement.empty else (),
            core_bounds,
        )
        main["source_fid"] = main.index.to_numpy(dtype="int64")
    if not supplement.empty:
        supplement["gap_fill"] = np.int8(1)
        supplement["seam_band"] = np.int8(1)
        supplement["source_fid"] = supplement.index.to_numpy(dtype="int64")
    output = gpd.GeoDataFrame(pd.concat([main, supplement], ignore_index=True), crs=frame.crs)
    output["tile_id"] = tile_dir.name
    output["strategy"] = "centroid_gapfill"
    destination = tile_dir / "postprocess" / "segments.fgb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    pyogrio.write_dataframe(output, destination, driver="FlatGeobuf", spatial_index=False)
    if int(pyogrio.read_info(destination)["features"]) != len(output):
        raise RuntimeError(f"postprocess output feature count changed for {tile_dir.name}")
    return str(destination)


def process_tile_batch(
    tile_directories: tuple[Path, ...],
    invalid_class_name: str,
    filter_invalid_class: bool,
) -> tuple[tuple[str, str, str], ...]:
    """Process a bounded batch in one worker process."""
    results: list[tuple[str, str, str]] = []
    for tile_dir in tile_directories:
        try:
            results.append((tile_dir.name, process_tile(tile_dir, invalid_class_name, filter_invalid_class), ""))
        except Exception as error:
            results.append((tile_dir.name, "", f"{type(error).__name__}: {error}"))
    return tuple(results)
