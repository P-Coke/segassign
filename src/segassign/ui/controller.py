"""Qt thread adapter; backend services remain unaware of the UI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..models import ResourceBudget
from ..runtime import CancellationToken, EventQueue, RunContext


class _BackendWorker(QObject):
    completed = Signal(object)
    crashed = Signal(str)

    def __init__(self, run: Callable, request, context: RunContext) -> None:
        super().__init__()
        self._run = run
        self._request = request
        self._context = context

    @Slot()
    def execute(self) -> None:
        try:
            self.completed.emit(self._run(self._request, self._context))
        except BaseException as error:
            self.crashed.emit(f"{type(error).__name__}: {error}")


class TaskController(QObject):
    event_received = Signal(dict)
    result_ready = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _BackendWorker | None = None
        self._cancellation: CancellationToken | None = None
        self._events = EventQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._drain_events)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(
        self,
        run: Callable,
        request,
        work_dir: Path,
        resume: bool,
        resources: ResourceBudget | None = None,
        cleanup_on_success: bool = True,
    ) -> None:
        if self.running:
            raise RuntimeError("a task is already running")
        self._events.drain()
        self._cancellation = CancellationToken()
        context = RunContext(
            work_dir=work_dir,
            cancellation=self._cancellation,
            event_sink=self._events,
            resources=resources or ResourceBudget(),
            resume=resume,
            cleanup_on_success=cleanup_on_success,
        )
        thread = QThread(self)
        worker = _BackendWorker(run, request, context)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.completed.connect(self._on_completed)
        worker.crashed.connect(self._on_failed)
        worker.completed.connect(thread.quit)
        worker.crashed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        self._timer.start()
        self.running_changed.emit(True)
        thread.start()

    @Slot()
    def cancel(self) -> None:
        if self._cancellation is not None:
            self._cancellation.cancel()

    @Slot()
    def _drain_events(self) -> None:
        for event in self._events.drain():
            self.event_received.emit(event.serializable())

    @Slot(object)
    def _on_completed(self, result) -> None:
        self._drain_events()
        self.result_ready.emit(result)

    @Slot(str)
    def _on_failed(self, detail: str) -> None:
        self._drain_events()
        self.failed.emit(detail)

    @Slot()
    def _on_thread_finished(self) -> None:
        self._timer.stop()
        self._drain_events()
        thread = self._thread
        self._thread = None
        self._worker = None
        self._cancellation = None
        self.running_changed.emit(False)
        if thread is not None:
            thread.deleteLater()
