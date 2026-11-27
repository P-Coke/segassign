from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import (
    CompositeBatchRequest,
    RunContext,
    SegmentBatchRequest,
    TaskStatus,
    ZonalBatchRequest,
    ZonalOptions,
    ZonalRequest,
    composite,
    segment,
    zonal,
)
from segassign.ecognition import ECOGNITION_SDK_FILES
from segassign.segment import batch as seg_batch
from segassign.segment.service import SharedEnginePool
from segassign.ui.progress import BatchMonitorModel


def _write_tif(path: Path, width: int = 32, height: int = 32, bands: int = 1, val: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=bands,
        dtype="uint8",
        crs="EPSG:4526",
        transform=from_origin(0, height, 1, 1),
    ) as ds:
        ds.write(np.full((bands, height, width), val, dtype=np.uint8))
    return path


def _write_shp(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 16, 16)], crs="EPSG:4526"),
        path,
        driver="ESRI Shapefile",
    )
    return path


def _make_sdk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in ECOGNITION_SDK_FILES:
        (path / name).write_bytes(b"sdk")
    return path


def test_selected_inputs_filters_batch_requests_and_populates_items_preview(tmp_path):
    proc_dir = tmp_path / "proc"
    base_tif = _write_tif(tmp_path / "base.tif")
    cls_tif = _write_tif(tmp_path / "cls.tif", val=2)
    sdk = _make_sdk(tmp_path / "eco" / "bin")
    a = _write_tif(proc_dir / "scene_a.tif")
    b = _write_tif(proc_dir / "scene_b.tif")
    c = _write_tif(proc_dir / "scene_c.tif")

    seg_req = SegmentBatchRequest(proc_dir, base_tif, tmp_path / "seg_out", sdk)
    full_plan = segment.plan_batch(seg_req)
    assert full_plan.metrics["items"] == 3
    assert len(full_plan.metrics["items_preview"]) == 3

    filtered_seg = SegmentBatchRequest(proc_dir, base_tif, tmp_path / "seg_out", sdk, selected_inputs=(a, c))
    filtered_plan = segment.plan_batch(filtered_seg)
    assert filtered_plan.metrics["items"] == 2
    assert [e["name"] for e in filtered_plan.metrics["items_preview"]] == ["scene_a.tif", "scene_c.tif"]

    filtered_comp = CompositeBatchRequest(
        proc_dir, base_tif, cls_tif, tmp_path / "comp_out", sdk, selected_inputs=(b,)
    )
    comp_plan = composite.plan_batch(filtered_comp)
    assert comp_plan.metrics["items"] == 1
    assert comp_plan.metrics["items_preview"][0]["name"] == "scene_b.tif"

    vec_dir = tmp_path / "vecs"
    v1 = _write_shp(vec_dir / "area1.shp")
    _write_shp(vec_dir / "area2.shp")
    filtered_zonal = ZonalBatchRequest(
        vec_dir,
        cls_tif,
        tmp_path / "zonal_out",
        ZonalOptions(valid_values=(2,), tile_size=256),
        selected_inputs=(v1,),
    )
    zonal_plan = zonal.plan_batch(filtered_zonal)
    assert zonal_plan.metrics["items"] == 1
    assert zonal_plan.metrics["items_preview"][0]["name"] == "area1.shp"


def test_tail_launch_starts_second_scene_before_first_scene_finishes(tmp_path, monkeypatch):
    proc_dir = tmp_path / "proc"
    base_tif = _write_tif(tmp_path / "base.tif")
    sdk = _make_sdk(tmp_path / "eco" / "bin")
    s1 = _write_tif(proc_dir / "s1.tif")
    s2 = _write_tif(proc_dir / "s2.tif")

    overlap_detected = threading.Event()
    s1_finishing = threading.Event()
    active_lock = threading.Lock()
    active_scenes: set[str] = set()

    def fake_engine_stage(
        item,
        ctx,
        *,
        hold_lease=True,
        prefetched=None,
        on_prepare_complete=None,
        shared_pool=None,
        on_tail_ready=None,
    ):
        assert isinstance(shared_pool, SharedEnginePool)
        name = item.process_raster.name
        with active_lock:
            active_scenes.add(name)
            if len(active_scenes) >= 2:
                overlap_detected.set()
        ctx.emit("progress", "ecognition", "eCognition completed 1/2 tiles", completed=1, total=2)
        if name == "s1.tif":
            # Signal tail ready while s1 still holds a tail tile open, proving s2 starts concurrently!
            if on_tail_ready is not None:
                on_tail_ready()
            overlap_detected.wait(timeout=2.0)
            s1_finishing.set()
        else:
            s1_finishing.wait(timeout=2.0)
            if on_tail_ready is not None:
                on_tail_ready()
        with active_lock:
            active_scenes.discard(name)
        from segassign.models import Artifact, SegmentResult

        report = tmp_path / f"{name}.json"
        report.write_text("{}", encoding="utf-8")
        _write_shp(item.output_vector)
        return SegmentResult(
            TaskStatus.SUCCEEDED,
            (Artifact("primary_vector", item.output_vector), Artifact("run_report", report)),
            (),
            {"total": 0.1},
            10.0,
            report,
            item.output_vector,
            2,
            2,
            0,
        )

    monkeypatch.setattr(seg_batch, "_run_engine_stage", fake_engine_stage)
    monkeypatch.setattr(seg_batch, "wait_for_external_engines", lambda _ctx: None)

    events = []
    ctx = RunContext(tmp_path / "work", event_sink=events.append, resume=False)
    req = SegmentBatchRequest(proc_dir, base_tif, tmp_path / "out", sdk)
    result = seg_batch.run_batch(req, ctx)

    assert result.status is TaskStatus.SUCCEEDED
    assert result.items_succeeded == 2
    assert overlap_detected.is_set()
    # Scoped item_id tags must exist on per-scene progress events.
    eco_events = [e for e in events if e.phase == "ecognition"]
    assert {e.data.get("item_id") for e in eco_events} == {str(s1.resolve()), str(s2.resolve())}


