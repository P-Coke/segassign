from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from segassign import RunContext, TaskStatus, ZonalOptions, ZonalRequest, zonal
from segassign.runtime import (
    DriveInfo,
    HardwareProfile,
    RunWorkspace,
    adapt_tile_size,
    build_resource_budget,
    cache_usage_bytes,
    clear_cache,
    prune_expired_cache,
    select_best_cache_drive,
)
from segassign.runtime import workspace as workspace_module
from segassign.segment.grid import build_grid
from segassign.segment.service import _order_rows_by_valid_density


def test_select_best_cache_drive_prefers_non_system_nvme_with_room():
    drives = (
        DriveInfo(Path("C:/"), "NVMe", free_gb=10.5, total_gb=200.0, is_system=True),
        DriveInfo(Path("G:/"), "USB", free_gb=88.0, total_gb=477.0, is_system=False),
        DriveInfo(Path("D:/"), "NVMe", free_gb=184.0, total_gb=750.0, is_system=False),
        DriveInfo(Path("E:/"), "USB", free_gb=3000.0, total_gb=3700.0, is_system=False),
    )

    best = select_best_cache_drive(drives)
    assert best is not None
    assert best.mount == Path("D:/")


def test_build_resource_budget_scales_with_physical_cores_and_available_ram():
    laptop = HardwareProfile(
        physical_cores=14,
        logical_cpus=20,
        total_memory_gb=24.0,
        available_memory_gb=16.0,
        recommended_cache_dir=Path("D:/SegAssign-Cache"),
        cache_drive_label="D: (NVMe)",
    )
    low = build_resource_budget(laptop, "low")
    auto = build_resource_budget(laptop, "auto")
    high = build_resource_budget(laptop, "high")

    assert low.ecognition_workers < auto.ecognition_workers <= high.ecognition_workers
    assert auto.ecognition_workers == 12
    assert high.ecognition_workers == 14

    tight_ram = HardwareProfile(
        physical_cores=16,
        logical_cpus=32,
        total_memory_gb=16.0,
        available_memory_gb=4.0,
        recommended_cache_dir=Path("D:/SegAssign-Cache"),
        cache_drive_label="D: (NVMe)",
    )
    auto_tight = build_resource_budget(tight_ram, "auto")
    assert auto_tight.ecognition_workers <= 5


def test_adapt_tile_size_shrinks_medium_rasters_and_keeps_3072_for_full_scenes():
    # Medium 6144x6144 on 10 workers: 3072 -> 4 tiles (<10), 2048 -> 9 tiles
    assert adapt_tile_size(6144, 6144, engine_workers=10, requested_tile_size=3072) == 2048
    # Full 45280x36900: 3072 -> 195 tiles (>=10), keep 3072 for peak Mpx/s
    assert adapt_tile_size(45280, 36900, engine_workers=12, requested_tile_size=3072) == 3072
    # Tiny raster: only 1 tile at 3072, keep 3072
    assert adapt_tile_size(1024, 1024, engine_workers=10, requested_tile_size=3072) == 3072
    # Explicit custom tile_size is respected untouched
    assert adapt_tile_size(6144, 6144, engine_workers=10, requested_tile_size=1024) == 1024


