from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import (
    CompositeBatchRequest,
    CompositeResult,
    RunContext,
    SegmentBatchRequest,
    SegmentOptions,
    SegmentResult,
    TaskStatus,
    ZonalOptions,
    composite,
    segment,
)
from segassign.models import Artifact
from segassign.raster_batch import RasterMatcher, discover_rasters, validate_output_directory


def _write_raster(path: Path, *, origin=(0.0, 16.0), size=(16, 16), pixel_size=1.0, crs="EPSG:3857", value=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full((1, size[1], size[0]), value, dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=size[0],
        height=size[1],
        count=1,
        dtype="uint8",
        crs=crs,
        transform=from_origin(origin[0], origin[1], pixel_size, pixel_size),
    ) as dataset:
        dataset.write(data)
    return path


def _write_vector(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 8, 8)], crs="EPSG:3857")
    pyogrio.write_dataframe(frame, path, driver="ESRI Shapefile", encoding="UTF-8")
    return path


def test_discover_rasters_skips_hidden_work_dirs_and_respects_recursive(tmp_path):
    root = tmp_path / "rasters"
    top = _write_raster(root / "a.tif")
    nested = _write_raster(root / "sub" / "b.TIFF")
    _write_raster(root / ".geotoolbox-work" / "stale.tif")

    assert discover_rasters(root, recursive=True) == (top.resolve(), nested.resolve())
    assert discover_rasters(root, recursive=False) == (top.resolve(),)
    assert discover_rasters(top) == (top.resolve(),)


def test_raster_matcher_supports_shared_relative_stem_and_spatial_overlap(tmp_path):
    proc_dir = tmp_path / "process"
    base_dir = tmp_path / "base"
    p1 = _write_raster(proc_dir / "zone1" / "tile_a.tif", origin=(0, 16))
    p2 = _write_raster(proc_dir / "zone2" / "tile_b.tif", origin=(100, 16))
    p3 = _write_raster(proc_dir / "zone3" / "tile_c.tif", origin=(200, 16))

    # 1. Shared single file
    shared = _write_raster(tmp_path / "shared_base.tif", origin=(0, 16), size=(256, 32))
    shared_matcher = RasterMatcher(shared, recursive=True, label="2 米基础影像")
    assert shared_matcher.match(p1, proc_dir).method == "shared"
    assert shared_matcher.match(p3, proc_dir).path == shared.resolve()

    # 2. Folder with relative path, unique stem, and spatial overlap fallback
    b1 = _write_raster(base_dir / "zone1" / "tile_a.tiff", origin=(0, 16))
    b2 = _write_raster(base_dir / "other" / "tile_b.tif", origin=(100, 16))
    b3_small = _write_raster(base_dir / "region_west.tif", origin=(196, 16), size=(8, 16))
    b3_large = _write_raster(base_dir / "region_main.tif", origin=(200, 16), size=(16, 16))

    folder_matcher = RasterMatcher(base_dir, recursive=True, label="2 米基础影像")
    m1 = folder_matcher.match(p1, proc_dir)
    assert m1.path == b1.resolve() and m1.method == "relative_path"
    m2 = folder_matcher.match(p2, proc_dir)
    assert m2.path == b2.resolve() and m2.method == "stem"
    m3 = folder_matcher.match(p3, proc_dir)
    assert m3.path == b3_large.resolve()
    assert m3.method == "spatial_overlap"
    assert m3.warning is not None and b3_large.name in m3.warning
    assert b3_small.is_file()

    # 3. No match -> clear ValueError
    p_outside = _write_raster(proc_dir / "zone4" / "orphan.tif", origin=(9000, 9000))
    with pytest.raises(ValueError, match="无法为亚米级影像"):
        folder_matcher.match(p_outside, proc_dir)


def test_validate_output_directory_rejects_nested_and_shp_paths(tmp_path):
    in_dir = tmp_path / "inputs"
    in_dir.mkdir()
    with pytest.raises(ValueError, match="子目录"):
        validate_output_directory(in_dir / "out", (in_dir,))
    with pytest.raises(ValueError, match="文件夹"):
        validate_output_directory(tmp_path / "out.shp", (in_dir,))