def test_batch_monitor_model_tracks_overlapping_items_and_total_monotonically():
    now = [100.0]
    model = BatchMonitorModel(clock=lambda: now[0])
    preview = [
        {"item_id": "G:/a.tif", "name": "a.tif", "output_name": "a.shp"},
        {"item_id": "G:/b.tif", "name": "b.tif", "output_name": "b.shp"},
    ]
    snap0 = model.start("segment-batch", preview)
    assert snap0.total_count == 2
    assert snap0.overall.percent == 0.0

    # Scene A runs to 80%, then Scene B launches in tail while A is still running!
    now[0] += 5.0
    snap1 = model.update(
        {
            "kind": "progress",
            "phase": "ecognition",
            "message": "eCognition completed 80/100 tiles",
            "data": {"item_id": "G:/a.tif", "completed": 80, "total": 100},
        }
    )
    now[0] += 3.0
    snap2 = model.update(
        {
            "kind": "progress",
            "phase": "ecognition",
            "message": "eCognition completed 20/100 tiles",
            "data": {"item_id": "G:/b.tif", "completed": 20, "total": 100},
        }
    )
    assert snap2.active_count == 2
    assert "尾段并行加速中" in snap2.overall.headline
    assert snap2.items[0].percent > 50.0
    assert 10.0 < snap2.items[1].percent < 30.0
    assert snap2.overall.percent > snap1.overall.percent

    # Scene A completes while Scene B continues; overall stays strictly monotonic.
    now[0] += 2.0
    snap3 = model.update(
        {
            "kind": "progress",
            "phase": "batch",
            "message": "已完成 1/2",
            "data": {"item_id": "G:/a.tif", "completed": 1, "total": 2, "output": "G:/out/a.shp"},
        }
    )
    assert snap3.items[0].state == "succeeded"
    assert snap3.items[0].percent == 100.0
    assert snap3.items[1].state == "running"
    assert snap3.overall.percent >= snap2.overall.percent

    now[0] += 4.0
    done = model.finish(TaskStatus.SUCCEEDED)
    assert done.overall.percent == 100.0
    assert all(item.percent == 100.0 for item in done.items)


