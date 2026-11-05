"""Mode 5: universal search."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.data_table import DataTable

PLAYER_COLS = [("display_name", "Player"), ("aliases", "Aliases"), ("uuid", "UUID"), ("identity_status", "Identity"), ("effective_rank", "Effective"), ("highest_observed", "Highest"), ("provisional", "Provisional"), ("staff_ranks", "Staff"), ("active_flags", "Flags"), ("stale", "Stale"), ("player_id", "ID")]
IMAGE_COLS = [("image_id", "ID"), ("filename", "File"), ("submitter", "Submitter"), ("ai_status", "AI"), ("date_status", "Date"), ("capture_source", "Date source"), ("sha256", "SHA-256"), ("duplicate_group", "Dup group")]
DET_COLS = [("detection_id", "ID"), ("image_id", "Image"), ("username", "Username"), ("raw_prefix", "Prefix"), ("rank", "Rank"), ("staff", "Staff"), ("custom_prefix", "Custom"), ("status", "Status"), ("confidence", "Conf"), ("raw_text", "Raw text"), ("player_id", "Player")]
CSV_COLS = [("csv_row_id", "ID"), ("batch_id", "Batch"), ("username", "Username"), ("uuid", "UUID"), ("rank", "Rank"), ("purchase_date", "Purchased"), ("transaction_id", "Transaction"), ("validation_status", "Validation"), ("is_active", "Active"), ("player_id", "Player")]
LOOKUP_COLS = [("attempt_id", "ID"), ("player_id", "Player"), ("queried_name", "Name"), ("provider", "Provider"), ("outcome", "Outcome"), ("failure_code", "Failure"), ("http_status", "HTTP"), ("proxy_id", "Proxy"), ("error_message", "Error"), ("started_at", "At")]


class SearchPage(Page):
    title = "Mode 5 · Search and review"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        box = QGroupBox("Filters (all optional, combined with AND)")
        form = QFormLayout(box)
        self.text = QLineEdit()
        self.text.setPlaceholderText("username, alias, UUID, filename, hash, raw extracted text, transaction id…")
        self.text.returnPressed.connect(self.run)
        self.exact = QCheckBox("exact match")
        self.types = {t: QCheckBox(t) for t in ("player", "image", "detection", "csv_row", "lookup")}
        for box_ in self.types.values():
            box_.setChecked(True)
        type_row = QHBoxLayout()
        for b in self.types.values():
            type_row.addWidget(b)
        self.rank = QLineEdit()
        self.rank.setPlaceholderText("old rank, alias or new rank")
        self.highest_rank = QLineEdit()
        self.provisional_only = QCheckBox("provisional high ranks only")
        self.staff = QLineEdit()
        self.custom_prefix = QLineEdit()
        self.flag_code = QComboBox()
        self.flag_code.setEditable(True)
        self.flag_code.addItem("")
        self.flag_active = QComboBox()
        self.flag_active.addItems(["active", "resolved", "any"])
        self.submitter = QLineEdit()
        self.ai_status = QComboBox()
        self.ai_status.addItems(["", "ai_indicator_found", "ai_filename_indicator", "no_ai_indicator_found", "manually_cleared", "manually_flagged"])
        self.date_status = QComboBox()
        self.date_status.addItems(["", "ok", "out_of_date", "unknown"])
        self.identity_status = QComboBox()
        self.identity_status.addItems(["", "verified", "manual", "unresolved", "ambiguous", "lookup_failed", "bedrock_pending", "not_attempted"])
        self.export_included = QComboBox()
        self.export_included.addItems(["", "included", "excluded"])
        self.lookup_failure = QLineEdit()
        self.lookup_failure.setPlaceholderText("UUID_LOOKUP_…")
        self.http_status = QLineEdit()
        self.http_status.setPlaceholderText("e.g. 429")
        self.min_sources = QSpinBox()
        self.min_sources.setRange(0, 20)
        self.limit = QSpinBox()
        self.limit.setRange(10, 2000)
        self.limit.setValue(200)
        form.addRow("Text", self.text)
        form.addRow("", self.exact)
        form.addRow("Tables", type_row)
        form.addRow("Rank", self.rank)
        form.addRow("Highest rank", self.highest_rank)
        form.addRow("", self.provisional_only)
        form.addRow("Staff", self.staff)
        form.addRow("Custom prefix", self.custom_prefix)
        form.addRow("Flag", self.flag_code)
        form.addRow("Flag state", self.flag_active)
        form.addRow("Submitter", self.submitter)
        form.addRow("AI status", self.ai_status)
        form.addRow("Date status", self.date_status)
        form.addRow("Identity", self.identity_status)
        form.addRow("Export", self.export_included)
        form.addRow("Lookup failure", self.lookup_failure)
        form.addRow("HTTP status", self.http_status)
        form.addRow("Min sources", self.min_sources)
        form.addRow("Limit", self.limit)
        self.run_btn = QPushButton("Search")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        form.addRow("", self.run_btn)
        self.tabs = QTabWidget()
        self.players = DataTable(PLAYER_COLS, colour_key="identity_status")
        self.images = DataTable(IMAGE_COLS, colour_key="ai_status")
        self.detections = DataTable(DET_COLS, colour_key="status")
        self.csv_rows = DataTable(CSV_COLS, colour_key="validation_status")
        self.lookups = DataTable(LOOKUP_COLS, colour_key="outcome")
        self.players.row_activated.connect(lambda r: self.state.open_player.emit(int(r["player_id"])))
        self.images.row_activated.connect(lambda r: self.state.open_image.emit(int(r["image_id"])))
        self.detections.row_activated.connect(lambda r: self.state.open_image.emit(int(r["image_id"])))
        self.detections.add_row_action("Open player", lambda r: r.get("player_id") and self.state.open_player.emit(int(r["player_id"])))
        self.csv_rows.row_activated.connect(lambda r: r.get("player_id") and self.state.open_player.emit(int(r["player_id"])))
        self.lookups.row_activated.connect(lambda r: r.get("player_id") and self.state.open_player.emit(int(r["player_id"])))
        for table, name in ((self.players, "Players"), (self.images, "Images"), (self.detections, "Detections"), (self.csv_rows, "CSV rows"), (self.lookups, "Lookups")):
            self.tabs.addTab(table, name)
        split = __import__("PySide6.QtWidgets", fromlist=["QSplitter"]).QSplitter()
        split.addWidget(box)
        split.addWidget(self.tabs)
        split.setSizes([380, 800])
        self.root.addWidget(split, 1)

    def refresh(self) -> None:
        if self.flag_code.count() == 1 and self.state.flag_catalogue:
            self.flag_code.addItems([f["code"] for f in self.state.flag_catalogue])

    def body(self) -> dict:
        flag_state = self.flag_active.currentText()
        return {
            "text": self.text.text().strip(),
            "exact": self.exact.isChecked(),
            "entity_types": [t for t, b in self.types.items() if b.isChecked()],
            "rank": self.rank.text().strip() or None,
            "highest_rank": self.highest_rank.text().strip() or None,
            "provisional_only": self.provisional_only.isChecked(),
            "staff": self.staff.text().strip() or None,
            "custom_prefix": self.custom_prefix.text().strip() or None,
            "flag_code": self.flag_code.currentText().strip() or None,
            "flag_active": True if flag_state == "active" else (False if flag_state == "resolved" else None),
            "submitter": self.submitter.text().strip() or None,
            "ai_status": self.ai_status.currentText() or None,
            "date_status": self.date_status.currentText() or None,
            "identity_status": self.identity_status.currentText() or None,
            "export_included": None if not self.export_included.currentText() else self.export_included.currentText() == "included",
            "lookup_failure_code": self.lookup_failure.text().strip() or None,
            "http_status": int(self.http_status.text()) if self.http_status.text().strip().isdigit() else None,
            "min_sources": self.min_sources.value() or None,
            "limit": self.limit.value(),
            "offset": 0,
        }

    def run(self) -> None:
        self.call(self.client.search, self.body(), on_result=self._show)

    def _show(self, data: dict) -> None:
        self.players.set_rows(data.get("players", []))
        self.images.set_rows(data.get("images", []))
        self.detections.set_rows(data.get("detections", []))
        self.csv_rows.set_rows(data.get("csv_rows", []))
        self.lookups.set_rows(data.get("lookups", []))
        self.tabs.setTabText(0, f"Players ({len(data.get('players', []))})")
        self.tabs.setTabText(1, f"Images ({len(data.get('images', []))})")
        self.tabs.setTabText(2, f"Detections ({len(data.get('detections', []))})")
        self.tabs.setTabText(3, f"CSV rows ({len(data.get('csv_rows', []))})")
        self.tabs.setTabText(4, f"Lookups ({len(data.get('lookups', []))})")