def test_segment_batch_runs_preserves_structure_and_resumes(tmp_path, monkeypatch):
    proc_dir = tmp_path / "process"
    _write_raster(proc_dir / "a.tif", origin=(0, 16))
    _write_raster(proc_dir / "sub" / "b.tif", origin=(0, 16))
    base_file = _write_raster(tmp_path / "base.tif", origin=(0, 16))
    out_dir = tmp_path / "output"
    eco_dir = tmp_path / "eco"
    eco_dir.mkdir()

    calls = []

    def fake_plan(req):
        from segassign.models import ExecutionPlan

        return ExecutionPlan(
            "segment",
            (req.process_raster, req.base_raster),
            (req.output_vector,),
            ("segment",),
            {"tiles": 2},
        )

    def fake_run(req, context):
        calls.append(req.process_raster.name)
        _write_vector(req.output_vector)
        report = context.work_dir / f"{req.output_vector.stem}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        return SegmentResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", req.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.1},
            10.0,
            report,
            req.output_vector,
            2,
            2,
        )

    monkeypatch.setattr("segassign.segment.batch.plan", fake_plan)
    monkeypatch.setattr("segassign.segment.batch.run", fake_run)

    request = SegmentBatchRequest(
        proc_dir,
        base_file,
        out_dir,
        eco_dir,
        options=SegmentOptions(tile_size=512),
    )
    execution_plan = segment.plan_batch(request)
    assert execution_plan.metrics["items"] == 2
    assert execution_plan.metrics["tiles_total"] == 4

    result = segment.run_batch(request, RunContext(out_dir / ".geotoolbox-work", resume=True))
    assert result.status is TaskStatus.SUCCEEDED
    assert result.items_succeeded == result.items_total == 2
    assert {p.relative_to(out_dir).as_posix() for p in result.primary_vectors} == {"a.shp", "sub/b.shp"}
    assert calls == ["a.tif", "b.tif"]

    # Resume should skip re-running items
    resumed = segment.run_batch(request, RunContext(out_dir / ".geotoolbox-work", resume=True))
    assert resumed.status is TaskStatus.SUCCEEDED
    assert resumed.timings["total"] == 0.0
    assert calls == ["a.tif", "b.tif"]


def test_composite_batch_pairs_folders_caches_classification_values_and_resumes(tmp_path, monkeypatch):
    proc_dir = tmp_path / "process"
    base_dir = tmp_path / "base"
    _write_raster(proc_dir / "area1.tif", origin=(0, 16))
    _write_raster(proc_dir / "nested" / "area2.tif", origin=(100, 16))
    _write_raster(base_dir / "area1.tif", origin=(0, 16))
    _write_raster(base_dir / "area2.tif", origin=(100, 16))
    class_file = _write_raster(tmp_path / "classes.tif", origin=(0, 16), size=(128, 32), value=3)
    out_dir = tmp_path / "output"
    eco_dir = tmp_path / "eco"
    eco_dir.mkdir()

    seen_predetected = []

    def fake_plan(req):
        from segassign.models import ExecutionPlan

        return ExecutionPlan(
            "composite",
            (req.process_raster, req.base_raster, req.classification_raster),
            (req.output_vector,),
            ("segment", "zonal"),
            {"segment": {"tiles": 1}, "zonal": {}},
        )

    def fake_run(req, context, *, predetected_values=None):
        seen_predetected.append(predetected_values)
        _write_vector(req.output_vector)
        report = context.work_dir / f"{req.output_vector.stem}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        return CompositeResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", req.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.2},
            12.0,
            report,
            req.output_vector,
            None,
            None,
        )

    monkeypatch.setattr("segassign.composite.plan", fake_plan)
    monkeypatch.setattr("segassign.composite.run", fake_run)

    request = CompositeBatchRequest(
        proc_dir,
        base_dir,
        class_file,
        out_dir,
        eco_dir,
        segment_options=SegmentOptions(tile_size=512),
        zonal_options=ZonalOptions(tile_size=256),
    )
    execution_plan = composite.plan_batch(request)
    assert execution_plan.metrics["items"] == 2
    assert len(execution_plan.metrics["pairs"]) == 2

    result = composite.run_batch(request, RunContext(out_dir / ".geotoolbox-work", resume=True))
    assert result.status is TaskStatus.SUCCEEDED
    assert result.items_succeeded == 2
    assert seen_predetected == [(3,), (3,)]
    assert {p.relative_to(out_dir).as_posix() for p in result.primary_vectors} == {
        "area1.shp",
        "nested/area2.shp",
    }

    resumed = composite.run_batch(request, RunContext(out_dir / ".geotoolbox-work", resume=True))
    assert resumed.status is TaskStatus.SUCCEEDED
    assert resumed.timings["total"] == 0.0
    assert len(seen_predetected) == 2


