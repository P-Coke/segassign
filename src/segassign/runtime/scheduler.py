"""Small resource policy; it selects bounded throughput, not raw CPU count."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import psutil

from ..models import ResourceBudget
from .context import RunContext


class EngineLease:
    """Cross-process lease preventing two UI tasks from racing eCognition."""

    def __init__(self, context: RunContext, path: Path | None = None, poll_seconds: float = 0.25) -> None:
        root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "SegAssign-v2"
        self.path = (path or root / "ecognition.lock").resolve()
        self.context = context
        self.poll_seconds = poll_seconds
        self._stream: IO[bytes] | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"0")
            self._stream.flush()
        reported = False
        while True:
            self.context.checkpoint()
            self._stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if not reported:
                    self.context.emit("waiting", "ecognition", "Waiting for the global eCognition execution lease")
                    reported = True
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


@dataclass(frozen=True, slots=True)
class ResourceScheduler:
    budget: ResourceBudget

    @property
    def memory_limit_bytes(self) -> int:
        return int(psutil.virtual_memory().total * self.budget.memory_fraction)

    def zonal_workers(self, window_count: int, bytes_per_worker: int) -> int:
        cpus = self.budget.logical_cpus
        default_workers = max(1, min(32, round(cpus * 0.90)))
        requested = self.budget.raster_workers or default_workers
        memory_cap = max(1, self.memory_limit_bytes // max(1, bytes_per_worker))
        return max(1, min(window_count, requested, memory_cap))

    def ecognition_workers(self, requested: int | None = None) -> int:
        cpus = self.budget.logical_cpus
        physical = psutil.cpu_count(logical=False) or max(1, cpus // 2)
        default_workers = min(32, max(physical, round(cpus * 0.85)))
        if cpus > 2:
            default_workers = min(default_workers, cpus - 1)
        value = requested or self.budget.ecognition_workers or default_workers
        # Calibrated eCognition 9 usage is ~0.65 GiB per worker; use 0.78 GiB
        # safety allowance so 90% RAM budget scales aggressively without OOM.
        memory_cap = max(1, self.memory_limit_bytes // int(0.78 * 1024**3))
        return max(1, min(value, memory_cap, cpus))

    def ecognition_max_workers(self, requested: int | None = None) -> int:
        """Upper ceiling for dynamic concurrency ramp-up when live RAM/CPU allow."""
        if requested is not None:
            return self.ecognition_workers(requested)
        cpus = self.budget.logical_cpus
        cpu_cap = max(1, min(32, round(cpus * 0.90)))
        if cpus > 2:
            cpu_cap = min(cpu_cap, cpus - 1)
        memory_cap = max(1, self.memory_limit_bytes // int(0.72 * 1024**3))
        return max(self.ecognition_workers(), min(cpu_cap, memory_cap))

    def postprocess_workers(self, requested: int | None = None) -> int:
        cpus = self.budget.logical_cpus
        value = requested or max(2, min(24, round(cpus * 0.90)))
        memory_cap = max(1, self.memory_limit_bytes // (512 * 1024**2))
        return max(1, min(value, memory_cap))

    def prepare_workers(self) -> int:
        return max(2, min(8, round(self.budget.logical_cpus * 0.40)))

    def background_postprocess_workers(self) -> int:
        return max(1, min(6, self.postprocess_workers() // 3))

    def background_zonal_workers(self) -> int:
        return max(2, min(8, round(self.budget.logical_cpus * 0.40)))
