"""Background execution of blocking API calls and job polling."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

_POOL: QThreadPool | None = None
# Workers stay referenced until their ``finished`` signal is delivered on the GUI
# thread; otherwise the pool's auto-delete can destroy the signal object before
# the queued ``result`` emission reaches its slot.
_ACTIVE: set[ApiWorker] = set()


def pool() -> QThreadPool:
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(6)
    return _POOL


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(int, int)
    finished = Signal()


class ApiWorker(QRunnable):
    """Run ``fn(*args, **kwargs)`` off the GUI thread and report back via signals."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:
        try:
            if "progress_cb" in self.kwargs and self.kwargs["progress_cb"] is None:
                self.kwargs["progress_cb"] = lambda done, total: self.signals.progress.emit(done, total)
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
            self.signals.error.emit(f"{exc}\n{traceback.format_exc(limit=3)}" if not str(exc) else str(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


def run_async(fn: Callable[..., Any], *args: Any, on_result: Callable[[Any], None] | None = None, on_error: Callable[[str], None] | None = None, on_progress: Callable[[int, int], None] | None = None, on_finished: Callable[[], None] | None = None, **kwargs: Any) -> ApiWorker:
    worker = ApiWorker(fn, *args, **kwargs)
    if on_result:
        worker.signals.result.connect(on_result)
    if on_error:
        worker.signals.error.connect(on_error)
    if on_progress:
        worker.signals.progress.connect(on_progress)
    if on_finished:
        worker.signals.finished.connect(on_finished)
    _ACTIVE.add(worker)
    worker.signals.finished.connect(lambda w=worker: _ACTIVE.discard(w))
    pool().start(worker)
    return worker


TERMINAL = {"completed", "failed", "cancelled"}


class JobWatcher(QObject):
    """Polls one job every second until it reaches a terminal state."""

    progress = Signal(dict)
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, fetch: Callable[[int], dict], job_id: int, interval_ms: int = 1000, parent: QObject | None = None):
        super().__init__(parent)
        self.fetch = fetch
        self.job_id = job_id
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)
        self._busy = False
        self.last: dict = {}

    def start(self) -> None:
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        run_async(self.fetch, self.job_id, on_result=self._on_result, on_error=self._on_error, on_finished=self._done)

    def _done(self) -> None:
        self._busy = False

    def _on_result(self, job: dict) -> None:
        self.last = job
        self.progress.emit(job)
        if job.get("status") in TERMINAL:
            self._timer.stop()
            self.finished.emit(job)

    def _on_error(self, message: str) -> None:
        self._timer.stop()
        self.error.emit(message)
