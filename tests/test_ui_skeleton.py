from __future__ import annotations

import pytest


@pytest.mark.parametrize("page", [0, 1, 2])
def test_ui_exposes_three_decoupled_task_pages(monkeypatch, page):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.mode.setCurrentIndex(page)

    assert window.pages.currentIndex() == page
    assert window.mode.tabText(page) in {"00_分割并赋值", "01_单独分割", "02_单独赋值"}
    assert not window.controller.running
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
    assert window.run_button.text() == "开始批量赋值"
    window.close()
    application.processEvents()
