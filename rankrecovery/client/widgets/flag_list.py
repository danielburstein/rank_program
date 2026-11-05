"""Active vs resolved flags with resolve/reopen actions."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import NoteDialog, error_box

FLAG_COLUMNS = [("id", "ID"), ("code", "Code"), ("severity", "Severity"), ("details", "Details"), ("source", "Source"), ("evidence_ref", "Evidence"), ("created_by", "By"), ("created_at", "Created"), ("resolved_by", "Resolved by"), ("resolution_note", "Resolution note")]


class FlagList(QWidget):
    """Two tables: active flags and resolved flags. Actions call back into the page."""

    def __init__(self, *, on_resolve: Callable[[int, str], None] | None = None, on_reopen: Callable[[int, str], None] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.on_resolve = on_resolve
        self.on_reopen = on_reopen
        self.active = DataTable(FLAG_COLUMNS, colour_key="severity")
        self.resolved = DataTable(FLAG_COLUMNS, colour_fn=lambda _r: __import__("rankrecovery.client.theme", fromlist=["status_colour"]).status_colour("resolved"))
        self.resolve_btn = QPushButton("Resolve selected…")
        self.reopen_btn = QPushButton("Reopen selected…")
        self.resolve_btn.clicked.connect(self._resolve)
        self.reopen_btn.clicked.connect(self._reopen)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("Active flags"))
        head.addStretch(1)
        head.addWidget(self.resolve_btn)
        layout.addLayout(head)
        layout.addWidget(self.active, 2)
        head2 = QHBoxLayout()
        head2.addWidget(QLabel("Resolved / historical flags"))
        head2.addStretch(1)
        head2.addWidget(self.reopen_btn)
        layout.addLayout(head2)
        layout.addWidget(self.resolved, 1)

    def set_flags(self, flags: list[dict]) -> None:
        self.active.set_rows([f for f in flags if f.get("is_active")])
        self.resolved.set_rows([f for f in flags if not f.get("is_active")])

    def _resolve(self) -> None:
        rows = self.active.selected_rows()
        if not rows:
            error_box(self, "Select at least one active flag.")
            return
        note = NoteDialog.ask(self, "Resolve flag", f"Resolving {len(rows)} flag(s). The flag stays in history as resolved.")
        if note and self.on_resolve:
            for row in rows:
                self.on_resolve(int(row["id"]), note)

    def _reopen(self) -> None:
        rows = self.resolved.selected_rows()
        if not rows:
            error_box(self, "Select at least one resolved flag.")
            return
        note = NoteDialog.ask(self, "Reopen flag", f"Reopening {len(rows)} flag(s).")
        if note and self.on_reopen:
            for row in rows:
                self.on_reopen(int(row["id"]), note)
