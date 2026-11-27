"""Runtime support shared by all backends without coupling to any UI."""

from .context import CancellationToken, CancelledError, EventQueue, EventSink, RunContext
from .hardware import (
    DriveInfo,
    HardwareProfile,
    adapt_tile_size,
    build_resource_budget,
    detect_hardware,
    discover_drives,
    select_best_cache_drive,
)
from .metrics import PeakMemoryTracker, StageTimer
from .scheduler import EngineLease, ResourceScheduler
from .workspace import RunWorkspace, cache_usage_bytes, clear_cache, prune_expired_cache

__all__ = [
    "CancellationToken",
    "CancelledError",
    "DriveInfo",
    "EngineLease",
    "EventQueue",
    "EventSink",
    "HardwareProfile",
    "PeakMemoryTracker",
    "ResourceScheduler",
    "RunContext",
    "RunWorkspace",
    "StageTimer",
    "adapt_tile_size",
    "build_resource_budget",
    "cache_usage_bytes",
    "clear_cache",
    "detect_hardware",
    "discover_drives",
    "prune_expired_cache",
    "select_best_cache_drive",
]