def test_segment_and_composite_batches_execute_cross_scene_pipeline(tmp_path, monkeypatch):
    import json
    from contextlib import nullcontext

    from segassign.models import ExecutionPlan
    from segassign.segment import batch as seg_batch
    from segassign.segment import service as seg_service

    proc_dir = tmp_path / "process"
    p1 = _write_raster(proc_dir / "scene1.tif", origin=(0, 16))
    p2 = _write_raster(proc_dir / "scene2.tif", origin=(0, 16))
    base_file = _write_raster(tmp_path / "base.tif", origin=(0, 16))
    class_file = _write_raster(tmp_path / "class.tif", origin=(0, 16), value=5)
    eco_dir = tmp_path / "eco"
    eco_dir.mkdir()

    prepared_tiles: list[tuple[str, str]] = []
    real_prepare = seg_service.prepare_tile

    def tracking_prepare(tile, tile_dir, process_ds, *args):
        prepared_tiles.append((Path(process_ds.name).name, tile.tile_id))
        return real_prepare(tile, tile_dir, process_ds, *args)

    def fake_engine(tile_dir, *_args):
        output = tile_dir / "ecognition_export" / "ObjectShapes.shp"
        output.parent.mkdir(parents=True, exist_ok=True)
        pyogrio.write_dataframe(
            gpd.GeoDataFrame({"Class_name": ["ok"]}, geometry=[box(0, 0, 16, 16)], crs="EPSG:3857"),
            output,
            driver="ESRI Shapefile",
        )
        return {"export": str(output)}

    def fake_patch(_src, dst, *_args):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"ruleset")

    def fake_plan(req):
        return ExecutionPlan(
            "segment",
            (req.process_raster, req.base_raster),
            (req.output_vector,),
            ("segment",),
            {"tiles": 1},
        )

    monkeypatch.setattr(segment, "plan", fake_plan)
    monkeypatch.setattr(seg_service, "plan", fake_plan)
    monkeypatch.setattr(seg_batch, "plan", fake_plan)
    monkeypatch.setattr(seg_service, "EngineLease", lambda _ctx: nullcontext())
    monkeypatch.setattr(seg_batch, "EngineLease", lambda _ctx: nullcontext())
    monkeypatch.setattr(composite, "EngineLease", lambda _ctx: nullcontext())
    monkeypatch.setattr(seg_service, "wait_for_external_engines", lambda _ctx: None)
    monkeypatch.setattr(seg_batch, "wait_for_external_engines", lambda _ctx: None)
    monkeypatch.setattr(composite, "wait_for_external_engines", lambda _ctx: None)
    monkeypatch.setattr(seg_service, "prepare_tile", tracking_prepare)
    monkeypatch.setattr(seg_service, "run_tile", fake_engine)
    monkeypatch.setattr(seg_service, "patch", fake_patch)

    # 1. Pipelined 01 segment.run_batch
    seg_out = tmp_path / "seg_out"
    seg_req = SegmentBatchRequest(
        proc_dir,
        base_file,
        seg_out,
        eco_dir,
        options=SegmentOptions(tile_size=512, postprocess_workers=1),
    )
    seg_res = segment.run_batch(seg_req, RunContext(seg_out / ".geotoolbox-work", resume=False))
    assert seg_res.status is TaskStatus.SUCCEEDED
    assert seg_res.items_succeeded == 2
    assert json.loads(seg_res.report_path.read_text(encoding="utf-8"))["pipelined"] is True
    # Each scene's tile should be prepared exactly once (prefetch is reused, never re-prepared).
    assert sorted(prepared_tiles) == [("scene1.tif", "r000_c000"), ("scene2.tif", "r000_c000")]
    assert p1.is_file() and p2.is_file()

    # 2. Pipelined 00 composite.run_batch
    comp_out = tmp_path / "comp_out"
    comp_req = CompositeBatchRequest(
        proc_dir,
        base_file,
        class_file,
        comp_out,
        eco_dir,
        segment_options=SegmentOptions(tile_size=512, postprocess_workers=1),
        zonal_options=ZonalOptions(valid_values=(5,), tile_size=256, workers=1),
    )
    comp_res = composite.run_batch(comp_req, RunContext(comp_out / ".geotoolbox-work", resume=False))
    assert comp_res.status is TaskStatus.SUCCEEDED
    assert comp_res.items_succeeded == 2
    assert json.loads(comp_res.report_path.read_text(encoding="utf-8"))["pipelined"] is True
    for shp in comp_res.primary_vectors:
        assigned = pyogrio.read_dataframe(shp)
        assert assigned["class"].tolist() == [5]
        assert not {"tile_id", "source_fid", "gap_fill", "seam_band"}.intersection(assigned.columns)
