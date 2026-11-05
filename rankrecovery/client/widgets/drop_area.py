"""Drag-and-drop target for files and folders."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class DropArea(QFrame):
    paths_dropped = Signal(list)

    def __init__(self, text: str = "Drop screenshots or folders here", parent=None):  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        layout = QVBoxLayout(self)
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName("dim")
        layout.addWidget(label)

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.mimeData().hasUrls():
            self.setProperty("active", "true")
            self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self.setProperty("active", "false")
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # noqa: ANN001, N802
        self.setProperty("active", "false")
        self.style().polish(self)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
        event.acceptProposedAction()
