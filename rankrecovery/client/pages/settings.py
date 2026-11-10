"""Settings: every persisted setting, editable with proper widgets."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import ConfirmDialog, NoteDialog, error_box, info_box
from rankrecovery.config.defaults import DEFAULT_SETTINGS, SECRET_SETTINGS

GROUP_TITLES = {
    "ingest": "Bulk submissions (newuploads folder)",
    "drive": "Google Drive",
    "ranks": "Rank lists, aliases, staff prefixes and conversion mapping",
    "verification": "High-rank verification by independent submitters",
    "csv": "Trusted purchase CSV",
    "ai": "AI metadata indicators",
    "dates": "Image date policy",
    "extraction": "Text extraction (vision model and OCR)",
    "images": "Image import",
    "lookup": "UUID lookup and proxies",
    "conversion": "Conversion (Mode 2)",
    "export": "Export (Mode 4)",
    "gui": "GUI preferences",
    "database": "Database",
    "vision": "Vision API",
    "config": "Schema",
}
LABELS = {
    "ingest.folder": "Folder on the server that staff drop screenshots into (relative to the server's working directory)",
    "ingest.submitter": "Submitter recorded for dropped files when the command names none",
    "ingest.skip_folders": "Sub-folder names the ingest passes over (rejected is always skipped)",
    "drive.api_key": "Google API key (optional; lists Drive folders of any size)",
    "lookup.bulk_lookups": "Ask Mojang for ten names per request before single lookups",
    "extraction.vision_concurrency": "Images read at the same time by one extraction job",
    "extraction.vision_region_concurrency": "Crops of one image read at the same time",
    "extraction.vision_max_concurrent_calls": "Cap on model calls in flight across the whole job",
    "extraction.rate_limit_retries": "Retries for an image the API rate-limits (15s, 30s, 60s... between tries)",
    "lookup.history_mode": "History check: fallback = only when nobody holds the name now, always = cross-check every name",
    "lookup.history_pick": "Several accounts used a name: most_recent takes the latest holder, ambiguous asks a person",
    "lookup.rate_limit_cooldown_seconds": "First pause after a 429 without Retry-After (doubles on each repeat)",
    "lookup.rate_limit_max_cooldown_seconds": "Longest single rate-limit pause",
    "lookup.rate_limit_max_retries": "429s tolerated for one request before it is reported failed",
    "lookup.burst": "Requests that may go out back to back before pacing applies",
    "verification.require_two_submitters_above_immortal": "Require 2 independent submitters for ranks above IMMORTAL",
    "csv.treat_as_confirmed_exempt_from_verification": "Treat trusted purchase CSV records as confirmed and exempt from submitter verification",
    "dates.exclude_out_of_date_from_eligibility": "Exclude OUT_OF_DATE evidence from rank eligibility (unchecked = flag only)",
    "ai.filename_indicator_makes_ineligible": "Filename-only AI indicators make evidence ineligible (weak evidence; default off)",
    "verification.unknown_submitter_counts_as_one_source": "All images with an unknown submitter count together as one source",
    "extraction.default_extractor": "Extractor (auto = vision when an API key is configured, else OCR)",
}
CHOICES = {
    "extraction.default_extractor": ["auto", "vision", "ocr"],
    "extraction.vision_effort": ["low", "medium", "high", "xhigh", "max"],
    "extraction.ocr_engine": ["tesseract", "easyocr"],
    "lookup.java_provider": ["mojang", "minecraftservices"],
    "lookup.bedrock_provider": ["geyser", "manual"],
    "lookup.historical_provider": ["crafty", "labynet", "custom", "none"],
    "lookup.history_mode": ["fallback", "always"],
    "lookup.history_pick": ["most_recent", "ambiguous"],
    "lookup.proxy_mode": ["direct", "rotate", "sticky"],
    "conversion.input_mode": ["mode1"],
    "gui.theme": ["dark"],
}
READONLY = {"export.command_template", "config.version"}
SPECIAL = {"ranks.player_ranks", "ranks.staff_prefixes", "ranks.mapping", "verification.per_rank_thresholds", "conversion.presets", "conversion.allowed_flags"}


class ListEditor(QPlainTextEdit):
    """One entry per line. Keeps integer lists (e.g. HTTP status codes) numeric."""

    def __init__(self, values: list[Any]):
        super().__init__()
        self.numeric = bool(values) and all(isinstance(v, int) and not isinstance(v, bool) for v in values)
        self.setPlainText("\n".join(str(v) for v in values))
        self.setFixedHeight(120)

    def value(self) -> list[Any]:
        lines = [line.strip() for line in self.toPlainText().splitlines() if line.strip()]
        if self.numeric:
            return [int(line) if line.lstrip("-").isdigit() else line for line in lines]
        return lines


class RankEditor(QWidget):
    """Table of player ranks ordered lowest to highest."""

    def __init__(self, ranks: list[dict]):
        super().__init__()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Rank", "Aliases (comma separated)", "Enabled"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for r in ranks:
            self._add(r.get("name", ""), ", ".join(r.get("aliases", [])), bool(r.get("enabled", True)))
        bar = QHBoxLayout()
        for label, fn in (("Add", lambda: self._add("NEWRANK", "", True)), ("Remove", self._remove), ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1))):
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            bar.addWidget(btn)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Lowest rank first. IMMORTAL and IMMORTAL+ stay distinct; aliases must not match two ranks."))
        layout.addWidget(self.table)
        layout.addLayout(bar)

    def _add(self, name: str, aliases: str, enabled: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(aliases))
        box = QCheckBox()
        box.setChecked(enabled)
        self.table.setCellWidget(row, 2, box)

    def _remove(self) -> None:
        for index in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(index)

    def _move(self, direction: int) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        row = rows[0]
        target = row + direction
        if target < 0 or target >= self.table.rowCount():
            return
        values = self.value()
        values[row], values[target] = values[target], values[row]
        self.table.setRowCount(0)
        for r in values:
            self._add(r["name"], ", ".join(r["aliases"]), r["enabled"])
        self.table.selectRow(target)

    def value(self) -> list[dict]:
        out = []
        for row in range(self.table.rowCount()):
            name = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip().upper()
            aliases = [a.strip().upper() for a in (self.table.item(row, 1).text() if self.table.item(row, 1) else "").split(",") if a.strip()]
            widget = self.table.cellWidget(row, 2)
            enabled = widget.isChecked() if isinstance(widget, QCheckBox) else True
            if name:
                out.append({"name": name, "aliases": aliases, "enabled": enabled})
        return out


class StaffEditor(QWidget):
    def __init__(self, staff: list[dict]):
        super().__init__()
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Staff prefix", "Equivalent spellings (comma separated)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        for s in staff:
            self._add(s.get("name", ""), ", ".join(s.get("aliases", [])))
        bar = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._add("NEW", ""))
        rem = QPushButton("Remove")
        rem.clicked.connect(self._remove)
        bar.addWidget(add)
        bar.addWidget(rem)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)
        layout.addLayout(bar)

    def _add(self, name: str, aliases: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(aliases))

    def _remove(self) -> None:
        for index in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(index)

    def value(self) -> list[dict]:
        out = []
        for row in range(self.table.rowCount()):
            name = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip().upper()
            aliases = [a.strip().upper() for a in (self.table.item(row, 1).text() if self.table.item(row, 1) else "").split(",") if a.strip()]
            if name:
                out.append({"name": name, "aliases": aliases})
        return out


class MappingEditor(QWidget):
    """Old rank -> new group, exact spelling preserved. Empty = unmapped."""

    def __init__(self, mapping: dict[str, str], rank_names: list[str], *, value_label: str = "New group (exact spelling)", key_label: str = "Old rank", numeric: bool = False):
        super().__init__()
        self.numeric = numeric
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([key_label, value_label])
        self.table.horizontalHeader().setStretchLastSection(True)
        names = list(rank_names) + [k for k in mapping if k not in rank_names]
        for name in names:
            row = self.table.rowCount()
            self.table.insertRow(row)
            key_item = QTableWidgetItem(name)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, key_item)
            self.table.setItem(row, 1, QTableWidgetItem("" if mapping.get(name) is None else str(mapping.get(name))))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)

    def value(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 0).text().strip()
            raw = (self.table.item(row, 1).text() if self.table.item(row, 1) else "").strip()
            if not raw:
                continue
            out[key] = int(raw) if self.numeric and raw.isdigit() else raw
        return out


class SettingsPage(Page):
    title = "Settings"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.widgets: dict[str, QWidget] = {}
        self.values: dict[str, Any] = {}
        self.data: dict = {}
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        bar = QHBoxLayout()
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("reason for this change (kept in settings history)")
        self.save_btn = QPushButton("Save changes")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self.refresh)
        bar.addWidget(self.reason, 1)
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.save_btn)
        self.root.addLayout(bar)
        self.history = DataTable([("changed_at", "At"), ("key", "Key"), ("old", "Old"), ("new", "New"), ("changed_by", "By"), ("reason", "Reason")])
        self.versions = DataTable([("kind", "Kind"), ("version", "Version"), ("fingerprint", "Fingerprint"), ("created_at", "At"), ("created_by", "By"), ("reason", "Reason")])

    # ------------------------------------------------------------------ load
    def refresh(self) -> None:
        self.call(self.client.settings, on_result=self._build)
        self.call(self.client.settings_history, None, 300, on_result=self.history.set_rows, on_error=lambda _m: None)
        self.call(self.client.policy_versions, None, 100, on_result=self.versions.set_rows, on_error=lambda _m: None)

    def build_from(self, data: dict) -> None:
        """Build the form from a /settings payload (also used by tests)."""
        self._build(data)

    def _build(self, data: dict) -> None:
        self.data = data
        self.values = dict(data.get("values", {}))
        self.widgets = {}
        while self.tabs.count():
            self.tabs.removeTab(0)
        groups: dict[str, list[str]] = {}
        keys = list(DEFAULT_SETTINGS) + [k for k in self.values if k not in DEFAULT_SETTINGS]
        for key in keys:
            groups.setdefault(key.split(".", 1)[0], []).append(key)
        if "vision" not in groups:
            groups["vision"] = []
        order = ["ranks", "verification", "csv", "ai", "dates", "extraction", "vision", "images", "lookup", "conversion", "export", "gui", "database", "config"]
        for group in order + [g for g in groups if g not in order]:
            if group not in groups:
                continue
            self.tabs.addTab(self._group_widget(group, groups[group]), GROUP_TITLES.get(group, group))
        self.tabs.addTab(self.history, "Change history")
        self.tabs.addTab(self.versions, "Policy versions")

    def _group_widget(self, group: str, keys: list[str]) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        rank_names = [r["name"] for r in (self.values.get("ranks.player_ranks") or [])]
        for key in keys:
            if key in SPECIAL and key != "conversion.allowed_flags":
                continue
            widget = self._widget_for(key, self.values.get(key, DEFAULT_SETTINGS.get(key)))
            if widget is None:
                continue
            self.widgets[key] = widget
            form.addRow(LABELS.get(key, key.split(".", 1)[1].replace("_", " ")), widget)
        if group == "ranks":
            self.widgets["ranks.player_ranks"] = RankEditor(self.values.get("ranks.player_ranks") or [])
            self.widgets["ranks.staff_prefixes"] = StaffEditor(self.values.get("ranks.staff_prefixes") or [])
            self.widgets["ranks.mapping"] = MappingEditor(self.values.get("ranks.mapping") or {}, rank_names)
            for title, w in (("Player ranks (lowest to highest)", self.widgets["ranks.player_ranks"]), ("Staff prefixes", self.widgets["ranks.staff_prefixes"]), ("Old rank → new LuckPerms group (DEFAULT deliberately unmapped)", self.widgets["ranks.mapping"])):
                box = QGroupBox(title)
                QVBoxLayout(box).addWidget(w)
                outer.addWidget(box)
        if group == "verification":
            self.widgets["verification.per_rank_thresholds"] = MappingEditor({k: v for k, v in (self.values.get("verification.per_rank_thresholds") or {}).items()}, rank_names, value_label="Required independent sources (blank = default rule)", numeric=True)
            box = QGroupBox("Per-rank source thresholds")
            QVBoxLayout(box).addWidget(self.widgets["verification.per_rank_thresholds"])
            outer.addWidget(box)
        if group == "vision":
            self.api_key = QLineEdit()
            self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
            configured = (self.data.get("secrets_configured") or {}).get("vision.anthropic_api_key")
            self.api_key.setPlaceholderText("configured (enter a new key to replace)" if configured else "not configured: paste your Anthropic API key")
            self.widgets["vision.anthropic_api_key"] = self.api_key
            form.addRow("Anthropic API key", self.api_key)
            form.addRow("", QLabel("The key is stored on the server, never shown again and never logged. ANTHROPIC_API_KEY in the server environment overrides it."))
        if group == "database":
            self.backup_btn = QPushButton("Backup now")
            self.backup_btn.clicked.connect(self._backup)
            self.backups = DataTable([("path", "Backup file"), ("size", "Bytes"), ("modified", "Modified")])
            self.restore_btn = QPushButton("Restore selected backup…")
            self.restore_btn.setObjectName("danger")
            self.restore_btn.clicked.connect(self._restore)
            self.refresh_backups_btn = QPushButton("Refresh list")
            self.refresh_backups_btn.clicked.connect(self._load_backups)
            row = QHBoxLayout()
            for b in (self.backup_btn, self.refresh_backups_btn, self.restore_btn):
                row.addWidget(b)
            row.addStretch(1)
            outer.addLayout(row)
            outer.addWidget(self.backups)
            outer.addWidget(QLabel("Backups use SQLite's online backup API and are safe while the server runs. Restore replaces the live database; stop jobs first and restart the server afterwards."))
        if group == "export":
            outer.addWidget(QLabel("Warning: 'lp user <uuid> parent set <group>' replaces a player's existing parent groups, including staff groups. The format is fixed by the migration requirement."))
        outer.insertLayout(0, form)
        reset = QPushButton(f"Restore defaults for '{group}'…")
        reset.clicked.connect(lambda _c, g=group: self._reset(g))
        outer.addWidget(reset)
        outer.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _widget_for(self, key: str, value: Any) -> QWidget | None:
        if key == "drive.api_key":
            editor = QLineEdit()
            editor.setEchoMode(QLineEdit.EchoMode.Password)
            configured = (self.data.get("secrets_configured") or {}).get(key)
            editor.setPlaceholderText("configured (enter a new key to replace)" if configured else "optional: a Google API key with the Drive API enabled, for Drive folders over 50 files")
            return editor
        if key in SECRET_SETTINGS:
            if key == "lookup.proxy_list":
                editor = QPlainTextEdit()
                editor.setPlaceholderText("one proxy per line: http://user:pass@host:port, host:port, host:port:user:pass … (stored on the server; never shown again)")
                editor.setFixedHeight(100)
                load = QPushButton("Load from TXT…")
                load.clicked.connect(lambda: self._load_proxy_file(editor))
                holder = QWidget()
                lay = QVBoxLayout(holder)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.addWidget(editor)
                lay.addWidget(load)
                holder.editor = editor  # type: ignore[attr-defined]
                return holder
            return None
        if key in CHOICES:
            combo = QComboBox()
            combo.addItems(CHOICES[key])
            combo.setCurrentText(str(value))
            return combo
        if key == "dates.cutoff":
            edit = QDateEdit()
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            try:
                d = date.fromisoformat(str(value))
                edit.setDate(QDate(d.year, d.month, d.day))
            except ValueError:
                edit.setDate(QDate(2026, 1, 1))
            return edit
        if isinstance(value, bool):
            box = QCheckBox()
            box.setChecked(value)
            return box
        if isinstance(value, int):
            spin = QSpinBox()
            spin.setRange(0, 10_000_000)
            spin.setValue(value)
            return spin
        if isinstance(value, float):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1_000_000.0)
            spin.setDecimals(3)
            spin.setValue(value)
            return spin
        if isinstance(value, list):
            return ListEditor(value)
        if isinstance(value, dict):
            editor = QPlainTextEdit(json.dumps(value, indent=2))
            editor.setFixedHeight(100)
            return editor
        edit = QLineEdit("" if value is None else str(value))
        if key in READONLY:
            edit.setReadOnly(True)
        return edit

    def _value_of(self, key: str, widget: QWidget) -> Any:
        if hasattr(widget, "editor"):
            return widget.editor.toPlainText()  # type: ignore[attr-defined]
        if isinstance(widget, RankEditor | StaffEditor | MappingEditor | ListEditor):
            return widget.value()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentText()
        if isinstance(widget, QDateEdit):
            return widget.date().toString("yyyy-MM-dd")
        if isinstance(widget, QSpinBox | QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, QPlainTextEdit):
            text = widget.toPlainText()
            try:
                return json.loads(text) if text.strip() else {}
            except ValueError:
                return text
        if isinstance(widget, QLineEdit):
            text = widget.text()
            default = DEFAULT_SETTINGS.get(key)
            if isinstance(default, int) and not isinstance(default, bool) and text.strip().lstrip("-").isdigit():
                return int(text)
            return text
        return None

    def collect(self) -> dict[str, Any]:
        """Values that differ from what the server reported."""
        changed: dict[str, Any] = {}
        for key, widget in self.widgets.items():
            value = self._value_of(key, widget)
            if key in SECRET_SETTINGS:
                if value and value != "***":
                    changed[key] = value
                continue
            if key in READONLY:
                continue
            if json.dumps(value, sort_keys=True) != json.dumps(self.values.get(key, DEFAULT_SETTINGS.get(key)), sort_keys=True):
                changed[key] = value
        return changed

    def save(self) -> None:
        changed = self.collect()
        if not changed:
            info_box(self, "No changes to save.")
            return
        self.call(self.client.update_settings, changed, self.reason.text().strip() or "settings edited in desktop client", on_result=self._saved)

    def _saved(self, result: dict) -> None:
        text = f"Saved: {', '.join(result.get('changed', [])) or 'nothing changed'}"
        if result.get("job_id"):
            text += f"\nRank summaries were marked stale and recompute job {result['job_id']} started."
            self.watch_job(int(result["job_id"]), label="recompute")
        info_box(self, text)
        if hasattr(self, "api_key"):
            self.api_key.clear()
        self.refresh()

    def _reset(self, group: str) -> None:
        dialog = ConfirmDialog("Restore defaults", f"Restore every '{group}' setting to its default? History is kept and rank summaries will be recomputed.", parent=self, typed_confirmation="RESET")
        if dialog.exec():
            self.call(self.client.reset_settings, None, f"{group}.", on_result=self._saved)

    def _load_proxy_file(self, editor: QPlainTextEdit) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Proxy list", "", "Text files (*.txt);;All files (*)")
        if file:
            try:
                editor.setPlainText(open(file, encoding="utf-8").read())
            except OSError as exc:
                error_box(self, str(exc))

    def _backup(self) -> None:
        self.call(self.client.backup, "", on_result=lambda r: (info_box(self, f"Backup written: {r.get('path')}"), self._load_backups()))

    def _load_backups(self) -> None:
        self.call(self.client.backups, on_result=self.backups.set_rows)

    def _restore(self) -> None:
        row = self.backups.current_row()
        if not row:
            error_box(self, "Select a backup first.")
            return
        first = ConfirmDialog("Restore database", f"Replace the LIVE database with {row['path']}? Everything imported since that backup will be lost.", parent=self, typed_confirmation="RESTORE")
        if not first.exec():
            return
        note = NoteDialog.ask(self, "Second confirmation", "Confirm once more. The server must be restarted afterwards.")
        if note:
            self.call(self.client.restore, row["path"], on_result=lambda r: info_box(self, f"{r.get('restored')}\n{r.get('note')}"))
