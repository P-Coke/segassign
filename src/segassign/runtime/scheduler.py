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
        default_workers = max(1, min(48, cpus if cpus <= 12 else round(cpus * 0.90)))
        requested = self.budget.raster_workers or default_workers
        memory_cap = max(1, self.memory_limit_bytes // max(1, bytes_per_worker))
        return max(1, min(window_count, requested, memory_cap))

    def ecognition_workers(self, requested: int | None = None) -> int:
        cpus = self.budget.logical_cpus
        physical = psutil.cpu_count(logical=False) or max(1, cpus // 2)
        if physical <= 8 or cpus <= 12:
            default_workers = max(1, min(48, cpus))
        else:
            default_workers = min(48, max(physical, round(cpus * 0.90)))
            if cpus > 2:
                default_workers = min(default_workers, cpus - 1)
        value = requested or self.budget.ecognition_workers or default_workers
        # Calibrated eCognition 9 usage is ~0.65 GiB per worker.
        memory_cap = max(2, self.memory_limit_bytes // int(0.68 * 1024**3))
        return max(1, min(value, memory_cap, cpus))

    def ecognition_max_workers(self, requested: int | None = None) -> int:
        """Upper ceiling for dynamic concurrency ramp-up bounded by mode fraction and RAM."""
        base = self.ecognition_workers(requested)
        if requested is not None:
            return base
        cpus = self.budget.logical_cpus
        mode_cap = max(base, min(48, round(cpus * min(1.0, self.budget.memory_fraction + 0.05))))
        memory_cap = max(2, self.memory_limit_bytes // int(0.65 * 1024**3))
        return max(base, min(mode_cap, memory_cap))

    def postprocess_workers(self, requested: int | None = None) -> int:
        cpus = self.budget.logical_cpus
        default_workers = cpus if cpus <= 12 else round(cpus * 0.90)
        value = requested or max(2, min(32, default_workers))
        memory_cap = max(1, self.memory_limit_bytes // (512 * 1024**2))
        return max(1, min(value, memory_cap))

    def prepare_workers(self) -> int:
        # Low-core hosts stay at 2 threads so eCognition gets maximum CPU; larger hosts use up to 4.
        return 2 if self.budget.logical_cpus <= 12 else max(2, min(4, self.budget.logical_cpus // 4))

    def background_postprocess_workers(self) -> int:
        # 1 worker on low-core hosts, 2 on multi-core hosts: keeps up easily while preserving RAM & CPU.
        return 1 if self.budget.logical_cpus <= 12 else 2

    def background_zonal_workers(self) -> int:
        return max(2, min(6, round(self.budget.logical_cpus * 0.30)))
