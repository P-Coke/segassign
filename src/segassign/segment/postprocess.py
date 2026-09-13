"""Crash-isolated geometry ownership and clipping for one eCognition tile."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box


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


def _clean(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    result.geometry = [polygonal(geometry) for geometry in result.geometry]
    return result.loc[result.geometry.notna() & ~result.geometry.is_empty].copy()


def _mask_frame(path: Path, target_crs):
    frame = pyogrio.read_dataframe(path)
    if frame.empty:
        return frame, None
    if target_crs is not None and frame.crs is not None and frame.crs != target_crs:
        frame = frame.to_crs(target_crs)
    return frame, shapely.union_all(frame.geometry.array)


def _intersecting_positions(geometries, mask) -> np.ndarray:
    """Return exact intersections in STRtree traversal order.

    eCognition 9 postprocessing historically used GeoPandas' spatial-index
    traversal order before clipping. That order is also the overlap priority
    later consumed by rasterize, so it is an observable part of the result.
    """
    if len(geometries) == 0:
        return np.empty(0, dtype=np.int64)
    positions = np.asarray(shapely.STRtree(geometries).query(mask), dtype=np.int64)
    if not positions.size:
        return positions
    return positions[shapely.intersects(geometries[positions], mask)]


def process_tile(tile_dir: Path, invalid_class_name: str, filter_invalid_class: bool) -> str:
    """Top-level/pickleable worker. Returning paths keeps large frames out of IPC."""
    metadata = json.loads((tile_dir / "tile.json").read_text(encoding="utf-8"))
    source = tile_dir / "ecognition_export" / "ObjectShapes.shp"
    frame = pyogrio.read_dataframe(source, bbox=tuple(metadata["core_bounds"]), fid_as_index=True)
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
    intersections = shapely.intersection(frame.geometry.array, buffer_mask)
    frame.geometry = [_geometry for _geometry in intersections]
    frame = _clean(frame)
    if frame.empty:
        return ""
    _, core_mask = _mask_frame(tile_dir / "vector" / "valid_core.shp", frame.crs)
    if core_mask is None:
        return ""
    core_mask = shapely.intersection(core_mask, box(*metadata["core_bounds"]))
    points = shapely.point_on_surface(frame.geometry.array)
    owned = shapely.within(points, core_mask)
    main = frame.loc[owned].copy()
    supplement = frame.loc[~owned].copy()
    if not supplement.empty:
        positions = _intersecting_positions(supplement.geometry.array, core_mask)
        supplement = supplement.iloc[positions].copy()
        if not supplement.empty:
            supplement.geometry = shapely.intersection(supplement.geometry.array, core_mask)
            supplement = _clean(supplement)
    if main.empty and supplement.empty:
        return ""
    if not main.empty:
        main["gap_fill"] = 0
        main["source_fid"] = main.index.to_numpy(dtype="int64")
    if not supplement.empty:
        supplement["gap_fill"] = 1
        supplement["source_fid"] = supplement.index.to_numpy(dtype="int64")
    output = gpd.GeoDataFrame(pd.concat([main, supplement], ignore_index=True), crs=frame.crs)
    output["tile_id"] = tile_dir.name
    output["strategy"] = "centroid_gapfill"
    destination = tile_dir / "postprocess" / "segments.shp"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for old in destination.parent.glob(destination.stem + ".*"):
        old.unlink(missing_ok=True)
    pyogrio.write_dataframe(output, destination, driver="ESRI Shapefile", encoding="UTF-8")
    if int(pyogrio.read_info(destination)["features"]) != len(output):
        raise RuntimeError(f"postprocess output feature count changed for {tile_dir.name}")
    return str(destination)


def process_tile_batch(
    tile_directories: tuple[Path, ...],
    invalid_class_name: str,
    filter_invalid_class: bool,
) -> tuple[tuple[str, str, str], ...]:
    """Process a bounded batch in one disposable worker process."""
    results: list[tuple[str, str, str]] = []
    for tile_dir in tile_directories:
        try:
            results.append((tile_dir.name, process_tile(tile_dir, invalid_class_name, filter_invalid_class), ""))
        except Exception as error:
            results.append((tile_dir.name, "", f"{type(error).__name__}: {error}"))
    return tuple(results)
