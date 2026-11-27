"""Hardware profiling, automatic concurrency tuning, and fastest-disk cache selection."""

from __future__ import annotations

import ctypes
import math
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from ..models import ResourceBudget

_BUS_NAMES = {
    7: "USB",
    11: "SATA",
    17: "NVMe",
}
_MIN_CACHE_FREE_GB = 20.0
_CACHE_DIR_NAME = "SegAssign-Cache"


@dataclass(frozen=True, slots=True)
class DriveInfo:
    mount: Path
    bus: str
    free_gb: float
    total_gb: float
    is_system: bool

    @property
    def label(self) -> str:
        drive = str(self.mount).rstrip("\\/")
        return f"{drive} ({self.bus}, {self.free_gb:.1f} GB 可用)"


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    physical_cores: int
    logical_cpus: int
    total_memory_gb: float
    available_memory_gb: float
    recommended_cache_dir: Path
    cache_drive_label: str


def _windows_drive_bus(drive_letter: str) -> str:
    """Query storage BusType without admin rights via IOCTL_STORAGE_QUERY_PROPERTY."""
    if os.name != "nt":
        return "Local"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        device_io = kernel32.DeviceIoControl
        device_io.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        device_io.restype = ctypes.c_bool
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool

        clean_letter = drive_letter.rstrip(":\\/")
        volume_path = f"\\\\.\\{clean_letter}:"
        handle = create_file(volume_path, 0, 3, None, 3, 0, None)
        invalid_handle = ctypes.c_void_p(-1).value
        if not handle or handle == invalid_handle:
            return "Local"
        try:
            # STORAGE_PROPERTY_QUERY: PropertyId=0 (StorageDeviceProperty), QueryType=0
            query = (ctypes.c_uint32 * 3)(0, 0, 0)
            out_buf = ctypes.create_string_buffer(1024)
            returned = ctypes.c_uint32(0)
            ioctl_storage_query_property = 0x002D1400
            ok = device_io(
                handle,
                ioctl_storage_query_property,
                ctypes.byref(query),
                ctypes.sizeof(query),
                out_buf,
                len(out_buf),
                ctypes.byref(returned),
                None,
            )
            if ok and returned.value >= 32:
                # BusType is at byte offset 28 in STORAGE_DEVICE_DESCRIPTOR
                bus_type = int.from_bytes(out_buf.raw[28:32], "little")
                return _BUS_NAMES.get(bus_type, "Local")
        finally:
            close_handle(handle)
    except Exception:
        pass
    return "Local"


def discover_drives() -> tuple[DriveInfo, ...]:
    """List writable local/fixed drives with bus type and free space."""
    system_drive = os.environ.get("SystemDrive", "C:").casefold().rstrip("\\/")
    drives: list[DriveInfo] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint
        key = mount.casefold()
        if key in seen:
            continue
        seen.add(key)
        opts = part.opts.casefold()
        if "cdrom" in opts or "ro" in opts.split(","):
            continue
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        mount_path = Path(mount)
        if not os.access(mount_path, os.W_OK):
            continue
        bus = _windows_drive_bus(mount[:2]) if os.name == "nt" and len(mount) >= 2 and mount[1] == ":" else "Local"
        drives.append(
            DriveInfo(
                mount=mount_path,
                bus=bus,
                free_gb=round(usage.free / 1024**3, 1),
                total_gb=round(usage.total / 1024**3, 1),
                is_system=mount.casefold().rstrip("\\/") == system_drive,
            )
        )
    return tuple(drives)


def select_best_cache_drive(drives: tuple[DriveInfo, ...]) -> DriveInfo | None:
    """Prefer an internal NVMe/SATA non-system drive with >= 20 GB free space."""
    if not drives:
        return None
    bus_rank = {"NVMe": 4, "SATA": 3, "Local": 2, "USB": 1}

    def score(drive: DriveInfo) -> tuple[int, int, int, float]:
        has_room = 1 if drive.free_gb >= _MIN_CACHE_FREE_GB else 0
        non_sys = 1 if not drive.is_system else 0
        return (has_room, bus_rank.get(drive.bus, 1), non_sys, drive.free_gb)

    return max(drives, key=score)


