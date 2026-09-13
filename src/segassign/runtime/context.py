"""Cancellation, progress events, and per-run resource policy."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ResourceBudget, TaskEvent


class CancelledError(RuntimeError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise CancelledError("task cancelled")


EventSink = Callable[[TaskEvent], None]


class EventQueue:
    """Bounded, non-blocking event sink intended for UI polling."""

    def __init__(self, max_events: int = 2048) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def __call__(self, event: TaskEvent) -> None:
        with self._lock:
            self._events.append(event)

    def drain(self) -> tuple[TaskEvent, ...]:
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
        return events


@dataclass(slots=True)
class RunContext:
    work_dir: Path
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    event_sink: EventSink | None = None
    resources: ResourceBudget = field(default_factory=ResourceBudget)
    resume: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _sequence: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir).expanduser().resolve()

    def checkpoint(self) -> None:
        self.cancellation.checkpoint()

    def emit(self, kind: str, phase: str, message: str, **data) -> TaskEvent:
        with self._lock:
            self._sequence += 1
            event = TaskEvent(self._sequence, kind, phase, message, data, self.task_id)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                # A UI/log consumer is outside the execution boundary and must
                # never turn a successful geospatial stage into a failed run.
                pass
        return event
