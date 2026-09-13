"""Low-overhead process metrics collected uniformly for every backend run."""

from __future__ import annotations

import threading
import time
from contextlib import AbstractContextManager

import psutil


class PeakMemoryTracker(AbstractContextManager["PeakMemoryTracker"]):
    """Sample this process and its children without holding up useful work."""

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        process = psutil.Process()
        while not self._stop.is_set():
            try:
                current = process.memory_info().rss
                current += sum(child.memory_info().rss for child in process.children(recursive=True))
                self.peak_bytes = max(self.peak_bytes, current)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            self._stop.wait(self.interval)

    @property
    def peak_mb(self) -> float:
        return round(self.peak_bytes / 1024**2, 2)

    def __enter__(self) -> PeakMemoryTracker:
        self._thread = threading.Thread(target=self._sample, name="memory-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 2))


class StageTimer:
    def __init__(self, timings: dict[str, float], name: str) -> None:
        self.timings = timings
        self.name = name
        self.started = 0.0

    def __enter__(self) -> StageTimer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        elapsed = time.perf_counter() - self.started
        self.timings[self.name] = round(self.timings.get(self.name, 0.0) + elapsed, 6)