def detect_hardware() -> HardwareProfile:
    """Detect CPU physical/logical topology, memory headroom, and fastest cache disk."""
    logical = max(1, os.cpu_count() or 1)
    physical = psutil.cpu_count(logical=False) or max(1, logical // 2)
    physical = max(1, min(physical, logical))
    vm = psutil.virtual_memory()
    total_gb = round(vm.total / 1024**3, 1)
    avail_gb = round(vm.available / 1024**3, 1)
    best_drive = select_best_cache_drive(discover_drives())
    if best_drive is not None:
        if best_drive.is_system:
            cache_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / _CACHE_DIR_NAME
        else:
            cache_dir = best_drive.mount / _CACHE_DIR_NAME
        label = best_drive.label
    else:
        cache_dir = Path(tempfile.gettempdir()) / _CACHE_DIR_NAME
        label = f"{cache_dir.anchor or str(cache_dir)} (默认临时盘)"
    return HardwareProfile(
        physical_cores=physical,
        logical_cpus=logical,
        total_memory_gb=total_gb,
        available_memory_gb=avail_gb,
        recommended_cache_dir=cache_dir.resolve(),
        cache_drive_label=label,
    )


def configure_gdal_cache(profile: HardwareProfile | None = None) -> int:
    """Tune GDAL_CACHEMAX to ~8% of total RAM (256-2048 MB) unless explicitly set."""
    existing = os.environ.get("GDAL_CACHEMAX", "").strip()
    if existing:
        try:
            return int(existing)
        except ValueError:
            return 0
    total_gb = profile.total_memory_gb if profile is not None else psutil.virtual_memory().total / 1024**3
    cache_mb = max(256, min(2048, int(round(total_gb * 1024 * 0.08))))
    os.environ["GDAL_CACHEMAX"] = str(cache_mb)
    return cache_mb


def build_resource_budget(profile: HardwareProfile, mode: str = "auto") -> ResourceBudget:
    """Translate hardware profile + performance mode into aggressive, RAM-guarded worker limits."""
    phys = profile.physical_cores
    logical = profile.logical_cpus

    if mode == "low":
        eco = max(2, min(6, phys // 2 or 1))
        raster = max(2, min(6, max(1, round(logical * 0.40))))
        return ResourceBudget(
            logical_cpus=max(1, round(logical * 0.50)),
            memory_fraction=0.55,
            ecognition_workers=eco,
            raster_workers=raster,
        )

    if mode == "high":
        # Push up to ~95% CPU and ~92% total RAM while retaining an 8% OS floor.
        safe_headroom_gb = max(
            1.5,
            min(
                profile.total_memory_gb * 0.92,
                profile.available_memory_gb - profile.total_memory_gb * 0.08 + 2.0,
            ),
        )
        mem_cap = max(1, int(safe_headroom_gb / 0.68))
        cpu_cap = max(1, logical - 1 if logical > 2 else logical)
        eco_target = min(32, cpu_cap, max(phys + 2, round(logical * 0.90)))
        eco = max(1, min(eco_target, mem_cap))
        raster = max(1, min(32, cpu_cap))
        return ResourceBudget(
            logical_cpus=logical,
            memory_fraction=0.92,
            ecognition_workers=eco,
            raster_workers=raster,
        )

    # Default "auto" (自动极速 · 动态 90% 调优): target ~90% CPU and ~90% total RAM.
    # Calibrated eCognition 9 usage is ~0.65 GiB/worker; 0.78 GiB/worker preserves burst headroom.
    reserve_gb = profile.total_memory_gb * 0.10
    safe_headroom_gb = max(
        1.6,
        min(
            profile.total_memory_gb * 0.90,
            profile.available_memory_gb - reserve_gb + min(2.0, profile.total_memory_gb * 0.08),
        ),
    )
    mem_cap = max(1, int(safe_headroom_gb / 0.78))
    cpu_90 = max(1, min(32, round(logical * 0.90)))
    eco_target = min(cpu_90, max(phys, round(logical * 0.85)))
    if logical > 2:
        eco_target = min(eco_target, logical - 1)
    eco = max(1, min(eco_target, mem_cap))
    raster = max(1, min(32, cpu_90))
    return ResourceBudget(
        logical_cpus=logical,
        memory_fraction=0.90,
        ecognition_workers=eco,
        raster_workers=raster,
    )


class DynamicAutoTuner:
    """Low-overhead runtime concurrency controller targeting ~90% CPU & RAM safely."""

    def __init__(
        self,
        *,
        initial_workers: int,
        min_workers: int,
        max_workers: int,
        target_cpu_pct: float = 90.0,
        target_ram_pct: float = 90.0,
        sample_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sampler: Callable[[], tuple[float, float]] | None = None,
    ) -> None:
        self.min_workers = max(1, int(min_workers))
        self.max_workers = max(self.min_workers, int(max_workers))
        self.current_workers = max(self.min_workers, min(self.max_workers, int(initial_workers)))
        self.target_cpu_pct = float(target_cpu_pct)
        self.target_ram_pct = float(target_ram_pct)
        self.sample_interval = max(0.5, float(sample_interval))
        self._clock = clock
        self._sampler = sampler or self._default_sample
        self._last_sample_at = self._clock()
        self.adjustments = 0
        self.peak_workers = self.current_workers
        self.min_observed_workers = self.current_workers
        if sampler is None:
            # Prime psutil's non-blocking CPU counter once.
            psutil.cpu_percent(interval=None)

    @staticmethod
    def _default_sample() -> tuple[float, float]:
        return float(psutil.cpu_percent(interval=None)), float(psutil.virtual_memory().percent)

    def recommended_concurrency(self) -> int:
        now = self._clock()
        if (now - self._last_sample_at) < self.sample_interval:
            return self.current_workers
        self._last_sample_at = now
        try:
            cpu_pct, ram_pct = self._sampler()
        except Exception:
            return self.current_workers

        previous = self.current_workers
        if ram_pct >= self.target_ram_pct:
            step = 2 if ram_pct >= self.target_ram_pct + 3.0 else 1
            self.current_workers = max(self.min_workers, self.current_workers - step)
        elif cpu_pct >= min(98.5, self.target_cpu_pct + 6.0) and ram_pct >= self.target_ram_pct - 5.0:
            self.current_workers = max(self.min_workers, self.current_workers - 1)
        elif ram_pct <= self.target_ram_pct - 4.0 and cpu_pct <= self.target_cpu_pct - 3.0:
            self.current_workers = min(self.max_workers, self.current_workers + 1)

        if self.current_workers != previous:
            self.adjustments += 1
            self.peak_workers = max(self.peak_workers, self.current_workers)
            self.min_observed_workers = min(self.min_observed_workers, self.current_workers)
        return self.current_workers

    def summary(self) -> dict[str, int | float]:
        return {
            "target_cpu_pct": self.target_cpu_pct,
            "target_ram_pct": self.target_ram_pct,
            "max_workers": self.max_workers,
            "peak_workers": self.peak_workers,
            "min_observed_workers": self.min_observed_workers,
            "adjustments": self.adjustments,
        }


def adapt_tile_size(
    width: int,
    height: int,
    engine_workers: int,
    requested_tile_size: int = 3072,
    *,
    auto_adapt: bool = True,
) -> int:
    """Use 2048 on medium images when 3072 would leave most workers idle; keep 3072 on full scenes."""
    if not auto_adapt or requested_tile_size != 3072 or engine_workers <= 4:
        return requested_tile_size
    tiles_3072 = math.ceil(width / 3072) * math.ceil(height / 3072)
    if 1 < tiles_3072 < engine_workers and max(width, height) >= 3500:
        tiles_2048 = math.ceil(width / 2048) * math.ceil(height / 2048)
        if tiles_2048 > tiles_3072:
            return 2048
    return requested_tile_size
