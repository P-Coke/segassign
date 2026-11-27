from __future__ import annotations

from pathlib import Path

from segassign.ecognition import (
    ECOGNITION_SDK_FILES,
    detect_ecognition_bin,
    discover_ecognition_bins,
    normalize_ecognition_bin,
    resolve_ecognition_bin,
)
from segassign.models import CompositeBatchRequest, CompositeRequest, SegmentBatchRequest, SegmentRequest


def _make_sdk_bin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for filename in ECOGNITION_SDK_FILES:
        (path / filename).write_bytes(b"sdk")
    return path


def test_resolve_ecognition_bin_accepts_bin_install_root_bin64_and_exe(tmp_path):
    install_root = tmp_path / "eCognition Developer 9.0"
    sdk_bin = _make_sdk_bin(install_root / "bin")
    fake_exe = sdk_bin / "eCognitionDeveloper.exe"
    fake_exe.write_bytes(b"exe")

    assert resolve_ecognition_bin(sdk_bin) == sdk_bin.resolve()
    assert resolve_ecognition_bin(install_root) == sdk_bin.resolve()
    assert resolve_ecognition_bin(fake_exe) == sdk_bin.resolve()

    bin64_root = tmp_path / "eCognition Custom"
    bin64 = _make_sdk_bin(bin64_root / "bin64")
    assert resolve_ecognition_bin(bin64_root) == bin64.resolve()

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "Engine.dll").write_bytes(b"partial")
    assert resolve_ecognition_bin(incomplete) is None
    assert normalize_ecognition_bin(incomplete) == incomplete.resolve()


def test_discover_ecognition_bins_scans_common_dirs_and_ranks_v90_first(tmp_path, monkeypatch):
    for var in ("ECOGNITION_BIN", "ECOGNITION_HOME", "ECOGNITION_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", "")

    drive_c = tmp_path / "drive_c"
    drive_d = tmp_path / "drive_d"
    v10 = _make_sdk_bin(drive_c / "Program Files" / "Trimble" / "eCognition Developer 10.2" / "bin")
    v93 = _make_sdk_bin(drive_d / "Software" / "eCognition Developer 9.3" / "bin")
    v90 = _make_sdk_bin(drive_d / "Program Files (x86)" / "eCognition Developer 9.0" / "bin")

    found = discover_ecognition_bins(
        extra_roots=(drive_c, drive_d),
        include_registry=False,
        include_system_drives=False,
    )
    assert found == (v90.resolve(), v93.resolve(), v10.resolve())
    assert (
        detect_ecognition_bin(
            extra_roots=(drive_c, drive_d),
            include_registry=False,
            include_system_drives=False,
        )
        == v90.resolve()
    )


def test_discover_ecognition_bins_reads_environment_variable(tmp_path, monkeypatch):
    custom_root = tmp_path / "CustomTool" / "eCognition9"
    sdk_bin = _make_sdk_bin(custom_root / "bin")
    monkeypatch.setenv("ECOGNITION_HOME", str(custom_root))
    monkeypatch.setenv("PATH", "")

    found = discover_ecognition_bins(include_registry=False, include_system_drives=False)
    assert sdk_bin.resolve() in found


def test_request_models_normalize_install_root_to_bin(tmp_path):
    install_root = tmp_path / "eCognition Developer 9.0"
    sdk_bin = _make_sdk_bin(install_root / "bin")
    proc = tmp_path / "p.tif"
    base = tmp_path / "b.tif"
    cls = tmp_path / "c.tif"
    out_shp = tmp_path / "out.shp"
    out_dir = tmp_path / "out_dir"

    assert SegmentRequest(proc, base, out_shp, install_root).ecognition_bin == sdk_bin.resolve()
    assert SegmentBatchRequest(proc, base, out_dir, install_root).ecognition_bin == sdk_bin.resolve()
    assert CompositeRequest(proc, base, cls, out_shp, install_root).ecognition_bin == sdk_bin.resolve()
    assert CompositeBatchRequest(proc, base, cls, out_dir, install_root).ecognition_bin == sdk_bin.resolve()


def test_main_window_prefills_and_syncs_detected_ecognition(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    sdk_bin = _make_sdk_bin(tmp_path / "eCognition Developer 9.0" / "bin")
    monkeypatch.setattr("segassign.ui.main_window.detect_ecognition_bin", lambda: sdk_bin)

    from PySide6.QtWidgets import QApplication

    from segassign.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window.composite_page.fields["ecognition"].raw_text() == str(sdk_bin)
    assert window.segment_page.fields["ecognition"].raw_text() == str(sdk_bin)

    other = str(tmp_path / "other_eco")
    window.composite_page.fields["ecognition"].edit.setText(other)
    assert window.segment_page.fields["ecognition"].raw_text() == other

    window.close()
    app.processEvents()