def test_cache_lifecycle_purge_ttl_and_manual_clear(tmp_path):
    cache_root = tmp_path / "SegAssign-Cache"
    ws = RunWorkspace(cache_root, "segment", "abcdef0123456789", resume=False)
    ws.ensure()
    (ws.root / "tiles").mkdir()
    (ws.root / "tiles" / "big.bin").write_bytes(b"x" * 4096)
    report = ws.report("run", {"status": "succeeded"})

    assert cache_usage_bytes(cache_root) >= 4096
    archived, warnings = ws.purge_after_success(report)
    assert warnings == ()
    assert archived.is_file()
    assert not ws.root.exists()
    assert cache_usage_bytes(cache_root) == 0

    # Create one stale (10 days old) and one fresh run cache
    stale = RunWorkspace(cache_root, "zonal", "1111111111111111", resume=True)
    stale.ensure()
    stale_file = stale.root / "staging" / "old.bin"
    stale_file.write_bytes(b"y" * 2048)
    old_time = time.time() - 10 * 86400
    for item in [stale_file, stale.staging, stale.checkpoints, stale.reports, stale.root]:
        os.utime(item, (old_time, old_time))

    fresh = RunWorkspace(cache_root, "zonal", "2222222222222222", resume=True)
    fresh.ensure()
    (fresh.root / "staging" / "new.bin").write_bytes(b"z" * 1024)

    removed, freed = prune_expired_cache(cache_root, max_age_days=7.0)
    assert removed == 1
    assert freed >= 2048
    assert not stale.root.exists()
    assert fresh.root.exists()

    cleared, cleared_bytes = clear_cache(cache_root)
    assert cleared == 1
    assert cleared_bytes >= 1024
    assert cache_usage_bytes(cache_root) == 0


def test_zonal_cleanup_on_success_purges_workspace_and_keeps_archived_report(tmp_path):
    raster = tmp_path / "cls.tif"
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=8,
        height=8,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 8, 1, 1),
    ) as ds:
        ds.write(np.full((1, 8, 8), 3, dtype=np.uint8))
    vector = tmp_path / "in.shp"
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 8, 8)], crs="EPSG:3857"),
        vector,
        driver="ESRI Shapefile",
    )
    out = tmp_path / "out.shp"
    cache = tmp_path / "cache"
    result = zonal.run(
        ZonalRequest(vector, raster, out, ZonalOptions(valid_values=(3,), tile_size=256)),
        RunContext(cache, resume=False, cleanup_on_success=True),
    )

    assert result.status is TaskStatus.SUCCEEDED
    assert out.is_file()
    assert result.report_path.is_file()
    assert cache_usage_bytes(cache) == 0


def test_publish_shapefile_handles_cross_device_exdev_fallback(tmp_path, monkeypatch):
    staged = tmp_path / "stage" / "res.shp"
    dest = tmp_path / "dest" / "res.shp"
    staged.parent.mkdir()
    for suffix in (".shp", ".shx", ".dbf"):
        staged.with_suffix(suffix).write_bytes(b"payload-" + suffix.encode())

    def exdev_replace(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(workspace_module.os, "replace", exdev_replace)
    workspace_module.publish_shapefile(staged, dest)

    for suffix in (".shp", ".shx", ".dbf"):
        assert dest.with_suffix(suffix).read_bytes() == b"payload-" + suffix.encode()
        assert not staged.with_suffix(suffix).exists()


def test_order_rows_by_valid_density_schedules_dense_center_rows_first(tmp_path):
    tif = tmp_path / "diamond.tif"
    # 4 rows x 4 cols of 512px = 2048x2048; make row 1 & 2 dense, row 0 & 3 sparse
    arr = np.zeros((1, 2048, 2048), dtype=np.uint8)
    arr[0, 512:1024, :] = 10  # row 1: 100% valid
    arr[0, 1024:1536, 0:1536] = 10  # row 2: 75% valid
    arr[0, 0:512, 0:512] = 10  # row 0: 25% valid
    # row 3: 0% valid
    with rasterio.open(
        tif,
        "w",
        driver="GTiff",
        width=2048,
        height=2048,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 2048, 1, 1),
    ) as ds:
        ds.write(arr)
    with rasterio.open(tif) as ds:
        tiles = build_grid(ds.width, ds.height, ds.transform, 512, 16)
        rows: dict[int, list] = {}
        for t in tiles:
            rows.setdefault(t.row, []).append(t)
        ordered = _order_rows_by_valid_density(ds, rows, (0,))

    assert ordered == [1, 2, 0, 3]
