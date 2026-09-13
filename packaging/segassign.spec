import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).parent
runtime_data = [
    (str(project_root / "assets"), "assets"),
    (str(Path(os.environ["CONDA_PREFIX"]) / "Library" / "share" / "gdal"), "gdal_data"),
    *collect_data_files("rasterio"),
    *collect_data_files("pyproj"),
]
hidden_imports = [
    "pyogrio._env",
    "pyogrio._err",
    "pyogrio._geometry",
    "pyogrio._io",
    "pyogrio._ogr",
    "pyogrio._vsi",
    "pyogrio.core",
    "pyogrio.geopandas",
    "pyogrio.raw",
    *collect_submodules("rasterio"),
    *collect_submodules("pyproj"),
    "segassign.ui.main_window",
    "segassign.zonal.batch",
]

analysis = Analysis(
    [str(project_root / "packaging" / "entrypoint.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=runtime_data,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "matplotlib", "notebook", "pytest", "tkinter"],
    noarchive=False,
    optimize=0,
)
analysis.binaries = [
    entry
    for entry in analysis.binaries
    if not entry[0].casefold().endswith(".mamba_trash")
    and not entry[1].casefold().endswith(".mamba_trash")
]
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SegAssign",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
release = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="SegAssign",
)
