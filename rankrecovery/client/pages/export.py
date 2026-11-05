"""Mode 4: LuckPerms command export."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.theme import mono_font, status_colour
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import error_box, info_box

ROW_COLUMNS = [("display_name", "Player"), ("uuid", "UUID"), ("old_rank", "Old rank"), ("new_rank", "New rank"), ("included", "Included"), ("skip_reason", "Skip reason"), ("command", "Command"), ("player_id", "ID")]
HISTORY_COLUMNS = [("id", "Batch"), ("created_at", "At"), ("created_by", "By"), ("filename", "File"), ("file_path", "Server path"), ("command_count", "Commands"), ("skipped_count", "Skipped"), ("stale", "Stale"), ("stale_reason", "Stale reason"), ("policy_fingerprint", "Policy"), ("mapping_fingerprint", "Mapping")]


class ExportPage(Page):
    title = "Mode 4 · LuckPerms export"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.preview_data: dict = {}
        self.banner = QLabel("")
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        self.root.addWidget(self.banner)
        bar = QHBoxLayout()
        self.preview_btn = QPushButton("Refresh preview")
        self.preview_btn.clicked.connect(self.refresh)
        self.ack = QCheckBox("I understand that 'lp user <uuid> parent set <group>' REPLACES existing parent groups (including staff groups)")
        self.path = QLineEdit()
        self.path.setPlaceholderText("output file (local)")
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        self.write_btn = QPushButton("Write TXT")
        self.write_btn.setObjectName("primary")
        self.write_btn.clicked.connect(self.write)
        bar.addWidget(self.preview_btn)
        bar.addWidget(self.path, 1)
        bar.addWidget(self.browse_btn)
        bar.addWidget(self.write_btn)
        self.root.addLayout(bar)
        self.root.addWidget(self.ack)
        self.summary = QLabel("")
        self.root.addWidget(self.summary)
        tabs = QTabWidget()
        self.commands = QPlainTextEdit()
        self.commands.setReadOnly(True)
        self.commands.setFont(mono_font())
        self.included = DataTable(ROW_COLUMNS, colour_fn=lambda _r: status_colour("confirmed"))
        self.skipped = DataTable(ROW_COLUMNS, colour_fn=lambda _r: status_colour("warning"))
        for t in (self.included, self.skipped):
            t.add_row_action("Open player", lambda r: self.state.open_player.emit(int(r["player_id"])))
        self.history = DataTable(HISTORY_COLUMNS, colour_fn=lambda r: status_colour("stale") if r.get("stale") else None)
        self.history.row_activated.connect(self._open_batch)
        tabs.addTab(self.commands, "Commands")
        tabs.addTab(self.included, "Included")
        tabs.addTab(self.skipped, "Skipped (with reasons)")
        tabs.addTab(self.history, "Export history")
        split = QSplitter()
        split.addWidget(tabs)
        self.root.addWidget(split, 1)

    def refresh(self) -> None:
        self.call(self.client.export_preview, on_result=self._show)
        self.call(self.client.export_history, 100, on_result=self.history.set_rows)

    def _show(self, data: dict) -> None:
        self.preview_data = data
        warnings = data.get("warnings") or []
        if warnings:
            self.banner.setText("\n".join(warnings))
            self.banner.show()
        else:
            self.banner.hide()
        stale = data.get("stale_exports") or []
        self.summary.setText(f"{data.get('command_count', 0)} commands · {data.get('skipped_count', 0)} skipped" + (f" · {len(stale)} earlier export(s) are now outdated: {stale}" if stale else ""))
        cmds = data.get("commands") or []
        self.commands.setPlainText("\n".join(cmds[:500]) + ("\n…" if len(cmds) > 500 else ""))
        self.included.set_rows(data.get("included", []))
        self.skipped.set_rows(data.get("skipped", []))

    def _browse(self) -> None:
        file, _ = QFileDialog.getSaveFileName(self, "Save LuckPerms commands", "luckperms_commands.txt", "Text files (*.txt)")
        if file:
            self.path.setText(file)

    def write(self) -> None:
        if self.preview_data.get("warnings") and not self.ack.isChecked():
            error_box(self, "Acknowledge the LuckPerms parent-set warning first.")
            return
        target = self.path.text().strip()
        if not target:
            error_box(self, "Choose an output file.")
            return
        self.call(self.client.export_write, None, self.ack.isChecked(), {"client_path": target}, on_result=lambda r: self._written(r, Path(target)))

    def _written(self, result: dict, target: Path) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result.get("text", ""), encoding="utf-8", newline="\n")
        except OSError as exc:
            error_box(self, f"server recorded export batch {result.get('batch_id')} but the file could not be written locally: {exc}")
            return
        info_box(self, f"Wrote {result.get('command_count')} commands to {target} (export batch {result.get('batch_id')}, {result.get('skipped_count')} skipped).")
        self.refresh()

    def _open_batch(self, row: dict) -> None:
        self.call(self.client.export_detail, int(row["id"]), on_result=lambda d: self.commands.setPlainText(f"# export batch {row['id']} (stale={d.get('stale')} {d.get('stale_reason', '')})\n" + (d.get("content") or "")))
