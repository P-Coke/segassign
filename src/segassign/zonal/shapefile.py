"""Fast, exact Shapefile envelope reads and geometry-preserving publication."""

from __future__ import annotations

import mmap
import os
import re
import shutil
import struct
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import CRS, Transformer

INDEX_VERSION = 1
INTERNAL_OUTPUT_FIELDS = frozenset({"tile_id", "source_fid", "gap_fill"})
_ASSIGNMENT_FIELD = re.compile(r"^(?:class|v\d+p?)$", re.IGNORECASE)


def merge_assignment_attributes(attributes, statistics, update_mask: np.ndarray):
    """Apply this raster's statistics without erasing assignments outside it."""
    if len(attributes) != len(statistics) or len(attributes) != len(update_mask):
        raise ValueError("attribute, statistic, and update-mask row counts must match")
    result = attributes.drop(
        columns=[column for column in attributes.columns if column.casefold() in INTERNAL_OUTPUT_FIELDS],
        errors="ignore",
    ).copy()
    update_mask = np.asarray(update_mask, dtype=bool)
    update_positions = np.flatnonzero(update_mask)
    generated = {column.casefold() for column in statistics.columns}
    for column in result.columns:
        if _ASSIGNMENT_FIELD.fullmatch(str(column)) and column.casefold() not in generated and update_positions.size:
            result.iloc[update_positions, result.columns.get_loc(column)] = 0
    for column in statistics.columns:
        values = statistics[column].to_numpy()
        existing = next((name for name in result.columns if name.casefold() == column.casefold()), None)
        if existing is None:
            series = pd.Series(pd.array([pd.NA] * len(result), dtype="Int32"), index=result.index)
            if update_positions.size:
                series.iloc[update_positions] = values[update_positions]
            result[column] = series
        elif update_positions.size:
            result.iloc[update_positions, result.columns.get_loc(existing)] = values[update_positions]
    return result


def read_polygon_bounds(path: Path) -> np.ndarray:
    """Read record envelopes through SHX offsets without decoding polygons."""
    index_path = path.with_suffix(".shx")
    if not index_path.is_file():
        raise ValueError(f"Shapefile index is missing: {index_path}")
    index_size = index_path.stat().st_size
    if index_size < 100 or (index_size - 100) % 8:
        raise ValueError(f"invalid SHX length: {index_path}")
    feature_count = (index_size - 100) // 8
    bounds = np.full((feature_count, 4), np.nan, dtype=np.float64)
    with index_path.open("rb") as index_stream, path.open("rb") as shape_stream:
        index_stream.seek(100)
        offsets = np.frombuffer(index_stream.read(), dtype=">i4").reshape(-1, 2)[:, 0].astype(np.int64) * 2
        with mmap.mmap(shape_stream.fileno(), 0, access=mmap.ACCESS_READ) as shape_data:
            for position, offset in enumerate(offsets):
                shape_type = struct.unpack_from("<i", shape_data, int(offset) + 8)[0]
                if shape_type in {3, 5, 8, 13, 15, 18, 23, 25, 28, 31}:
                    bounds[position] = struct.unpack_from("<4d", shape_data, int(offset) + 12)
                elif shape_type != 0:
                    raise ValueError(f"unsupported Shapefile shape type {shape_type} at feature {position}")
    return bounds


def transform_envelopes(bounds: np.ndarray, source_crs: CRS, target_crs: CRS) -> np.ndarray:
    if source_crs == target_crs:
        return bounds
    valid = ~np.isnan(bounds).any(axis=1)
    result = bounds.copy()
    selected = bounds[valid]
    minimum_x, minimum_y, maximum_x, maximum_y = selected.T
    middle_x = (minimum_x + maximum_x) / 2
    middle_y = (minimum_y + maximum_y) / 2
    x = np.stack((minimum_x, maximum_x, minimum_x, maximum_x, middle_x, middle_x, minimum_x, maximum_x))
    y = np.stack((minimum_y, minimum_y, maximum_y, maximum_y, minimum_y, maximum_y, middle_y, middle_y))
    transformed_x, transformed_y = Transformer.from_crs(source_crs, target_crs, always_xy=True).transform(x, y)
    # One centimetre expansion is conservative relative to the sub-millimetre
    # geometry equivalence tolerance and prevents projection-curvature misses.
    epsilon = 0.01 if not target_crs.is_geographic else 1e-10
    result[valid, 0] = np.min(transformed_x, axis=0) - epsilon
    result[valid, 1] = np.min(transformed_y, axis=0) - epsilon
    result[valid, 2] = np.max(transformed_x, axis=0) + epsilon
    result[valid, 3] = np.max(transformed_y, axis=0) + epsilon
    return result


