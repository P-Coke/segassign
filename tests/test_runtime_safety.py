from __future__ import annotations

from pathlib import Path

import numpy as np
import psutil
import rasterio
from rasterio.transform import from_origin

from segassign import ResourceBudget, SegmentOptions, SegmentRequest, TaskEvent, segment
from segassign.runtime import EventQueue, ResourceScheduler, RunContext, RunWorkspace
from segassign.runtime import workspace as workspace_module


def _raster(path: Path):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=16,
        height=16,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 16, 1, 1),
    ) as dataset:
        dataset.write(np.ones((1, 16, 16), dtype=np.uint8))


def test_missing_sdk_fails_before_any_output_or_work_write(tmp_path):
    process = tmp_path / "process.tif"
    base = tmp_path / "base.tif"
    _raster(process)
    _raster(base)
    output = tmp_path / "output" / "segments.shp"

    try:
        segment.run(
            SegmentRequest(
                process,
                base,
                output,
                tmp_path / "missing-sdk",
                options=SegmentOptions(tile_size=512),
            )
        )
    except ValueError as error:
        assert "eCognition" in str(error)
    else:
        raise AssertionError("missing SDK did not fail")
    assert not output.parent.exists()


def test_corrupt_checkpoint_is_locally_invalidated(tmp_path):
    workspace = RunWorkspace(tmp_path, "zonal", "abc123", resume=True)
    workspace.ensure()
    workspace.checkpoint_path("tile-a").write_text("{broken", encoding="utf-8")

    assert not workspace.checkpoint_matches("tile-a", "fingerprint")
    workspace.mark("tile-b", "fingerprint", output="ok")
    assert workspace.checkpoint_matches("tile-b", "fingerprint")

    artifact = tmp_path / "tile.tif"
    artifact.write_bytes(b"valid")
    workspace.mark(
        "tile-c",
        "fingerprint",
        artifacts={str(artifact): workspace_module.file_fingerprint(artifact)},
    )
    assert workspace.checkpoint_matches("tile-c", "fingerprint", (artifact,))
    artifact.write_bytes(b"corrupted")
    assert not workspace.checkpoint_matches("tile-c", "fingerprint", (artifact,))


def test_completed_shapefile_fingerprint_invalidates_on_sidecar_change(tmp_path):
    shape = tmp_path / "result.shp"
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        shape.with_suffix(suffix).write_bytes(("valid" + suffix).encode())
    shape.with_suffix(".gtz.npz").write_bytes(b"valid-state")
    expected = workspace_module.dataset_fingerprint(shape)

    assert workspace_module.dataset_matches(shape, expected)
    shape.with_suffix(".dbf").write_bytes(b"corrupted")
    assert not workspace_module.dataset_matches(shape, expected)
    shape.with_suffix(".dbf").write_bytes(b"valid.dbf")
    shape.with_suffix(".gtz.npz").write_bytes(b"corrupted-state")
    assert not workspace_module.dataset_matches(shape, expected)


def test_task_event_has_json_safe_serialization():
    event = TaskEvent(1, "progress", "prepare", "ready", {"tile": "r000_c000"})

    assert event.serializable() == {
        "sequence": 1,
        "kind": "progress",
        "phase": "prepare",
        "message": "ready",
        "data": {"tile": "r000_c000"},
        "task_id": "",
        "timestamp": event.timestamp,
    }


def test_event_queue_is_bounded_and_ui_sink_failures_do_not_fail_backend():
    events = EventQueue(max_events=2)
    context = RunContext(Path.cwd(), event_sink=events, task_id="task")
    context.emit("progress", "prepare", "one")
    context.emit("progress", "prepare", "two")
    context.emit("progress", "prepare", "three")

    drained = events.drain()
    assert [event.message for event in drained] == ["two", "three"]
    assert all(event.task_id == "task" for event in drained)
    RunContext(Path.cwd(), event_sink=lambda event: 1 / 0).emit("progress", "prepare", "safe")


def test_calibrated_ecognition_budget_allows_seven_workers(monkeypatch):
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("Memory", (), {"total": 24 * 1024**3})())
    scheduler = ResourceScheduler(ResourceBudget(memory_fraction=0.80, ecognition_workers=7))

    assert scheduler.ecognition_workers() == 7


def test_shapefile_publish_rolls_back_all_sidecars_on_failure(tmp_path, monkeypatch):
    staged = tmp_path / "stage" / "result.shp"
    destination = tmp_path / "output" / "result.shp"
    staged.parent.mkdir()
    destination.parent.mkdir()
    for suffix in (".shp", ".shx", ".dbf", ".prj", ".gtz.npz"):
        staged.with_suffix(suffix).write_bytes(b"new-" + suffix.encode())
        destination.with_suffix(suffix).write_bytes(b"old-" + suffix.encode())

    original_replace = workspace_module.os.replace
    calls = 0

    def fail_second_publish(source, target):
        nonlocal calls
        if Path(source).parent == staged.parent:
            calls += 1
            if calls == 2:
                raise OSError("injected publication failure")
        return original_replace(source, target)

    monkeypatch.setattr(workspace_module.os, "replace", fail_second_publish)
    try:
        workspace_module.publish_shapefile(staged, destination)
    except OSError as error:
        assert "injected" in str(error)
    else:
        raise AssertionError("injected failure did not escape publication")

    for suffix in (".shp", ".shx", ".dbf", ".prj", ".gtz.npz"):
        assert destination.with_suffix(suffix).read_bytes() == b"old-" + suffix.encode()
