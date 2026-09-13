"""Single release entrypoint."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def _configure_gis_data() -> None:
    bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    gdal = bundle / "gdal_data"
    proj = bundle / "Library" / "share" / "proj"
    if gdal.is_dir():
        os.environ["GDAL_DATA"] = str(gdal)
    if (proj / "proj.db").is_file():
        os.environ["PROJ_DATA"] = str(proj)
        os.environ["PROJ_LIB"] = str(proj)


def _run_batch_smoke(configuration: Path) -> int:
    """Exercise the frozen GIS stack and Windows process pool for release QA."""
    from segassign import RunContext, TaskStatus, ZonalBatchRequest, ZonalOptions, zonal

    payload = json.loads(configuration.read_text(encoding="utf-8"))
    request = ZonalBatchRequest(
        Path(payload["vector_source"]),
        Path(payload["raster"]),
        Path(payload["output_directory"]),
        ZonalOptions(
            valid_values=tuple(payload.get("valid_values", ())),
            tile_size=int(payload.get("tile_size", 256)),
            workers=int(payload.get("workers", 2)),
        ),
    )
    result = zonal.run_batch(request, RunContext(request.output_directory / ".geotoolbox-work"))
    configuration.with_suffix(".result.json").write_text(
        json.dumps(
            {
                "status": result.status.value,
                "items_total": result.items_total,
                "items_succeeded": result.items_succeeded,
                "outputs": [str(path) for path in result.primary_vectors],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if result.status is TaskStatus.SUCCEEDED else 1


def main() -> int:
    try:
        multiprocessing.freeze_support()
        _configure_gis_data()
        batch_smoke = os.environ.get("SEGASSIGN_BATCH_SMOKE_CONFIG", "").strip()
        if batch_smoke:
            return _run_batch_smoke(Path(batch_smoke).resolve())
        from segassign.ui import MainWindow

        application = QApplication(sys.argv)
        application.setApplicationName("SegAssign")
        window = MainWindow()
        window.show()
        if os.environ.get("SEGASSIGN_RELEASE_SMOKE") == "1":
            QTimer.singleShot(300, application.quit)
        return application.exec()
    except Exception:
        diagnostic = Path(os.environ.get("TEMP", ".")) / "segassign-startup-error.txt"
        diagnostic.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
