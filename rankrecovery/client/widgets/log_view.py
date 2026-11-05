"""Server log tail."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.state import AppState
from rankrecovery.client.theme import mono_font
from rankrecovery.client.workers import run_async


class LogView(QWidget):
    def __init__(self, state: AppState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state
        self.level = QComboBox()
        self.level.addItems(["ALL", "INFO", "WARNING", "ERROR"])
        self.limit = QSpinBox()
        self.limit.setRange(50, 2000)
        self.limit.setValue(300)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font())
        bar = QHBoxLayout()
        bar.addWidget(self.level)
        bar.addWidget(self.limit)
        bar.addWidget(self.refresh_btn)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.text, 1)

    def refresh(self) -> None:
        if self.state.client is None:
            return
        level = None if self.level.currentText() == "ALL" else self.level.currentText()
        run_async(self.state.client.logs, self.limit.value(), level, on_result=self._fill, on_error=lambda m: self.text.setPlainText(m))

    def _fill(self, records: list[dict]) -> None:
        lines = [f"{r.get('time', '')[:19]} {r.get('level', ''):8} {r.get('logger', '')}: {r.get('message', '')}" + (f"\n{r['exception']}" if r.get("exception") else "") for r in records]
        self.text.setPlainText("\n".join(lines))
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())
