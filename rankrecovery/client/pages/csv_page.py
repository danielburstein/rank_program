"""Trusted purchase CSV import."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import NoteDialog, error_box, info_box

FIELDS = ["username", "uuid", "rank", "purchase_date", "transaction_id"]
PREVIEW_COLUMNS = [("row_number", "Row"), ("status", "Status"), ("username", "Username"), ("uuid", "UUID"), ("rank_raw", "Rank raw"), ("rank", "Rank"), ("purchase_date", "Purchased"), ("transaction_id", "Transaction"), ("errors", "Errors"), ("warnings", "Warnings")]
BATCH_COLUMNS = [("id", "ID"), ("filename", "File"), ("source", "Source"), ("row_count", "Rows"), ("valid_count", "Valid"), ("rejected_count", "Rejected"), ("duplicate_count", "Duplicates"), ("imported_by", "By"), ("imported_at", "At"), ("notes", "Notes")]
ROW_COLUMNS = [("csv_row_id", "ID"), ("row_number", "Row"), ("username", "Username"), ("uuid", "UUID"), ("rank_raw", "Rank raw"), ("rank", "Rank"), ("purchase_date", "Purchased"), ("transaction_id", "Transaction"), ("validation_status", "Validation"), ("validation_errors", "Errors"), ("player_id", "Player"), ("is_active", "Active"), ("deactivated_reason", "Deactivated")]


class CsvPage(Page):
    title = "Trusted purchase CSV"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.path: Path | None = None
        self.preview_data: dict = {}
        tabs = QTabWidget()
        # --- import tab
        import_tab = QWidget()
        layout = QVBoxLayout(import_tab)
        bar = QHBoxLayout()
        self.file_label = QLabel("no file selected")
        self.choose_btn = QPushButton("Choose CSV…")
        self.choose_btn.clicked.connect(self._choose)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.clicked.connect(self.preview)
        self.delimiter = QComboBox()
        self.delimiter.addItems(["auto", ",", ";", "tab", "|"])
        bar.addWidget(self.choose_btn)
        bar.addWidget(self.file_label, 1)
        bar.addWidget(QLabel("delimiter"))
        bar.addWidget(self.delimiter)
        bar.addWidget(self.preview_btn)
        layout.addLayout(bar)
        mapping_box = QGroupBox("Header mapping (auto-detected, editable)")
        self.mapping_form = QFormLayout(mapping_box)
        self.mapping_combos: dict[str, QComboBox] = {}
        for field in FIELDS:
            combo = QComboBox()
            combo.addItem("(not mapped)")
            self.mapping_form.addRow(field, combo)
            self.mapping_combos[field] = combo
        layout.addWidget(mapping_box)
        self.summary_label = QLabel("")
        layout.addWidget(self.summary_label)
        self.preview_table = DataTable(PREVIEW_COLUMNS, colour_key="status")
        layout.addWidget(self.preview_table, 1)
        commit_bar = QHBoxLayout()
        self.reject_invalid = QCheckBox("Import valid rows and reject invalid ones (unchecked = refuse the whole file if any row is invalid)")
        self.reject_invalid.setChecked(True)
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("import notes (source, who exported it)")
        self.import_btn = QPushButton("Import")
        self.import_btn.setObjectName("primary")
        self.import_btn.clicked.connect(self.commit)
        commit_bar.addWidget(self.reject_invalid)
        commit_bar.addWidget(self.notes, 1)
        commit_bar.addWidget(self.import_btn)
        layout.addLayout(commit_bar)
        tabs.addTab(import_tab, "Import file")
        # --- batches tab
        batches_tab = QWidget()
        b_layout = QVBoxLayout(batches_tab)
        self.batches = DataTable(BATCH_COLUMNS)
        self.batches.selection_changed.connect(self._batch_selected)
        self.rows = DataTable(ROW_COLUMNS, colour_key="validation_status")
        row_bar = QHBoxLayout()
        self.deactivate_btn = QPushButton("Deactivate row…")
        self.reactivate_btn = QPushButton("Reactivate row…")
        self.refresh_btn = QPushButton("Refresh")
        self.deactivate_btn.clicked.connect(lambda: self._row_action(False))
        self.reactivate_btn.clicked.connect(lambda: self._row_action(True))
        self.refresh_btn.clicked.connect(self.refresh)
        for b in (self.deactivate_btn, self.reactivate_btn, self.refresh_btn):
            row_bar.addWidget(b)
        row_bar.addStretch(1)
        split = QSplitter()
        split.setOrientation(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Orientation.Vertical)
        split.addWidget(self.batches)
        split.addWidget(self.rows)
        b_layout.addWidget(split, 1)
        b_layout.addLayout(row_bar)
        tabs.addTab(batches_tab, "Imported batches")
        # --- manual row tab
        manual_tab = QWidget()
        m_layout = QFormLayout(manual_tab)
        self.m_username = QLineEdit()
        self.m_uuid = QLineEdit()
        self.m_rank = QLineEdit()
        self.m_date = QLineEdit()
        self.m_txn = QLineEdit()
        self.m_reason = QLineEdit()
        for label, widget in (("Username", self.m_username), ("UUID", self.m_uuid), ("Rank (required)", self.m_rank), ("Purchase date", self.m_date), ("Transaction id", self.m_txn), ("Reason (required)", self.m_reason)):
            m_layout.addRow(label, widget)
        self.m_add_btn = QPushButton("Add manual purchase record")
        self.m_add_btn.clicked.connect(self._manual_row)
        m_layout.addRow("", self.m_add_btn)
        m_layout.addRow("", QLabel("Manual rows are stored as a separate batch with source=manual and your name in the audit trail."))
        tabs.addTab(manual_tab, "Manual purchase row")
        self.root.addWidget(tabs, 1)

    def refresh(self) -> None:
        self.call(self.client.csv_batches, on_result=self.batches.set_rows)

    # -------------------------------------------------------------- preview
    def _choose(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "Choose purchase CSV", "", "CSV files (*.csv *.txt);;All files (*)")
        if file:
            self.path = Path(file)
            self.file_label.setText(str(self.path))
            self.preview()

    def _delimiter(self) -> str | None:
        text = self.delimiter.currentText()
        return None if text == "auto" else ("\t" if text == "tab" else text)

    def _mapping(self) -> dict[str, str | None] | None:
        if not self.preview_data:
            return None
        return {field: (None if combo.currentIndex() == 0 else combo.currentText()) for field, combo in self.mapping_combos.items()}

    def preview(self) -> None:
        if self.path is None:
            error_box(self, "Choose a CSV file first.")
            return
        self.call(self.client.preview_csv, self.path, self._mapping(), self._delimiter(), on_result=self._show_preview)

    def _show_preview(self, data: dict) -> None:
        self.preview_data = data
        headers = data.get("headers", [])
        for field, combo in self.mapping_combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(not mapped)")
            combo.addItems(headers)
            mapped = data.get("mapping", {}).get(field)
            if mapped in headers:
                combo.setCurrentText(mapped)
            combo.blockSignals(False)
        summary = data.get("summary", {})
        warn = f" · ALREADY IMPORTED as batch {data['already_imported_batch_id']}" if data.get("already_imported_batch_id") else ""
        self.summary_label.setText(f"{data.get('encoding')} · delimiter {data.get('delimiter')!r} · total {summary.get('total')} valid {summary.get('valid')} invalid {summary.get('invalid')} duplicates {summary.get('duplicate_in_file', 0) + summary.get('duplicate_existing', 0)} unknown ranks {summary.get('unknown_rank')}{warn}")
        self.preview_table.set_rows(data.get("rows", []))

    def commit(self) -> None:
        if self.path is None or not self.preview_data:
            error_box(self, "Preview the file first.")
            return
        self.call(self.client.import_csv, self.path, self._mapping(), self._delimiter(), reject_invalid=self.reject_invalid.isChecked(), notes=self.notes.text().strip(), on_result=self._imported)

    def _imported(self, result: dict) -> None:
        status = result.get("status")
        if status == "rejected":
            error_box(self, "Import refused: the file contains invalid rows and 'reject invalid rows' is off.\n" + "\n".join(f"row {e['row']}: {', '.join(e['errors'])}" for e in result.get("errors", [])[:30]))
            return
        info_box(self, f"Import {status}: batch {result.get('batch_id')} summary {result.get('summary')}")
        if result.get("job_id"):
            self.watch_job(int(result["job_id"]), label="recompute after CSV")
        self.refresh()

    # -------------------------------------------------------------- batches
    def _batch_selected(self, rows: list[dict]) -> None:
        if rows:
            self.call(self.client.csv_rows, int(rows[0]["id"]), on_result=self.rows.set_rows)

    def _row_action(self, reactivate: bool) -> None:
        row = self.rows.current_row()
        if not row:
            return
        note = NoteDialog.ask(self, "Reactivate row" if reactivate else "Deactivate row", "The row and its observation stay in history. Reason:")
        if note:
            fn = self.client.csv_reactivate if reactivate else self.client.csv_deactivate
            self.call(fn, int(row["csv_row_id"]), note, on_result=lambda _r: self._batch_selected(self.batches.selected_rows()))

    def _manual_row(self) -> None:
        if not self.m_rank.text().strip() or not self.m_reason.text().strip():
            error_box(self, "Rank and reason are required.")
            return
        body = {"username": self.m_username.text().strip() or None, "uuid": self.m_uuid.text().strip() or None, "rank": self.m_rank.text().strip(), "purchase_date": self.m_date.text().strip() or None, "transaction_id": self.m_txn.text().strip() or None, "reason": self.m_reason.text().strip()}
        self.call(self.client.csv_manual_row, on_result=self._imported, **body)
