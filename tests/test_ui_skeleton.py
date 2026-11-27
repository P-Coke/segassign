from __future__ import annotations

import pytest

from segassign.models import CompositeBatchRequest, CompositeRequest, SegmentBatchRequest, SegmentRequest


@pytest.mark.parametrize("page", [0, 1, 2])
def test_ui_exposes_three_decoupled_task_pages_without_vertical_compression(monkeypatch, page):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(960, 640)
    window.show()
    window.mode.setCurrentIndex(page)
    application.processEvents()

    assert window.pages.currentIndex() == page
    assert window.mode.tabText(page) in {"00_分割并赋值", "01_单独分割", "02_单独赋值"}
    assert not window.controller.running
    current = window._current_page()
    for field in current.fields.values():
        assert field.edit.height() >= 28
    assert window.upper_pane.geometry().bottom() < window.lower_pane.geometry().top()
    window.close()
    application.processEvents()


def test_00_and_01_support_file_or_directory_inputs_and_batch_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    proc_dir = tmp_path / "process"
    proc_dir.mkdir()
    base_file = tmp_path / "base.tif"
    class_file = tmp_path / "class.tif"
    out_dir = tmp_path / "out"
    eco_dir = tmp_path / "ecognition"
    eco_dir.mkdir()

    application = QApplication.instance() or QApplication([])
    window = MainWindow()

    # 00 composite: folder input -> CompositeBatchRequest
    window.mode.setCurrentIndex(0)
    comp = window.composite_page
    assert comp.fields["process"].mode == "file-or-directory"
    assert comp.fields["base"].mode == "file-or-directory"
    assert comp.fields["classification"].mode == "file-or-directory"
    assert comp.fields["output"].mode == "save-or-directory"
    comp.fields["process"].edit.setText(str(proc_dir))
    comp.fields["base"].edit.setText(str(base_file))
    comp.fields["classification"].edit.setText(str(class_file))
    comp.fields["output"].edit.setText(str(out_dir))
    comp.fields["ecognition"].edit.setText(str(eco_dir))
    _, _, batch_req, _, _ = window._backend()
    assert isinstance(batch_req, CompositeBatchRequest)

    # 00 composite: single files + .shp output -> CompositeRequest
    comp.fields["process"].edit.setText(str(tmp_path / "single.tif"))
    comp.fields["output"].edit.setText(str(tmp_path / "single.shp"))
    _, _, single_req, _, _ = window._backend()
    assert isinstance(single_req, CompositeRequest)

    # 01 segment: folder input -> SegmentBatchRequest
    window.mode.setCurrentIndex(1)
    seg = window.segment_page
    assert seg.fields["process"].mode == "file-or-directory"
    assert seg.fields["base"].mode == "file-or-directory"
    assert seg.fields["output"].mode == "save-or-directory"
    seg.fields["process"].edit.setText(str(proc_dir))
    seg.fields["base"].edit.setText(str(base_file))
    seg.fields["output"].edit.setText(str(out_dir))
    seg.fields["ecognition"].edit.setText(str(eco_dir))
    _, _, seg_batch_req, _, _ = window._backend()
    assert isinstance(seg_batch_req, SegmentBatchRequest)

    # 01 segment: single files + .shp output -> SegmentRequest
    seg.fields["process"].edit.setText(str(tmp_path / "single.tif"))
    seg.fields["output"].edit.setText(str(tmp_path / "single.shp"))
    _, _, seg_single_req, _, _ = window._backend()
    assert isinstance(seg_single_req, SegmentRequest)

    window.close()
    application.processEvents()


def test_zonal_ui_uses_file_or_directory_input_and_hidden_work_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.mode.setCurrentIndex(2)
    page = window.zonal_page
    page.fields["vector_source"].edit.setText(str(tmp_path / "vectors"))
    page.fields["classification"].edit.setText(str(tmp_path / "area-a.tif"))
    page.fields["output_directory"].edit.setText(str(tmp_path / "output"))

    assert set(page.fields) == {"vector_source", "classification", "output_directory"}
    assert page.fields["vector_source"].mode == "file-or-directory"
    assert "work" not in page.fields
    assert window.wizard_stack.currentIndex() == 0
    assert window.next_to_confirm_button.text().startswith("下一步")
    assert window.performance_modes["auto"].isChecked()
    assert window.auto_cleanup.isChecked()
    window.close()
    application.processEvents()
