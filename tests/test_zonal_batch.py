from __future__ import annotations

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import RunContext, TaskStatus, ZonalBatchRequest, ZonalOptions, zonal


def _write_raster(path, values):
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


def _write_vector(path, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame(
        {"name": [f"{label}-left", f"{label}-right"]},
        geometry=[box(0, 0, 4, 8), box(4, 0, 8, 8)],
        crs="EPSG:3857",
    )
    pyogrio.write_dataframe(frame, path, driver="ESRI Shapefile", encoding="UTF-8")


def test_batch_discovers_all_shapefiles_and_preserves_relative_paths(tmp_path):
    source = tmp_path / "vectors"
    _write_vector(source / "first.shp", "first")
    _write_vector(source / "nested" / "second.SHP", "second")
    _write_vector(source / ".geotoolbox-work" / "stale.shp", "stale")
    raster = tmp_path / "area-a.tif"
    values = np.zeros((8, 8), dtype=np.uint8)
    values[:, :4] = 3
    _write_raster(raster, values)
    output = tmp_path / "area-a-output"
    request = ZonalBatchRequest(
        source,
        raster,
        output,
        ZonalOptions(valid_values=(3, 7), tile_size=256, workers=2),
    )

    execution_plan = zonal.plan_batch(request)
    result = zonal.run_batch(request, RunContext(output / ".geotoolbox-work"))

    assert execution_plan.metrics["items"] == 2
    assert result.status is TaskStatus.SUCCEEDED
    assert result.items_succeeded == result.items_total == 2
    assert {path.relative_to(output).as_posix().casefold() for path in result.primary_vectors} == {
        "first.shp",
        "nested/second.shp",
    }
    assert pyogrio.read_dataframe(output / "first.shp")["class"].tolist()[0] == 3


def test_area_a_batch_output_can_be_area_b_batch_input(tmp_path):
    source = tmp_path / "source"
    _write_vector(source / "segments.shp", "segments")
    area_a = tmp_path / "area-a.tif"
    area_b = tmp_path / "area-b.tif"
    values_a = np.zeros((8, 8), dtype=np.uint8)
    values_a[:, :4] = 3
    values_b = np.zeros((8, 8), dtype=np.uint8)
    values_b[:, 4:] = 7
    _write_raster(area_a, values_a)
    _write_raster(area_b, values_b)
    options = ZonalOptions(valid_values=(3, 7), tile_size=256, workers=2)
    output_a = tmp_path / "output-a"
    output_b = tmp_path / "output-b"

    result_a = zonal.run_batch(
        ZonalBatchRequest(source, area_a, output_a, options),
        RunContext(output_a / ".geotoolbox-work"),
    )
    result_b = zonal.run_batch(
        ZonalBatchRequest(output_a, area_b, output_b, options),
        RunContext(output_b / ".geotoolbox-work"),
    )

    assert result_a.status is result_b.status is TaskStatus.SUCCEEDED
    assigned = pyogrio.read_dataframe(output_b / "segments.shp")
    assert assigned["class"].tolist() == [3, 7]
    assert assigned["v1"].tolist() == [3, 7]
    assert assigned["v1p"].tolist() == [100, 100]
    assert not {"tile_id", "source_fid", "gap_fill"}.intersection(assigned.columns)


def test_batch_accumulates_one_feature_crossing_two_regions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({"name": ["crossing"]}, geometry=[box(0, 0, 8, 8)], crs="EPSG:3857"),
        source / "crossing.shp",
        driver="ESRI Shapefile",
        encoding="UTF-8",
    )
    area_a = tmp_path / "area-a.tif"
    area_b = tmp_path / "area-b.tif"
    values_a = np.zeros((8, 8), dtype=np.uint8)
    values_a[:, :4] = 3
    values_b = np.zeros((8, 8), dtype=np.uint8)
    values_b[:, 4:] = 7
    _write_raster(area_a, values_a)
    _write_raster(area_b, values_b)
    options = ZonalOptions(valid_values=(3, 7), tile_size=256, workers=2, class_threshold=65)
    output_a = tmp_path / "output-a"
    output_b = tmp_path / "output-b"

    first = zonal.run_batch(
        ZonalBatchRequest(source, area_a, output_a, options),
        RunContext(output_a / ".geotoolbox-work"),
    )
    second = zonal.run_batch(
        ZonalBatchRequest(output_a, area_b, output_b, options),
        RunContext(output_b / ".geotoolbox-work"),
    )

    assert first.status is second.status is TaskStatus.SUCCEEDED
    assigned = pyogrio.read_dataframe(output_b / "crossing.shp")
    assert assigned[["class", "v1", "v1p", "v2", "v2p"]].iloc[0].tolist() == [0, 3, 50, 7, 50]
    assert (output_b / "crossing.gtz.npz").is_file()


def test_batch_rejects_output_nested_inside_input_to_avoid_rediscovery(tmp_path):
    source = tmp_path / "source"
    _write_vector(source / "segments.shp", "segments")
    raster = tmp_path / "area.tif"
    _write_raster(raster, np.full((8, 8), 3, dtype=np.uint8))

    request = ZonalBatchRequest(source, raster, source / "output", ZonalOptions(valid_values=(3,), tile_size=256))

    try:
        zonal.plan_batch(request)
    except ValueError as error:
        assert "subdirectories" in str(error)
    else:
        raise AssertionError("nested output directory should be rejected")


def test_completed_batch_resumes_without_rerunning_items(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_vector(source / "segments.shp", "segments")
    raster = tmp_path / "area.tif"
    _write_raster(raster, np.full((8, 8), 3, dtype=np.uint8))
    output = tmp_path / "output"
    request = ZonalBatchRequest(
        source,
        raster,
        output,
        ZonalOptions(valid_values=(3,), tile_size=256, workers=2),
    )
    context = RunContext(output / ".geotoolbox-work", resume=True)
    first = zonal.run_batch(request, context)
    monkeypatch.setattr("segassign.zonal.batch.run", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    resumed = zonal.run_batch(request, RunContext(output / ".geotoolbox-work", resume=True))

    assert first.status is resumed.status is TaskStatus.SUCCEEDED
    assert resumed.timings["total"] == 0.0
    assert resumed.primary_vectors == (output / "segments.shp",)
