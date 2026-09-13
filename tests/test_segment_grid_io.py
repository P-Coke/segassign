from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from segassign.segment.grid import build_grid
from segassign.segment.raster_io import BlockAwareReader, is_full_width_striped, reproject_valid_once


def _write(path, data, *, tiled):
    options = {}
    if tiled:
        options.update(tiled=True, blockxsize=16, blockysize=16)
    else:
        options.update(tiled=False, blockysize=1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[2],
        height=data.shape[1],
        count=data.shape[0],
        dtype=data.dtype,
        crs="EPSG:3857",
        transform=from_origin(0, data.shape[1], 1, 1),
        **options,
    ) as dataset:
        dataset.write(data)


def test_grid_has_stable_ids_buffers_and_complete_ownership():
    grid = build_grid(7000, 6500, from_origin(0, 6500, 1, 1), 3072, 256)
    assert [tile.tile_id for tile in grid[:4]] == ["r000_c000", "r000_c001", "r000_c002", "r001_c000"]
    assert sum(int(tile.core.width * tile.core.height) for tile in grid) == 7000 * 6500
    assert int(grid[0].buffer.col_off) == 0
    assert int(grid[0].buffer.row_off) == 0
    assert int(grid[0].buffer.width) == 3072 + 256
    assert int(grid[0].buffer.height) == 3072 + 256
    assert int(grid[1].buffer.col_off) == 3072 - 256


def test_striped_row_read_equals_normal_window_read(tmp_path):
    data = np.arange(2 * 40 * 48, dtype=np.uint16).reshape(2, 40, 48)
    striped = tmp_path / "striped.tif"
    tiled = tmp_path / "tiled.tif"
    _write(striped, data, tiled=False)
    _write(tiled, data, tiled=True)
    grid = build_grid(48, 40, from_origin(0, 40, 1, 1), 16, 3)
    first_row = [tile for tile in grid if tile.row == 0]
    with rasterio.open(striped) as source:
        assert is_full_width_striped(source)
        striped_arrays = BlockAwareReader(source).read_row(first_row)
    with rasterio.open(tiled) as source:
        assert not is_full_width_striped(source)
        regular_arrays = BlockAwareReader(source).read_row(first_row)
    for tile in first_row:
        np.testing.assert_array_equal(striped_arrays[tile.tile_id], regular_arrays[tile.tile_id])


def test_single_buffer_reprojection_core_slice_equals_separate_core_reprojection():
    from pyproj import CRS
    from rasterio.windows import Window

    base = np.ones((1, 18, 22), dtype=np.uint8)
    base[:, 3:6, 7:11] = 0
    base_transform = from_origin(-2, 20, 1, 1)
    buffer_transform = from_origin(0, 18, 1, 1)
    crs = CRS.from_epsg(3857)
    buffer_mask = reproject_valid_once(base, base_transform, crs, buffer_transform, crs, 14, 16, (0,))
    relative = Window(3, 2, 8, 7)
    sliced = buffer_mask[2:9, 3:11]
    separate = reproject_valid_once(
        base,
        base_transform,
        crs,
        rasterio.windows.transform(relative, buffer_transform),
        crs,
        7,
        8,
        (0,),
    )
    np.testing.assert_array_equal(sliced, separate)
