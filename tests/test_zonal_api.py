from __future__ import annotations

import os

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import RunContext, TaskStatus, ZonalOptions, ZonalRequest, zonal


def _fixture(tmp_path):
    raster = tmp_path / "classes.tif"
    array = np.zeros((8, 8), dtype=np.uint8)
    array[:, :4] = 3
    array[:, 4:] = 4
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0, 8, 1, 1),
    ) as dataset:
        dataset.write(array, 1)
    vector = tmp_path / "segments.shp"
    frame = gpd.GeoDataFrame(
        {"tile_id": ["a", "b"], "source_fid": [10, 11], "gap_fill": [0, 0]},
        geometry=[box(0, 0, 4, 8), box(4, 0, 8, 8)],
        crs="EPSG:3857",
    )
    pyogrio.write_dataframe(frame, vector, driver="ESRI Shapefile", encoding="UTF-8")
    return vector, raster


def test_zonal_plan_is_read_only_and_run_preserves_order_and_fields(tmp_path):
    vector, raster = _fixture(tmp_path)
    output = tmp_path / "result" / "assigned.shp"
    request = ZonalRequest(vector, raster, output, ZonalOptions(valid_values=(3, 4), tile_size=256, workers=2))

    execution_plan = zonal.plan(request)

    assert execution_plan.metrics["input_copy_bytes"] == 0
    assert not output.parent.exists()
    result = zonal.run(request, RunContext(tmp_path / "work"))
    assert result.status is TaskStatus.SUCCEEDED
    frame = pyogrio.read_dataframe(output)
    assert not {"tile_id", "source_fid", "gap_fill"}.intersection(frame.columns)
    assert frame["class"].tolist() == [3, 4]
    assert frame["v1p"].tolist() == [100, 100]
    assert not os.path.samefile(vector, output)
    assert vector.read_bytes() == output.read_bytes()
    assert vector.with_suffix(".shx").read_bytes() == output.with_suffix(".shx").read_bytes()


def test_zonal_can_assign_disjoint_rasters_without_erasing_previous_values(tmp_path):
    vector, first_raster = _fixture(tmp_path)
    first_values = np.zeros((8, 8), dtype=np.uint8)
    first_values[:, :4] = 3
    with rasterio.open(first_raster, "r+") as dataset:
        dataset.write(first_values, 1)
    first = tmp_path / "first.shp"
    first_request = ZonalRequest(
        vector,
        first_raster,
        first,
        ZonalOptions(valid_values=(3, 4), tile_size=256, workers=2),
    )
    assert zonal.run(first_request, RunContext(tmp_path / "work-first")).status is TaskStatus.SUCCEEDED

    second_raster = tmp_path / "second.tif"
    second_values = np.zeros((8, 8), dtype=np.uint8)
    second_values[:, 4:] = 7
    with rasterio.open(
        second_raster,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype=second_values.dtype,
        nodata=0,
        crs="EPSG:3857",
        transform=from_origin(0, 8, 1, 1),
    ) as dataset:
        dataset.write(second_values, 1)
    second = tmp_path / "second.shp"
    second_request = ZonalRequest(
        first,
        second_raster,
        second,
        ZonalOptions(valid_values=(3, 4, 7), tile_size=256, workers=2),
    )

    result = zonal.run(second_request, RunContext(tmp_path / "work-second"))

    assert result.status is TaskStatus.SUCCEEDED
    assigned = pyogrio.read_dataframe(second)
    assert assigned["class"].tolist() == [3, 7]
    assert assigned["v1"].tolist() == [3, 7]
    assert assigned["v1p"].tolist() == [100, 100]


def test_zonal_accumulates_counts_for_one_feature_crossing_two_regions(tmp_path):
    vector = tmp_path / "crossing.shp"
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({"name": ["crossing"]}, geometry=[box(0, 0, 8, 8)], crs="EPSG:3857"),
        vector,
        driver="ESRI Shapefile",
    )
    area_a = tmp_path / "area-a.tif"
    area_b = tmp_path / "area-b.tif"
    values_a = np.zeros((8, 8), dtype=np.uint8)
    values_a[:, :4] = 3
    values_b = np.zeros((8, 8), dtype=np.uint8)
    values_b[:, 4:] = 7
    for path, values in ((area_a, values_a), (area_b, values_b)):
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=8,
            height=8,
            count=1,
            dtype=values.dtype,
            nodata=0,
            crs="EPSG:3857",
            transform=from_origin(0, 8, 1, 1),
        ) as dataset:
            dataset.write(values, 1)
    first = tmp_path / "first.shp"
    second = tmp_path / "second.shp"
    options = ZonalOptions(valid_values=(3, 7), tile_size=256, workers=2, class_threshold=65)

    first_result = zonal.run(ZonalRequest(vector, area_a, first, options), RunContext(tmp_path / "work-a"))
    second_result = zonal.run(ZonalRequest(first, area_b, second, options), RunContext(tmp_path / "work-b"))

    assert first_result.status is second_result.status is TaskStatus.SUCCEEDED

    assigned = pyogrio.read_dataframe(second)
    assert assigned["class"].tolist() == [0]
    assert assigned["v1"].tolist() == [3]
    assert assigned["v1p"].tolist() == [50]
    assert assigned["v2"].tolist() == [7]
    assert assigned["v2p"].tolist() == [50]
    assert second.with_suffix(".gtz.npz").is_file()


def test_first_assignment_writes_null_not_zero_when_feature_has_no_pixels(tmp_path):
    vector, _ = _fixture(tmp_path)
    raster = tmp_path / "empty.tif"
    values = np.zeros((8, 8), dtype=np.uint8)
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype=values.dtype,
        nodata=0,
        crs="EPSG:3857",
        transform=from_origin(0, 8, 1, 1),
    ) as dataset:
        dataset.write(values, 1)
    output = tmp_path / "empty-assigned.shp"

    result = zonal.run(
        ZonalRequest(vector, raster, output, ZonalOptions(valid_values=(3, 4), tile_size=256)),
        RunContext(tmp_path / "work-empty"),
    )

    assert result.status is TaskStatus.SUCCEEDED
    assigned = pyogrio.read_dataframe(output)
    assert assigned["class"].isna().all()
    assert assigned["v1"].isna().all()


def test_auto_detected_empty_raster_is_a_noop_assignment(tmp_path):
    vector, _ = _fixture(tmp_path)
    raster = tmp_path / "empty-auto.tif"
    values = np.zeros((8, 8), dtype=np.uint8)
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype=values.dtype,
        nodata=0,
        crs="EPSG:3857",
        transform=from_origin(0, 8, 1, 1),
    ) as dataset:
        dataset.write(values, 1)
    output = tmp_path / "empty-auto-assigned.shp"

    result = zonal.run(ZonalRequest(vector, raster, output), RunContext(tmp_path / "work-empty-auto"))

    assert result.status is TaskStatus.SUCCEEDED
    assigned = pyogrio.read_dataframe(output)
    assert not {"class", "v1", "v1p", "tile_id", "source_fid", "gap_fill"}.intersection(assigned.columns)


def test_cancel_does_not_publish_partial_output(tmp_path):
    vector, raster = _fixture(tmp_path)
    output = tmp_path / "cancelled.shp"
    context = RunContext(tmp_path / "work")
    context.cancellation.cancel()

    result = zonal.run(
        ZonalRequest(vector, raster, output, ZonalOptions(valid_values=(3, 4), tile_size=256)),
        context,
    )

    assert result.status is TaskStatus.CANCELLED
    assert not output.exists()
