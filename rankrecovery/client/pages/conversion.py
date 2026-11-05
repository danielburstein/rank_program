"""Mode 2: conversion with transparent flag filtering."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.theme import status_colour
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import NoteDialog, error_box, info_box

CONV_COLUMNS = [("display_name", "Player"), ("uuid", "UUID"), ("old_rank", "Old rank"), ("new_rank", "New rank"), ("included", "Included"), ("reasons", "Reasons"), ("inherited_flags", "Flags"), ("excluded_higher_ranks", "Excluded higher"), ("manual_approval", "Manual approval"), ("stale", "Stale"), ("player_id", "ID")]


class ConversionPage(Page):
    title = "Mode 2 · Old-to-new rank conversion"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.checkboxes: dict[str, QCheckBox] = {}
        self.presets: dict[str, list[str]] = {}
        top = QHBoxLayout()
        self.input_mode = QComboBox()
        self.input_mode.addItems(["mode1"])
        self.allow_resolved = QCheckBox("Allow flags a person resolved (auto-resolved flags never block)")
        self.select_all = QPushButton("Select all")
        self.clear_all = QPushButton("Clear all")
        self.preset_combo = QComboBox()
        self.preset_combo.setEditable(True)
        self.preset_combo.setPlaceholderText("preset name")
        self.save_preset = QPushButton("Save preset")
        self.load_preset = QPushButton("Load preset")
        self.save_btn = QPushButton("Save filter settings")
        self.run_btn = QPushButton("Run conversion")
        self.run_btn.setObjectName("primary")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        self.clear_all.clicked.connect(lambda: self._set_all(False))
        self.save_preset.clicked.connect(self._save_preset)
        self.load_preset.clicked.connect(self._load_preset)
        self.save_btn.clicked.connect(self.save_settings)
        self.run_btn.clicked.connect(self.run)
        for w in (QLabel("Input"), self.input_mode, self.select_all, self.clear_all, self.preset_combo, self.save_preset, self.load_preset, self.save_btn, self.run_btn):
            top.addWidget(w)
        top.addStretch(1)
        self.root.addLayout(top)
        self.root.addWidget(self.allow_resolved)
        flags_box = QGroupBox("Allowed flag categories (checked = players carrying this flag may still convert; a failed verification is never bypassed)")
        self.grid = QGridLayout()
        inner = QWidget()
        inner.setLayout(self.grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        box_layout = QHBoxLayout(flags_box)
        box_layout.addWidget(scroll)
        self.summary = QLabel("")
        self.filter_included = QComboBox()
        self.filter_included.addItems(["all", "included", "excluded"])
        self.filter_included.currentIndexChanged.connect(lambda _i: self.load_preview())
        self.table = DataTable(CONV_COLUMNS, colour_fn=lambda r: status_colour("confirmed") if r.get("included") else status_colour("warning"))
        self.table.add_row_action("Open player", lambda r: self.state.open_player.emit(int(r["player_id"])))
        self.approve_btn = QPushButton("Manual approval for selected…")
        self.approve_btn.clicked.connect(self._approve)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Show"))
        bottom.addWidget(self.filter_included)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.approve_btn)
        split = QSplitter(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Orientation.Vertical)
        split.addWidget(flags_box)
        preview_box = QWidget()
        pv = __import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout(preview_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addLayout(bottom)
        pv.addWidget(self.table, 1)
        split.addWidget(preview_box)
        split.setSizes([260, 500])
        self.root.addWidget(split, 1)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("reason for changing the filter (stored in settings history)")
        self.root.addWidget(self.reason)

    def refresh(self) -> None:
        self.call(self.client.flag_catalogue, on_result=self._build_flags)
        self.call(self.client.settings, on_result=self._apply_settings)
        self.load_preview()

    def _build_flags(self, catalogue: list[dict]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.checkboxes = {}
        for i, f in enumerate(sorted(catalogue, key=lambda x: x["code"])):
            box = QCheckBox(f["code"])
            box.setToolTip(f"{f['severity']}: {f['description']}")
            self.grid.addWidget(box, i // 3, i % 3)
            self.checkboxes[f["code"]] = box
        self._apply_values()

    def _apply_settings(self, data: dict) -> None:
        self.values = data.get("values", {})
        self.presets = dict(self.values.get("conversion.presets") or {})
        self.preset_combo.clear()
        self.preset_combo.addItems(sorted(self.presets))
        self.allow_resolved.setChecked(bool(self.values.get("conversion.allow_resolved_flags", False)))
        self.input_mode.setCurrentText(str(self.values.get("conversion.input_mode", "mode1")))
        self._apply_values()

    def _apply_values(self) -> None:
        allowed = set(getattr(self, "values", {}).get("conversion.allowed_flags") or [])
        for code, box in self.checkboxes.items():
            box.setChecked(code in allowed)

    def _set_all(self, checked: bool) -> None:
        for box in self.checkboxes.values():
            box.setChecked(checked)

    def allowed(self) -> list[str]:
        return [code for code, box in self.checkboxes.items() if box.isChecked()]

    def _save_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if not name:
            error_box(self, "Enter a preset name.")
            return
        self.presets[name] = self.allowed()
        self.call(self.client.update_settings, {"conversion.presets": self.presets}, f"save preset {name}", on_result=lambda _r: info_box(self, f"Preset {name} saved."))
        if self.preset_combo.findText(name) < 0:
            self.preset_combo.addItem(name)

    def _load_preset(self) -> None:
        name = self.preset_combo.currentText().strip()
        if name not in self.presets:
            error_box(self, "Unknown preset.")
            return
        for code, box in self.checkboxes.items():
            box.setChecked(code in set(self.presets[name]))

    def save_settings(self) -> None:
        values = {"conversion.allowed_flags": self.allowed(), "conversion.allow_resolved_flags": self.allow_resolved.isChecked(), "conversion.input_mode": self.input_mode.currentText()}
        self.call(self.client.update_settings, values, self.reason.text().strip() or "conversion filter changed", on_result=self._saved)

    def _saved(self, result: dict) -> None:
        if result.get("job_id"):
            self.watch_job(int(result["job_id"]), label="recompute", on_finished=lambda _j: self.load_preview())
        self.status(f"saved: {', '.join(result.get('changed', [])) or 'no change'}")

    def run(self) -> None:
        self.call(self.client.run_conversion, on_result=lambda r: self.watch_job(int(r["job_id"]), label="conversion", on_finished=lambda _j: self.load_preview()))

    def load_preview(self) -> None:
        choice = self.filter_included.currentText()
        included = None if choice == "all" else (choice == "included")
        self.call(self.client.conversion_preview, included, 2000, 0, on_result=self._show)

    def _show(self, rows: list[dict]) -> None:
        self.table.set_rows(rows)
        inc = sum(1 for r in rows if r.get("included"))
        self.summary.setText(f"{inc} included, {len(rows) - inc} excluded (of the rows shown)")

    def _approve(self) -> None:
        row = self.table.current_row()
        if not row:
            return
        box = QCheckBox("approve")
        box.setChecked(not row.get("manual_approval"))
        dialog = NoteDialog("Manual approval", f"{row['display_name']}: manual approval lets this player through the flag filter only. Reason:", parent=self, extra_fields={"": box})
        if dialog.exec():
            self.call(self.client.conversion_approval, int(row["player_id"]), box.isChecked(), dialog.value, on_result=lambda _r: self.load_preview())
