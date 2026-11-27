"""Cancellation, progress events, and per-run resource policy."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

    def __init__(self, max_events: int = 8192) -> None:
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
    cleanup_on_success: bool = False
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    default_event_data: Mapping[str, Any] = field(default_factory=dict)
    _sequence_ref: list[int] = field(default_factory=lambda: [0], init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.work_dir = Path(self.work_dir).expanduser().resolve()
        self.default_event_data = dict(self.default_event_data)

    def scoped(self, *, resources: ResourceBudget | None = None, **extra_event_data: Any) -> RunContext:
        """Create a child context sharing cancellation, sequence lock, and sink with extra event tags."""
        merged = {**self.default_event_data, **extra_event_data}
        child = RunContext(
            work_dir=self.work_dir,
            cancellation=self.cancellation,
            event_sink=self.event_sink,
            resources=resources or self.resources,
            resume=self.resume,
            cleanup_on_success=self.cleanup_on_success,
            task_id=self.task_id,
            default_event_data=merged,
        )
        child._sequence_ref = self._sequence_ref
        child._lock = self._lock
        return child

    def checkpoint(self) -> None:
        self.cancellation.checkpoint()

    def emit(self, kind: str, phase: str, message: str, **data) -> TaskEvent:
        payload = {**self.default_event_data, **data} if self.default_event_data else data
        with self._lock:
            self._sequence_ref[0] += 1
            event = TaskEvent(self._sequence_ref[0], kind, phase, message, payload, self.task_id)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                # A UI/log consumer is outside the execution boundary and must
                # never turn a successful geospatial stage into a failed run.
                pass
        return event
