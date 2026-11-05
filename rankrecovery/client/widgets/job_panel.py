"""Jobs table with progress, cancel/pause/resume and error inspection."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from rankrecovery.client.state import AppState
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import error_box
from rankrecovery.client.workers import run_async

JOB_COLUMNS = [("id", "ID"), ("kind", "Kind"), ("label", "Label"), ("status", "Status"), ("percent", "%"), ("done", "Done"), ("total", "Total"), ("failed", "Failed"), ("progress_message", "Progress"), ("created_by", "By"), ("created_at", "Created"), ("finished_at", "Finished"), ("error", "Error")]


class JobPanel(QWidget):
    def __init__(self, state: AppState, *, auto_refresh_ms: int = 3000, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.table = DataTable(JOB_COLUMNS, colour_key="status")
        self.table.row_activated.connect(self.inspect)
        self.refresh_btn = QPushButton("Refresh")
        self.cancel_btn = QPushButton("Cancel")
        self.pause_btn = QPushButton("Pause")
        self.resume_btn = QPushButton("Resume")
        self.inspect_btn = QPushButton("Inspect errors")
        self.refresh_btn.clicked.connect(self.refresh)
        self.cancel_btn.clicked.connect(lambda: self._act("cancel_job"))
        self.pause_btn.clicked.connect(lambda: self._act("pause_job"))
        self.resume_btn.clicked.connect(lambda: self._act("resume_job"))
        self.inspect_btn.clicked.connect(lambda: self.inspect(self.table.current_row() or {}))
        bar = QHBoxLayout()
        for w in (self.refresh_btn, self.cancel_btn, self.pause_btn, self.resume_btn, self.inspect_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.table, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(auto_refresh_ms)
        self.timer.timeout.connect(self.refresh)
        self.auto_refresh = auto_refresh_ms > 0

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        if self.auto_refresh:
            self.timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: ANN001, N802
        super().hideEvent(event)
        self.timer.stop()

    def refresh(self) -> None:
        if self.state.client is None:
            return
        run_async(self.state.client.jobs, 100, on_result=lambda rows: self.table.set_rows(rows), on_error=lambda m: self.state.message.emit(f"jobs: {m}"))

    def _act(self, method: str) -> None:
        row = self.table.current_row()
        if row is None or self.state.client is None:
            return
        run_async(getattr(self.state.client, method), int(row["id"]), on_result=lambda _r: self.refresh(), on_error=lambda m: error_box(self, m))

    def inspect(self, row: dict) -> None:
        if not row or self.state.client is None:
            return
        run_async(self.state.client.job, int(row["id"]), on_result=self._show_job, on_error=lambda m: error_box(self, m))

    def _show_job(self, job: dict) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Job {job.get('id')} — {job.get('kind')} ({job.get('status')})")
        dialog.resize(760, 480)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = [f"label: {job.get('label')}", f"status: {job.get('status')}", f"progress: {job.get('done')}/{job.get('total')} failed={job.get('failed')} {job.get('progress_message', '')}", f"params: {job.get('params')}", f"result: {job.get('result')}", f"error: {job.get('error') or '-'}", "", "errors:"]
        for e in job.get("errors", []):
            lines.append(f"  [{e.get('at')}] {e.get('item')}: {e.get('message')}")
        text.setPlainText("\n".join(lines))
        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        dialog.exec()
