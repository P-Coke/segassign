from __future__ import annotations

from concurrent.futures import Future
from contextlib import nullcontext

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import ExecutionPlan, SegmentOptions, SegmentRequest, TaskStatus
from segassign.runtime import RunContext, RunWorkspace
from segassign.runtime.workspace import dataset_fingerprint
from segassign.segment.grid import build_grid
from segassign.segment.reading import RasterReadError
from segassign.segment.service import _run_postprocess, _stage_fingerprint


def _request(tmp_path):
    placeholder = tmp_path / "placeholder"
    return SegmentRequest(
        placeholder.with_suffix(".tif"),
        placeholder.with_name("base.tif"),
        tmp_path / "out.shp",
        tmp_path,
        placeholder.with_suffix(".dcp"),
        placeholder.with_name("invalid.dcp"),
        placeholder.with_suffix(".exe"),
        SegmentOptions(tile_size=512, postprocess_workers=1),
    )


def test_postprocess_worker_failure_retries_only_failed_tile(tmp_path, monkeypatch):
    from segassign.segment import service

    tiles = build_grid(900, 500, from_origin(0, 500, 1, 1), 512, 16)
    workspace = RunWorkspace(tmp_path / "work", "segment", "run", resume=False)
    workspace.ensure()
    tile_root = workspace.root / "tiles"
    calls: dict[str, int] = {}

    def flaky(tile_dir, invalid_class_name, filter_invalid_class):
        calls[tile_dir.name] = calls.get(tile_dir.name, 0) + 1
        if tile_dir.name == tiles[0].tile_id and calls[tile_dir.name] == 1:
            raise ValueError("injected geometry failure")
        return ""

    class InlinePool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def submit(self, function, *arguments):
            future = Future()
            try:
                future.set_result(function(*arguments))
            except Exception as error:
                future.set_exception(error)
            return future

    monkeypatch.setattr(service, "process_tile", flaky)
    monkeypatch.setattr(
        service,
        "process_tile_batch",
        lambda directories, name, should_filter: tuple(
            (directory.name, flaky(directory, name, should_filter), "") for directory in directories
        ),
    )
    monkeypatch.setattr(service, "ProcessPoolExecutor", InlinePool)
    _, resumed = _run_postprocess(tiles, tile_root, workspace, "run", _request(tmp_path), 1)

    assert resumed == 0
    assert calls[tiles[0].tile_id] == 2
    assert calls[tiles[1].tile_id] == 1
    for tile in tiles:
        checkpoint = workspace.read_checkpoint(f"{tile.tile_id}-postprocess")
        assert checkpoint["fingerprint"] == _stage_fingerprint("run", tile, "postprocess")


def test_prepare_read_failure_checkpoints_pending_engines_and_resumes(tmp_path, monkeypatch):
    import json

    from segassign.segment import service

    request = _request(tmp_path)
    for path in (request.process_raster, request.base_raster):
        with rasterio.open(
            path, "w", driver="GTiff", width=900, height=500, count=1, dtype="uint8",
            crs="EPSG:3857", transform=from_origin(0, 500, 1, 1), tiled=True, blockxsize=256, blockysize=256,
        ) as dataset:
            dataset.write(np.ones((1, 500, 900), dtype=np.uint8))
    for path in (request.ruleset, request.invalid_ruleset, request.bridge):
        path.write_bytes(b"test external adapter")
    unchanged = dataset_fingerprint(request.process_raster)
    prepare_calls = []
    engine_calls = []
    should_fail = True
    real_prepare = service.prepare_tile

    def prepare(tile, *args):
        prepare_calls.append(tile.tile_id)
        if tile.column == 1 and should_fail:
            raise RasterReadError({
                "source": str(request.base_raster), "role": "冬季影像", "tile_ids": [tile.tile_id],
                "window": {"col_off": 256, "row_off": 0, "width": 644, "height": 500},
                "attempts": 3, "errors": ["injected TIFFReadEncodedStrip failure"],
            })
        return real_prepare(tile, *args)

    def engine(tile_dir, *_args):
        engine_calls.append(tile_dir.name)
        output = tile_dir / "ecognition_export" / "ObjectShapes.shp"
        output.parent.mkdir(parents=True)
        for suffix in (".shp", ".shx", ".dbf"):
            output.with_suffix(suffix).write_bytes(b"test export")
        return {"export": str(output)}

    def patch(_source, destination, *_args):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"test ruleset")

    def assemble(_inputs, destination, _pixel_area):
        pyogrio.write_dataframe(
            gpd.GeoDataFrame({"test": [1]}, geometry=[box(0, 0, 900, 500)], crs="EPSG:3857"),
            destination, driver="ESRI Shapefile",
        )
        return 1

    monkeypatch.setattr(service, "plan", lambda _request: ExecutionPlan("segment", (), (), (), {}))
    monkeypatch.setattr(service, "EngineLease", lambda _context: nullcontext())
    monkeypatch.setattr(service, "wait_for_external_engines", lambda _context: None)
    monkeypatch.setattr(service, "prepare_tile", prepare)
    monkeypatch.setattr(service, "run_tile", engine)
    monkeypatch.setattr(service, "patch", patch)
    monkeypatch.setattr(service, "_run_postprocess", lambda *_args: ([], 0))
    monkeypatch.setattr(service, "assemble", assemble)

    result = service.run(request, RunContext(tmp_path / "work", resume=True))

    assert result.status is TaskStatus.FAILED
    assert not request.output_vector.exists()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["read_error"]["source"] == str(request.base_raster)
    assert report["read_error"]["tile_ids"] == ["r000_c001"]
    assert report["request"]["process_raster"] == str(request.process_raster)
    assert report["tiles_processed"] == 1
    engine_checkpoint = result.report_path.parent.parent / "checkpoints" / "r000_c000-engine.json"
    assert engine_checkpoint.is_file()
    should_fail = False

    resumed = service.run(request, RunContext(tmp_path / "work", resume=True))

    assert resumed.status is TaskStatus.SUCCEEDED
    assert request.output_vector.is_file()
    assert prepare_calls == ["r000_c000", "r000_c001", "r000_c001"]
    assert engine_calls == ["r000_c000", "r000_c001"]
    assert resumed.tiles_resumed == 1
    assert dataset_fingerprint(request.process_raster) == unchanged
