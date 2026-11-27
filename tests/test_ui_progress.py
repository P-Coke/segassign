from __future__ import annotations

from types import SimpleNamespace

from segassign.models import TaskStatus
from segassign.ui.progress import ProgressTracker, format_duration


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ev(kind: str, phase: str, message: str, **data) -> dict:
    return {"kind": kind, "phase": phase, "message": message, "data": data}


def test_segment_overlapped_progress_is_monotonic_and_estimates_eta():
    clock = _FakeClock()
    tracker = ProgressTracker(clock=clock)
    snap = tracker.start("segment", total_items=1)
    assert snap.indeterminate
    assert snap.pill_text == "启动中"

    history: list[float] = []
    events = [
        (0.2, _ev("started", "segment", "Segmentation started", plan={"tiles_total": 100})),
        (0.5, _ev("stage", "scan", "正在预扫描有效区域", total=100)),
        (1.0, _ev("progress", "prepare", "Prepared 10/100 tiles", completed=10, total=100)),
        (3.0, _ev("progress", "ecognition", "eCognition completed 10/100 tiles", completed=10, total=100)),
        (4.0, _ev("progress", "prepare", "Prepared 30/100 tiles", completed=30, total=100)),
        (6.0, _ev("progress", "ecognition", "eCognition completed 30/100 tiles", completed=30, total=100)),
        (10.0, _ev("progress", "ecognition", "eCognition completed 100/100 tiles", completed=100, total=100)),
        (11.0, _ev("progress", "postprocess", "Postprocessed 50/100 tiles", completed=50, total=100)),
        (12.0, _ev("stage", "mosaic", "正在拼接", total=100)),
        (13.0, _ev("stage", "publish", "正在发布")),
    ]
    for idx, (delta, event) in enumerate(events):
        clock.advance(delta)
        snap = tracker.update(event)
        if idx > 0:
            assert not snap.indeterminate
        history.append(snap.percent)

    assert history == sorted(history)
    assert history[3] < 20.0  # Row 0 does NOT prematurely jump to 75%.
    assert 92.0 <= history[-2] <= 98.0
    assert history[-1] > history[-2]
    assert snap.eta_seconds is not None and snap.eta_seconds > 0
    assert "预计剩余" in snap.time_text

    clock.advance(1.0)
    done = tracker.finish(TaskStatus.SUCCEEDED)
    assert done.percent == 100.0
    assert done.pill_state == "succeeded"
    assert "总耗时" in done.time_text


def test_batch_progress_never_drops_backward_between_items():
    clock = _FakeClock()
    tracker = ProgressTracker(clock=clock)
    tracker.start("segment-batch", total_items=4)

    percents: list[float] = []
    for idx in range(4):
        clock.advance(0.5)
        percents.append(
            tracker.update(
                _ev(
                    "progress",
                    "batch",
                    f"正在分割 {idx + 1}/4",
                    completed=idx,
                    total=4,
                    input=f"G:/data/scene_{idx + 1}.tif",
                )
            ).percent
        )
        for tile_done in (25, 50, 100):
            clock.advance(1.0)
            percents.append(
                tracker.update(
                    _ev(
                        "progress",
                        "ecognition",
                        f"eCognition completed {tile_done}/100 tiles",
                        completed=tile_done,
                        total=100,
                    )
                ).percent
            )
        clock.advance(0.5)
        snap = tracker.update(
            _ev(
                "progress",
                "batch",
                f"已完成分割 {idx + 1}/4",
                completed=idx + 1,
                total=4,
                output=f"G:/out/scene_{idx + 1}.shp",
            )
        )
        percents.append(snap.percent)

    assert percents == sorted(percents)
    assert 20.0 <= percents[4] <= 26.0
    assert percents[-1] >= 99.0


def test_composite_single_progresses_across_both_segment_and_zonal():
    clock = _FakeClock()
    tracker = ProgressTracker(clock=clock)
    tracker.start("composite", total_items=1)
    tracker.update(_ev("started", "composite", "Composite run started"))
    tracker.update(_ev("stage", "composite-segment", "步骤 1/2"))
    clock.advance(2.0)
    seg_end = tracker.update(_ev("stage", "publish", "正在发布"))
    assert 70.0 <= seg_end.percent <= 75.5

    clock.advance(1.0)
    zonal_start = tracker.update(_ev("stage", "composite-zonal", "步骤 2/2"))
    clock.advance(2.0)
    zonal_mid = tracker.update(_ev("progress", "count", "Counted 5/10 raster windows", completed=5, total=10))
    clock.advance(2.0)
    zonal_write = tracker.update(_ev("stage", "write", "正在写入"))
    assert zonal_start.percent >= seg_end.percent
    assert zonal_mid.percent > zonal_start.percent
    assert zonal_write.percent > zonal_mid.percent
    assert "步骤 2/2" in zonal_mid.headline


def test_resumed_jump_reanchors_eta_and_formats_duration():
    clock = _FakeClock()
    tracker = ProgressTracker(clock=clock)
    tracker.start("segment", total_items=1)
    clock.advance(0.2)
    resumed = tracker.update(
        _ev("progress", "ecognition", "eCognition completed 60/100 tiles", completed=60, total=100)
    )
    assert resumed.percent > 40.0
    assert resumed.eta_seconds is None

    clock.advance(4.0)
    next_snap = tracker.update(
        _ev("progress", "ecognition", "eCognition completed 70/100 tiles", completed=70, total=100)
    )
    assert next_snap.eta_seconds is not None
    assert next_snap.eta_seconds > 10.0
    assert format_duration(75) == "01:15"
    assert format_duration(3665) == "1:01:05"


def test_main_window_renders_progress_strip_and_state_transitions(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(960, 640)
    window.show()
    app.processEvents()

    assert window.progress.minimum() == 0
    assert window.progress.maximum() == 1000
    assert "就绪" in window.progress_detail.text()
    assert window.header_status.text() == "就绪"

    window._on_event(_ev("progress", "ecognition", "eCognition completed 50/100 tiles", completed=50, total=100))
    app.processEvents()
    assert window.progress.value() > 300
    assert "eCognition 分割" in window.progress_detail.text()
    assert window.header_status.text().startswith("运行中")
    assert "已完成 eCognition 分割 50/100 块瓦片" in window.log.toPlainText()

    dummy_result = SimpleNamespace(
        status=TaskStatus.SUCCEEDED,
        warnings=(),
        report_path=window._cache_root() / "run.json",
    )
    window._on_result(dummy_result)
    app.processEvents()
    assert window.progress.value() == 1000
    assert window.header_status.text() == "已完成"
    assert window.progress.property("state") == "succeeded"
    assert window.upper_pane.geometry().bottom() < window.lower_pane.geometry().top()

    window.close()
    app.processEvents()