def envelope_geometries(bounds: np.ndarray):
    valid = ~np.isnan(bounds).any(axis=1)
    geometries = np.full(bounds.shape[0], None, dtype=object)
    geometries[valid] = shapely.box(bounds[valid, 0], bounds[valid, 1], bounds[valid, 2], bounds[valid, 3])
    return geometries


def write_envelope_index(path: Path, bounds: np.ndarray) -> Path:
    shape = path.stat()
    index_path = path.with_suffix(".gtbidx")
    temporary = index_path.with_suffix(".gtbidx.tmp")
    with temporary.open("wb") as stream:
        np.savez(
            stream,
            version=np.asarray([INDEX_VERSION], dtype=np.int16),
            shape_size=np.asarray([shape.st_size], dtype=np.int64),
            shape_mtime_ns=np.asarray([shape.st_mtime_ns], dtype=np.int64),
            bounds=np.asarray(bounds, dtype=np.float64),
        )
    os.replace(temporary, index_path)
    return index_path


def read_envelope_index(path: Path) -> np.ndarray | None:
    index_path = path.with_suffix(".gtbidx")
    try:
        shape = path.stat()
        with np.load(index_path, allow_pickle=False) as archive:
            if int(archive["version"][0]) != INDEX_VERSION:
                return None
            if int(archive["shape_size"][0]) != shape.st_size:
                return None
            if int(archive["shape_mtime_ns"][0]) != shape.st_mtime_ns:
                return None
            return np.asarray(archive["bounds"], dtype=np.float64)
    except (OSError, KeyError, ValueError):
        return None


def _copy(source: Path, destination: Path, allow_link: bool = False) -> None:
    destination.unlink(missing_ok=True)
    if allow_link:
        try:
            os.link(source, destination)
            return
        except OSError:
            pass
    shutil.copy2(source, destination)


def write_attributes_with_source_geometry(
    source: Path,
    statistics,
    update_mask: np.ndarray,
    staged: Path,
    encoding: str,
) -> int:
    """Write a new DBF while preserving source SHP/SHX bytes and record order."""
    attributes = pyogrio.read_dataframe(source, read_geometry=False, fid_as_index=True)
    attributes = merge_assignment_attributes(attributes, statistics, update_mask)
    null_geometry = gpd.GeoSeries(np.full(len(attributes), None, dtype=object), crs=pyogrio.read_info(source)["crs"])
    table = gpd.GeoDataFrame(attributes.reset_index(drop=True), geometry=null_geometry)
    scratch = staged.parent / (staged.stem + "-attributes.shp")
    for old in staged.parent.glob(scratch.stem + ".*"):
        old.unlink(missing_ok=True)
    pyogrio.write_dataframe(table, scratch, driver="ESRI Shapefile", encoding=encoding)
    staged.parent.mkdir(parents=True, exist_ok=True)
    for old in staged.parent.glob(staged.stem + ".*"):
        old.unlink(missing_ok=True)
    internal_source = source.parent.name == ".geotoolbox-segment"
    _copy(source, staged, allow_link=internal_source)
    _copy(source.with_suffix(".shx"), staged.with_suffix(".shx"), allow_link=internal_source)
    shutil.move(scratch.with_suffix(".dbf"), staged.with_suffix(".dbf"))
    for suffix in (".prj", ".cpg"):
        source_sidecar = source.with_suffix(suffix)
        generated_sidecar = scratch.with_suffix(suffix)
        if source_sidecar.is_file():
            shutil.copy2(source_sidecar, staged.with_suffix(suffix))
        elif generated_sidecar.is_file():
            shutil.move(generated_sidecar, staged.with_suffix(suffix))
    source_index = source.with_suffix(".gtbidx")
    if source_index.is_file():
        shutil.copy2(source_index, staged.with_suffix(".gtbidx"))
    for old in staged.parent.glob(scratch.stem + ".*"):
        old.unlink(missing_ok=True)
    info = pyogrio.read_info(staged)
    if int(info["features"]) != len(attributes):
        raise RuntimeError("fast Shapefile staging changed feature count")
    return len(attributes)
