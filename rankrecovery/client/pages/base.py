"""Common page scaffolding."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from rankrecovery.client.api_client import ServerClient
from rankrecovery.client.state import AppState
from rankrecovery.client.widgets.forms import error_box
from rankrecovery.client.workers import JobWatcher, run_async


class Page(QWidget):
    title = "Page"

    def __init__(self, state: AppState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self._watchers: list[JobWatcher] = []
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(12, 10, 12, 10)
        header = QLabel(self.title)
        header.setObjectName("title")
        self.root.addWidget(header)
        self._loaded = False

    @property
    def client(self) -> ServerClient:
        return self.state.require()

    def call(self, fn: Callable[..., Any], *args: Any, on_result: Callable[[Any], None] | None = None, on_error: Callable[[str], None] | None = None, **kwargs: Any) -> None:
        """Run an API call off-thread; errors go to a dialog unless handled."""
        if self.state.client is None:
            error_box(self, "Not connected to a server.")
            return
        run_async(fn, *args, on_result=on_result, on_error=on_error or (lambda m: error_box(self, m)), **kwargs)

    def watch_job(self, job_id: int, *, on_progress: Callable[[dict], None] | None = None, on_finished: Callable[[dict], None] | None = None, label: str = "") -> JobWatcher:
        watcher = JobWatcher(self.client.job, job_id, parent=self)
        if on_progress:
            watcher.progress.connect(on_progress)
        if on_finished:
            watcher.finished.connect(on_finished)
        watcher.error.connect(lambda m: self.state.message.emit(f"job {job_id}: {m}"))
        watcher.start()
        self._watchers.append(watcher)
        self.state.job_started.emit(job_id, label)
        return watcher

    def refresh(self) -> None:  # pragma: no cover - overridden
        return None

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        if not self._loaded and self.state.client is not None:
            self._loaded = True
            self.refresh()

    def status(self, text: str) -> None:
        self.state.message.emit(text)
