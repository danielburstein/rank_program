"""Mode 6: targeted rescan and manual evidence correction."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.pages.images import ImageInspector, open_externally
from rankrecovery.client.theme import status_colour
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import error_box, info_box

CAND_COLUMNS = [("image_id", "Image"), ("filename", "File"), ("how", "How"), ("score", "Score"), ("rank", "Rank"), ("staff", "Staff"), ("status", "Status"), ("previous_flags", "Previous flags"), ("detection_id", "Detection")]
UNRES_COLUMNS = [("detection_id", "Detection"), ("image_id", "Image"), ("username", "Candidate name"), ("raw_prefix", "Prefix"), ("status", "Status"), ("confidence", "Conf"), ("raw_text", "Raw")]
FIND_COLUMNS = [("finding", "Finding"), ("image_id", "Image"), ("username", "Username"), ("raw_prefix", "Prefix"), ("rank", "Rank"), ("staff", "Staff"), ("status", "Status"), ("confidence", "Conf"), ("image_flags", "Image flags"), ("detection_id", "Detection")]


class RescanPage(Page):
    title = "Mode 6 · Rescan and manual evidence"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.username_bbox: list[int] | None = None
        self.prefix_bbox: list[int] | None = None
        top = QGroupBox("Target")
        form = QFormLayout(top)
        self.username = QLineEdit()
        self.username.setPlaceholderText("historical username")
        self.scope = QComboBox()
        self.scope.addItems(["all", "player", "flagged", "batches", "submitters", "images"])
        self.scope_value = QLineEdit()
        self.scope_value.setPlaceholderText("player id / batch ids / submitter names / image ids (comma separated)")
        self.extractor = QComboBox()
        self.extractor.addItems(["auto", "vision", "ocr"])
        buttons = QHBoxLayout()
        self.find_btn = QPushButton("Find candidates (indexed)")
        self.rescan_btn = QPushButton("Targeted rescan")
        self.full_btn = QPushButton("Full rescan (re-analyse every image in scope)")
        self.find_btn.clicked.connect(self.find)
        self.rescan_btn.clicked.connect(lambda: self.rescan(False))
        self.full_btn.clicked.connect(lambda: self.rescan(True))
        for b in (self.find_btn, self.rescan_btn, self.full_btn):
            buttons.addWidget(b)
        form.addRow("Username", self.username)
        form.addRow("Scope", self.scope)
        form.addRow("Scope value", self.scope_value)
        form.addRow("Extractor", self.extractor)
        form.addRow("", buttons)
        self.job_label = QLabel("")
        form.addRow("", self.job_label)
        self.root.addWidget(top)
        tabs = QTabWidget()
        self.candidates = DataTable(CAND_COLUMNS, colour_key="status")
        self.candidates.row_activated.connect(lambda r: self.inspector.load(int(r["image_id"]), highlight=self.username.text().strip()))
        self.unresolved = DataTable(UNRES_COLUMNS, colour_key="status")
        self.unresolved.row_activated.connect(lambda r: self.inspector.load(int(r["image_id"]), highlight=self.username.text().strip()))
        self.findings = DataTable(FIND_COLUMNS, colour_fn=lambda r: status_colour("warning") if r.get("finding") == "NAME_FOUND_RANK_UNREADABLE" else (status_colour("confirmed") if r.get("finding") == "MATCH" else None))
        self.findings.row_activated.connect(lambda r: self.inspector.load(int(r["image_id"]), highlight=self.username.text().strip()))
        tabs.addTab(self.candidates, "Candidates")
        tabs.addTab(self.unresolved, "Unresolved Mode 1 candidates (not asserted)")
        tabs.addTab(self.findings, "Rescan findings")
        self.inspector = ImageInspector(self)
        self.inspector.viewer.region_drawn.connect(self._region_drawn)
        correction = QGroupBox("Manual correction (saved under the screenshot's submitter — never a second source)")
        c_form = QFormLayout(correction)
        self.c_username = QLineEdit()
        self.c_prefix = QLineEdit()
        self.c_prefix.setPlaceholderText("exact prefix text as shown (optional)")
        self.c_rank = QComboBox()
        self.c_rank.addItem("")
        self.c_staff = QComboBox()
        self.c_staff.addItem("")
        self.c_region = QComboBox()
        self.c_region.addItems(["tab", "chat", "nametag", "scoreboard"])
        self.c_leading = QComboBox()
        self.c_leading.addItems(["none", "diamond", "yinyang", "infinity", "other"])
        self.c_trailing = QComboBox()
        self.c_trailing.addItems(["none", "yinyang", "infinity", "unknown"])
        self.c_target = QComboBox()
        self.c_target.addItems(["username box", "prefix box"])
        self.bbox_label = QLabel("no boxes drawn")
        self.c_supersede = QCheckBox("supersede the selected detection")
        self.c_note = QPlainTextEdit()
        self.c_note.setFixedHeight(60)
        self.c_note.setPlaceholderText("review note (required)")
        self.save_btn = QPushButton("Save corrected observation")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save_correction)
        self.open_btn = QPushButton("Open original file")
        self.open_btn.clicked.connect(self._open_original)
        c_form.addRow("Username", self.c_username)
        c_form.addRow("Prefix text", self.c_prefix)
        c_form.addRow("Rank", self.c_rank)
        c_form.addRow("Staff", self.c_staff)
        c_form.addRow("Region", self.c_region)
        c_form.addRow("Leading icon", self.c_leading)
        c_form.addRow("Trailing icon", self.c_trailing)
        c_form.addRow("Drawing", self.c_target)
        c_form.addRow("Boxes", self.bbox_label)
        c_form.addRow("", self.c_supersede)
        c_form.addRow("Note", self.c_note)
        row = QHBoxLayout()
        row.addWidget(self.save_btn)
        row.addWidget(self.open_btn)
        c_form.addRow("", row)
        hint = QLabel("Turn on 'Draw region' in the viewer, drag a box around the username (then switch to prefix box and drag again). Boxes are optional but recommended.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        c_form.addRow("", hint)
        right = QWidget()
        r_layout = QVBoxLayout(right)
        r_layout.setContentsMargins(0, 0, 0, 0)
        r_layout.addWidget(self.inspector, 3)
        r_layout.addWidget(correction, 2)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(tabs)
        split.addWidget(right)
        split.setSizes([450, 850])
        self.root.addWidget(split, 1)

    def refresh(self) -> None:
        policy = self.state.policy
        if self.c_rank.count() == 1 and policy:
            self.c_rank.addItems([r["name"] for r in policy.get("ranks", []) if r.get("enabled", True)])
            self.c_staff.addItems([s["name"] for s in policy.get("staff", [])])

    def _scope_body(self) -> dict:
        mode = self.scope.currentText()
        raw = [v.strip() for v in self.scope_value.text().split(",") if v.strip()]
        body = {"username": self.username.text().strip(), "mode": mode}
        if mode == "player" and raw and raw[0].isdigit():
            body["player_id"] = int(raw[0])
        elif mode == "batches":
            body["batch_ids"] = [int(v) for v in raw if v.isdigit()]
        elif mode == "submitters":
            body["submitter_names"] = raw
        elif mode == "images":
            body["image_ids"] = [int(v) for v in raw if v.isdigit()]
        return body

    def find(self) -> None:
        if not self.username.text().strip():
            error_box(self, "Enter a username.")
            return
        self.c_username.setText(self.username.text().strip())
        self.call(self.client.rescan_candidates, self._scope_body(), on_result=self._show_candidates)

    def _show_candidates(self, data: dict) -> None:
        self.candidates.set_rows(data.get("candidates", []))
        self.unresolved.set_rows(data.get("unresolved_candidates", []))
        self.status(f"{len(data.get('candidates', []))} candidate(s) in {data.get('scope_images', 0)} image(s) in scope")

    def rescan(self, full: bool) -> None:
        if not self.username.text().strip():
            error_box(self, "Enter a username.")
            return
        body = self._scope_body()
        body["full"] = full
        body["extractor"] = None if self.extractor.currentText() == "auto" else self.extractor.currentText()
        self.call(self.client.rescan, body, on_result=lambda r: self.watch_job(int(r["job_id"]), label="rescan", on_progress=lambda j: self.job_label.setText(f"rescan job {j['id']}: {j['status']} {j.get('done')}/{j.get('total')}"), on_finished=self._rescan_done))

    def _rescan_done(self, job: dict) -> None:
        result = job.get("result") or {}
        findings = result.get("findings") or (job.get("checkpoint") or {}).get("findings") or []
        self.findings.set_rows(findings)
        self.job_label.setText(f"rescan job {job['id']} {job['status']}: {result.get('images_scanned', 0)} image(s) scanned, {len(findings)} finding(s)")
        if job.get("status") == "failed":
            error_box(self, job.get("error") or "rescan failed")

    # -------------------------------------------------------------- correction
    def _region_drawn(self, bbox: list[int]) -> None:
        if self.c_target.currentText().startswith("username"):
            self.username_bbox = bbox
            self.c_target.setCurrentIndex(1)
        else:
            self.prefix_bbox = bbox
        self.bbox_label.setText(f"username {self.username_bbox or '-'} · prefix {self.prefix_bbox or '-'}")

    def save_correction(self) -> None:
        image_id = self.inspector.image_id
        if image_id is None:
            error_box(self, "Open an image first (double-click a candidate).")
            return
        note = self.c_note.toPlainText().strip()
        if not note or not self.c_username.text().strip():
            error_box(self, "Username and a review note are required.")
            return
        selected = self.inspector.detections.current_row()
        body = {
            "image_id": image_id,
            "username": self.c_username.text().strip(),
            "prefix_text": self.c_prefix.text().strip() or None,
            "rank": self.c_rank.currentText() or None,
            "staff": self.c_staff.currentText() or None,
            "region": self.c_region.currentText(),
            "username_bbox": self.username_bbox,
            "prefix_bbox": self.prefix_bbox,
            "trailing_icon": self.c_trailing.currentText(),
            "leading_icon": self.c_leading.currentText(),
            "note": note,
            "supersedes_detection_id": int(selected["detection_id"]) if (self.c_supersede.isChecked() and selected) else None,
        }
        self.call(self.client.manual_correction, body, on_result=self._saved)

    def _saved(self, result: dict) -> None:
        info_box(self, f"Saved detection {result.get('detection_id')} (run {result.get('run_id')}); players {result.get('players')} recomputed. Downstream conversions/exports are marked stale until rerun.")
        self.username_bbox = None
        self.prefix_bbox = None
        self.bbox_label.setText("no boxes drawn")
        self.inspector.reload()
        for pid in result.get("players") or []:
            self.state.message.emit(f"player {pid} updated by manual correction")

    def _open_original(self) -> None:
        path = self.inspector.context.get("image", {}).get("path") or ""
        if not open_externally(path):
            info_box(self, "Original file not found on this computer.")
