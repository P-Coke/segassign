"""Block-aware raster reads and one-pass tile mask preparation."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import CRS
from rasterio import features
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.transform import Affine, array_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window
from shapely import make_valid, union_all
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, shape

from ..models import SegmentOptions
from ..runtime import RunContext
from .grid import Tile
from .reading import RasterWindowReader


class SkipTile(RuntimeError):
    pass


def resolve_crs(dataset, default: str) -> CRS:
    if dataset.crs is not None:
        return CRS.from_user_input(dataset.crs)
    return CRS.from_user_input(default)


def valid_mask(data, invalid_values: tuple[int, ...]) -> np.ndarray:
    array = np.ma.getdata(data)
    if array.ndim != 3:
        raise ValueError(f"expected (bands, rows, columns), got {array.shape}")
    result = np.ones(array.shape[1:], dtype=bool)
    if np.ma.isMaskedArray(data):
        source_mask = np.ma.getmaskarray(data)
        if source_mask.ndim == 3:
            result &= ~np.any(source_mask, axis=0)
        elif source_mask.ndim == 2:
            result &= ~source_mask
    if invalid_values:
        for value in invalid_values:
            result &= np.all(array != value, axis=0)
    return result


def clean(data, valid: np.ndarray) -> np.ndarray:
    raw = np.ma.getdata(data)
    if valid.all():
        return raw
    result = raw.copy()
    result[:, ~valid] = 0
    return result


def is_full_width_striped(dataset) -> bool:
    return bool(dataset.block_shapes) and all(
        block_height == 1 and block_width == dataset.width for block_height, block_width in dataset.block_shapes
    )


class BlockAwareReader:
    """For full-width strips, one physical read serves an entire buffered tile row."""

    def __init__(self, dataset, context: RunContext | None = None) -> None:
        self.dataset = dataset
        self.reader = RasterWindowReader(dataset, role="夏季影像", context=context)
        self.striped = is_full_width_striped(dataset)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.reader.__exit__(*exc)

    def read_row(self, tiles: list[Tile]) -> dict[str, np.ndarray]:
        if not tiles:
            return {}
        if not self.striped:
            return {
                tile.tile_id: self.reader.read(tile.buffer, tile_ids=(tile.tile_id,))
                for tile in tiles
            }
        top = min(int(tile.buffer.row_off) for tile in tiles)
        bottom = max(int(tile.buffer.row_off + tile.buffer.height) for tile in tiles)
        stripe = self.reader.read(
            Window(0, top, self.dataset.width, bottom - top),
            tile_ids=tuple(tile.tile_id for tile in tiles),
        )
        result = {}
        for tile in tiles:
            row = int(tile.buffer.row_off) - top
            column = int(tile.buffer.col_off)
            result[tile.tile_id] = stripe[
                :,
                row : row + int(tile.buffer.height),
                column : column + int(tile.buffer.width),
            ]
        return result


def _polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        parts = [_polygonal(part) for part in geometry.geoms]
        parts = [part for part in parts if part is not None and not part.is_empty]
        return union_all(parts) if parts else None
    return None


def polygonize_mask(mask: np.ndarray, transform: Affine):
    if not mask.any():
        return []
    contiguous = np.ascontiguousarray(mask, dtype=np.uint8)
    return [
        _polygonal(shape(mapping))
        for mapping, value in features.shapes(
            contiguous,
            mask=contiguous.view(bool),
            transform=transform,
        )
        if value == 1
    ]


def write_mask(path: Path, mask: np.ndarray, transform: Affine, crs: CRS) -> None:
    if mask.size and mask.all():
        geometry = box(*array_bounds(mask.shape[0], mask.shape[1], transform))
        geometries = [geometry]
    else:
        geometries = [geometry for geometry in polygonize_mask(mask, transform) if geometry is not None]
    frame = gpd.GeoDataFrame({"kind": ["valid"] * len(geometries)}, geometry=geometries, crs=crs)
    path.parent.mkdir(parents=True, exist_ok=True)
    for old in path.parent.glob(path.stem + ".*"):
        old.unlink(missing_ok=True)
    frame.to_file(path, driver="ESRI Shapefile", encoding="UTF-8", engine="pyogrio")


def reproject_valid_once(
    base_data,
    base_transform: Affine,
    base_crs: CRS,
    process_transform: Affine,
    process_crs: CRS,
    height: int,
    width: int,
    invalid_values: tuple[int, ...],
    *,
    base_valid_mask: np.ndarray | None = None,
) -> np.ndarray:
    source_mask = base_valid_mask if base_valid_mask is not None else valid_mask(base_data, invalid_values)
    source = np.ascontiguousarray(source_mask, dtype=np.uint8)
    destination = np.zeros((height, width), dtype=np.uint8)
    reproject(
        source,
        destination,
        src_transform=base_transform,
        src_crs=base_crs,
        dst_transform=process_transform,
        dst_crs=process_crs,
        src_nodata=0,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    return destination.astype(bool)


def _tile_profile(dataset, window: Window, crs: CRS, compression: str) -> dict:
    profile = dataset.profile.copy()
    profile.update(
        width=int(window.width),
        height=int(window.height),
        transform=dataset.window_transform(window),
        crs=crs,
        nodata=0,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        bigtiff="IF_SAFER",
    )
    if compression.casefold() not in {"", "none", "uncompressed"}:
        profile.update(compress=compression, predictor=2)
    else:
        profile.pop("compress", None)
    return profile


def prepare_tile(
    tile: Tile,
    tile_dir: Path,
    process_dataset,
    base_dataset,
    process_data,
    options: SegmentOptions,
    base_reader: RasterWindowReader | None = None,
) -> None:
    process_crs = resolve_crs(process_dataset, options.default_crs)
    base_crs = resolve_crs(base_dataset, options.default_crs)
    process_mask = valid_mask(process_data, options.invalid_pixel_values)
    buffer_transform = process_dataset.window_transform(tile.buffer)
    relative_row = int(tile.core.row_off - tile.buffer.row_off)
    relative_column = int(tile.core.col_off - tile.buffer.col_off)
    core_slice = (
        slice(relative_row, relative_row + int(tile.core.height)),
        slice(relative_column, relative_column + int(tile.core.width)),
    )
    if not process_mask[core_slice].any():
        raise SkipTile(f"{tile.tile_id} has no valid process pixels in its core ownership area")
    base_bounds = (
        transform_bounds(process_crs, base_crs, *tile.buffer_bounds, densify_pts=21)
        if process_crs != base_crs
        else tile.buffer_bounds
    )
    base_window = (
        rasterio.windows.from_bounds(*base_bounds, transform=base_dataset.transform).round_offsets().round_lengths()
    )
    try:
        base_window = base_window.intersection(Window(0, 0, base_dataset.width, base_dataset.height))
    except WindowError as error:
        raise SkipTile(f"{tile.tile_id} does not overlap the base raster") from error
    if base_window.width <= 0 or base_window.height <= 0:
        raise SkipTile(f"{tile.tile_id} has an empty base-raster overlap")
    if base_reader is None:
        with RasterWindowReader(base_dataset, role="冬季影像") as temporary_reader:
            base_data = temporary_reader.read(base_window, tile_ids=(tile.tile_id,))
    else:
        base_data = base_reader.read(base_window, tile_ids=(tile.tile_id,))
    base_mask = valid_mask(base_data, options.invalid_pixel_values)
    if not base_mask.any():
        raise SkipTile(f"{tile.tile_id} has no valid base pixels in its overlap window")
    buffer_base_mask = reproject_valid_once(
        base_data,
        base_dataset.window_transform(base_window),
        base_crs,
        buffer_transform,
        process_crs,
        int(tile.buffer.height),
        int(tile.buffer.width),
        options.invalid_pixel_values,
        base_valid_mask=base_mask,
    )
    core_valid = process_mask[core_slice] & buffer_base_mask[core_slice]
    if not core_valid.any():
        raise SkipTile(f"{tile.tile_id} has no valid pixels in its core ownership area")

    input_dir = tile_dir / "input"
    vector_dir = tile_dir / "vector"
    input_dir.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        input_dir / "process_tile.tif",
        "w",
        **_tile_profile(process_dataset, tile.buffer, process_crs, options.tile_compression),
    ) as output:
        output.write(clean(process_data, process_mask))
    with rasterio.open(
        input_dir / "base_tile.tif",
        "w",
        **_tile_profile(base_dataset, base_window, base_crs, options.tile_compression),
    ) as output:
        output.write(clean(base_data, base_mask))
    write_mask(vector_dir / "valid_buffer.shp", process_mask & buffer_base_mask, buffer_transform, process_crs)
    write_mask(
        vector_dir / "valid_core.shp",
        core_valid,
        process_dataset.window_transform(tile.core),
        process_crs,
    )