def test_three_step_wizard_navigation_and_per_item_progress_bars(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    vec_dir = tmp_path / "vecs"
    v1 = _write_shp(vec_dir / "tile_01.shp")
    v2 = _write_shp(vec_dir / "tile_02.shp")
    cls_tif = _write_tif(tmp_path / "cls.tif", val=3)
    out_dir = tmp_path / "out"

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1240, 860)
    window.show()
    app.processEvents()

    # Step 1: configure 02 zonal folder batch
    window.mode.setCurrentIndex(2)
    window.zonal_page.fields["vector_source"].edit.setText(str(vec_dir))
    window.zonal_page.fields["classification"].edit.setText(str(cls_tif))
    window.zonal_page.fields["output_directory"].edit.setText(str(out_dir))
    app.processEvents()

    # Advance to Step 2: Confirmation table populated with 2 checked items by default
    window._go_to_confirm()
    app.processEvents()
    assert window.wizard_stack.currentIndex() == 1
    assert window.confirm_table.rowCount() == 2
    assert len(window._selected_inputs()) == 2
    assert "已选 2 项" in window.run_button.text()

    # Uncheck second item -> selection count updates immediately
    window.confirm_table.item(1, 0).setCheckState(Qt.Unchecked)
    app.processEvents()
    assert window._selected_inputs() == (v1.resolve(),)
    assert "已选 1 项" in window.run_button.text()

    # Re-select all -> simulate Step 3 per-item progress monitor
    window._set_all_confirm_checked(True)
    app.processEvents()
    assert len(window._selected_inputs()) == 2

    preview = list(window._staged_items)
    window._build_monitor_rows(preview)
    window._using_batch_monitor = True
    window._apply_monitor_snapshot(window._batch_monitor.start("zonal-batch", preview))
    window._set_wizard_step(2)
    app.processEvents()

    assert window.wizard_stack.currentIndex() == 2
    assert window.monitor_table.rowCount() == 2
    assert len(window._monitor_bars) == 2

    window._on_event(
        {
            "kind": "progress",
            "phase": "count",
            "message": "Counted 5/10 raster windows",
            "data": {"item_id": str(v1.resolve()), "completed": 5, "total": 10},
        }
    )
    window._on_event(
        {
            "kind": "progress",
            "phase": "count",
            "message": "Counted 2/10 raster windows",
            "data": {"item_id": str(v2.resolve()), "completed": 2, "total": 10},
        }
    )
    app.processEvents()
    assert window._monitor_bars[0].value() > window._monitor_bars[1].value() > 0
    assert window.progress.value() > 0

    window._on_result(
        SimpleNamespace(
            status=TaskStatus.SUCCEEDED,
            warnings=(),
            report_path=out_dir / "run.json",
            items_succeeded=2,
            items_total=2,
        )
    )
    app.processEvents()
    assert window.progress.value() == 1000
    assert all(bar.value() == 1000 for bar in window._monitor_bars)

    window.close()
    app.processEvents()
    _ = time.monotonic()


def test_tile_map_viewport_tracks_red_blue_green_and_emits_from_zonal(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    # 1. Verify zonal.run emits lightweight grid & tile events
    vec = _write_shp(tmp_path / "area.shp")
    cls = _write_tif(tmp_path / "cls.tif", width=64, height=64, val=3)
    out = tmp_path / "out.shp"
    emitted = []
    ctx = RunContext(tmp_path / "work", event_sink=emitted.append, resume=False)
    res = zonal.run(
        ZonalRequest(vec, cls, out, ZonalOptions(valid_values=(3,), tile_size=256)),
        ctx,
    )
    assert res.status is TaskStatus.SUCCEEDED
    grid_events = [e for e in emitted if e.kind == "grid"]
    tile_events = [e for e in emitted if e.kind == "tile"]
    assert len(grid_events) == 1
    assert any(e.data.get("state") == "done" for e in tile_events)

    # 2. Verify TileMapPanel renders pending (red) -> running (blue) -> done (green) -> skipped (gray)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1240, 860)
    window.show()
    preview = [
        {
            "item_id": "G:/scene_a.tif",
            "name": "scene_a.tif",
            "relative_path": "scene_a.tif",
            "output_name": "scene_a.shp",
        }
    ]
    window._build_monitor_rows(preview)
    window._using_batch_monitor = True
    window._apply_monitor_snapshot(window._batch_monitor.start("segment-batch", preview))
    window._set_wizard_step(2)

    window._on_events_batch(
        [
            {
                "kind": "grid",
                "phase": "tile-map",
                "message": "已生成 4 块切片网格",
                "data": {
                    "item_id": "G:/scene_a.tif",
                    "item_name": "scene_a.tif",
                    "width": 200,
                    "height": 200,
                    "crs": "EPSG:4526",
                    "bounds": [0.0, 0.0, 200.0, 200.0],
                    "tiles": [
                        ("r0_c0", 0, 0, 100, 100),
                        ("r0_c1", 100, 0, 100, 100),
                        ("r1_c0", 0, 100, 100, 100),
                        ("r1_c1", 100, 100, 100, 100),
                    ],
                },
            },
            {
                "kind": "tile",
                "phase": "tile-map",
                "message": "",
                "data": {"item_id": "G:/scene_a.tif", "tile_id": "r0_c0", "state": "done"},
            },
            {
                "kind": "tile",
                "phase": "tile-map",
                "message": "",
                "data": {"item_id": "G:/scene_a.tif", "tile_id": "r0_c1", "state": "running"},
            },
            {
                "kind": "tile",
                "phase": "tile-map",
                "message": "",
                "data": {"item_id": "G:/scene_a.tif", "tile_id": "r1_c1", "state": "skipped"},
            },
        ]
    )
    app.processEvents()
    assert window.tile_map._legend_labels["done"].text() == "已完成 1"
    assert window.tile_map._legend_labels["running"].text() == "处理中 1"
    assert window.tile_map._legend_labels["pending"].text() == "未处理 1"
    assert window.tile_map._legend_labels["skipped"].text() == "空区 1"
    # Tile state events must not spam the text log.
    assert window.log.toPlainText().strip() == ""

    window.close()
    app.processEvents()
