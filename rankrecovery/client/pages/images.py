"""Screenshot browser with overlay viewer, metadata, detections and reviews."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtWidgets import (
    QComboBox,
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
from rankrecovery.client.widgets.flag_list import FlagList
from rankrecovery.client.widgets.forms import KeyValueForm, NoteDialog, error_box, info_box
from rankrecovery.client.widgets.image_viewer import ImageViewer

IMAGE_COLUMNS = [("image_id", "ID"), ("filename", "File"), ("submitter", "Submitter"), ("ai_status", "AI"), ("date_status", "Date"), ("capture_at", "Capture"), ("capture_source", "Date source"), ("width", "W"), ("height", "H"), ("duplicate_group", "Dup group"), ("batch_id", "Batch"), ("imported_at", "Imported")]
DETECTION_COLUMNS = [("detection_id", "ID"), ("username", "Username"), ("raw_prefix", "Prefix"), ("rank", "Rank"), ("staff", "Staff"), ("custom_prefix", "Custom"), ("trailing_icon_rank", "Icon rank"), ("region", "Region"), ("status", "Status"), ("confidence", "Conf"), ("needs_review", "Review?"), ("manual", "Manual"), ("prefix_kind", "Kind"), ("misread_of", "Misread of"), ("raw_text", "Raw text"), ("notes", "Notes")]


def open_externally(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return True


class ImageInspector(QWidget):
    """Viewer + tabs for one image. Reused by the players and rescan pages."""

    def __init__(self, page: Page, parent: QWidget | None = None):
        super().__init__(parent)
        self.page = page
        self.image_id: int | None = None
        self.context: dict = {}
        self.viewer = ImageViewer()
        self.tabs = QTabWidget()
        self.info = KeyValueForm("Image")
        self.metadata = DataTable([("key", "Key"), ("value", "Value"), ("category", "Category"), ("reason", "Reason")])
        self.detections = DataTable(DETECTION_COLUMNS, colour_key="status")
        self.detections.selection_changed.connect(self._select_detection)
        self.flags = FlagList(on_resolve=self._resolve_flag, on_reopen=self._reopen_flag)
        self.runs = DataTable([("id", "Run"), ("extractor", "Extractor"), ("engine", "Engine"), ("model", "Model"), ("purpose", "Purpose"), ("status", "Status"), ("started_at", "Started"), ("warnings", "Warnings"), ("error", "Error"), ("cost", "Cost")], colour_key="status")
        self.paths = DataTable([("path", "Path"), ("filename", "File"), ("submitter_id", "Submitter"), ("imported_at", "Imported")])
        det_box = QWidget()
        det_layout = QVBoxLayout(det_box)
        det_layout.setContentsMargins(0, 0, 0, 0)
        det_bar = QHBoxLayout()
        self.accept_btn = QPushButton("Accept selected…")
        self.reject_btn = QPushButton("Reject selected…")
        self.accept_btn.clicked.connect(lambda: self._bulk(True))
        self.reject_btn.clicked.connect(lambda: self._bulk(False))
        det_bar.addWidget(self.accept_btn)
        det_bar.addWidget(self.reject_btn)
        det_bar.addStretch(1)
        det_layout.addLayout(det_bar)
        det_layout.addWidget(self.detections, 1)
        actions = QWidget()
        act_layout = QVBoxLayout(actions)
        self.submitter_btn = QPushButton("Correct submitter…")
        self.ai_flag_btn = QPushButton("Mark as AI-generated…")
        self.ai_clear_btn = QPushButton("Clear AI indicator…")
        self.date_btn = QPushButton("Set capture date…")
        self.date_unknown_btn = QPushButton("Mark date unknown…")
        self.open_btn = QPushButton("Open original file")
        self.submitter_btn.clicked.connect(self._submitter)
        self.ai_flag_btn.clicked.connect(lambda: self._ai(True))
        self.ai_clear_btn.clicked.connect(lambda: self._ai(False))
        self.date_btn.clicked.connect(self._date)
        self.date_unknown_btn.clicked.connect(lambda: self._date(unknown=True))
        self.open_btn.clicked.connect(self.open_original)
        for b in (self.submitter_btn, self.ai_flag_btn, self.ai_clear_btn, self.date_btn, self.date_unknown_btn, self.open_btn):
            act_layout.addWidget(b)
        act_layout.addWidget(QLabel("Metadata absence never proves authenticity; AI flags are metadata-based suspicion."))
        act_layout.addStretch(1)
        self.tabs.addTab(self.info, "Image")
        self.tabs.addTab(det_box, "Detections")
        self.tabs.addTab(self.flags, "Flags")
        self.tabs.addTab(self.metadata, "Metadata")
        self.tabs.addTab(self.runs, "Extraction runs")
        self.tabs.addTab(self.paths, "Paths")
        self.tabs.addTab(actions, "Review actions")
        split = QSplitter()
        split.addWidget(self.viewer)
        split.addWidget(self.tabs)
        split.setSizes([700, 500])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

    # ----------------------------------------------------------------- load
    def load(self, image_id: int, *, highlight: str | None = None) -> None:
        self.image_id = image_id
        self.viewer.highlight = highlight

        def fetch():  # noqa: ANN202
            ctx = self.page.client.image_context(image_id)
            data = self.page.client.image_content(image_id)
            try:
                overlay = self.page.client.image_overlay(image_id, highlight)
            except Exception:  # noqa: BLE001 - overlay is optional
                overlay = None
            return ctx, data, overlay

        self.page.call(fetch, on_result=self._loaded)

    def _loaded(self, result: tuple) -> None:
        ctx, data, overlay = result
        self.context = ctx
        image = ctx.get("image", {})
        info = f"{image.get('filename')} · {image.get('width')}×{image.get('height')} · submitter {image.get('submitter') or 'unknown'} · AI {image.get('ai_status')} · date {image.get('date_status')}"
        self.viewer.load_bytes(data, overlay=overlay, detections=ctx.get("detections", []), highlight=self.viewer.highlight, info=info)
        self.info.set_values(image, order=["image_id", "filename", "path", "submitter", "ai_status", "ai_details", "date_status", "capture_at", "capture_source", "capture_reason", "sha256", "phash", "duplicate_group", "format", "width", "height", "frames", "batch_id", "imported_at", "file_modified_at", "availability"])
        self.metadata.set_rows(ctx.get("metadata", []))
        self.detections.set_rows(ctx.get("detections", []))
        self.flags.set_flags(ctx.get("flags", []))
        self.runs.set_rows(ctx.get("runs", []))
        self.paths.set_rows(ctx.get("paths", []))
        self.open_btn.setEnabled(bool(image.get("path")) and os.path.exists(image.get("path") or ""))

    def reload(self) -> None:
        if self.image_id is not None:
            self.load(self.image_id, highlight=self.viewer.highlight)

    def _select_detection(self, rows: list[dict]) -> None:
        if rows:
            self.viewer.set_highlight(rows[0].get("username"))

    # -------------------------------------------------------------- actions
    def _bulk(self, accept: bool) -> None:
        rows = self.detections.selected_rows()
        if not rows:
            error_box(self, "Select detections first.")
            return
        note = NoteDialog.ask(self, "Review detections", f"{'Accept' if accept else 'Reject'} {len(rows)} detection(s)? A note is required and audited.")
        if note:
            self.page.call(self.page.client.bulk_review, [int(r["detection_id"]) for r in rows], accept, note, on_result=lambda _r: self.reload())

    def _resolve_flag(self, flag_id: int, note: str) -> None:
        self.page.call(self.page.client.resolve_flag, flag_id, note, on_result=lambda _r: self.reload())

    def _reopen_flag(self, flag_id: int, note: str) -> None:
        self.page.call(self.page.client.reopen_flag, flag_id, note, on_result=lambda _r: self.reload())

    def _submitter(self) -> None:
        if self.image_id is None:
            return
        edit = QLineEdit(self.context.get("image", {}).get("submitter") or "")
        dialog = NoteDialog("Correct submitter", "Who actually provided this screenshot? Leave empty for unknown. The change is audited.", parent=self, extra_fields={"Submitter": edit})
        if dialog.exec():
            self.page.call(self.page.client.set_submitter, self.image_id, edit.text().strip() or None, dialog.value, on_result=lambda _r: self.reload())

    def _ai(self, flagged: bool) -> None:
        if self.image_id is None:
            return
        note = NoteDialog.ask(self, "AI review", "Mark this image as AI-generated (ineligible for rank confirmation)." if flagged else "Clear the AI indicator for this image. Explain what you checked.")
        if note:
            self.page.call(self.page.client.ai_review, self.image_id, flagged, note, on_result=lambda _r: self.reload())

    def _date(self, unknown: bool = False) -> None:
        if self.image_id is None:
            return
        edit = QLineEdit("" if unknown else datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        fields = {} if unknown else {"Capture date (ISO)": edit}
        dialog = NoteDialog("Date review", "Record the capture date you verified, or mark it unknown. The raw evidence stays stored.", parent=self, extra_fields=fields)
        if dialog.exec():
            value = None if unknown else edit.text().strip() or None
            self.page.call(self.page.client.date_review, self.image_id, value, dialog.value, on_result=lambda _r: self.reload())

    def open_original(self) -> None:
        path = self.context.get("image", {}).get("path") or ""
        if not open_externally(path):
            info_box(self, "The original file is not available on this computer; the server keeps a copy in the database.")


class ImagesPage(Page):
    title = "Screenshots"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        bar = QHBoxLayout()
        self.submitter_filter = QComboBox()
        self.submitter_filter.setEditable(True)
        self.submitter_filter.setPlaceholderText("submitter")
        self.batch_filter = QLineEdit()
        self.batch_filter.setPlaceholderText("batch id")
        self.batch_filter.setFixedWidth(90)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(QLabel("Submitter"))
        bar.addWidget(self.submitter_filter)
        bar.addWidget(QLabel("Batch"))
        bar.addWidget(self.batch_filter)
        bar.addWidget(self.refresh_btn)
        bar.addStretch(1)
        self.root.addLayout(bar)
        self.table = DataTable(IMAGE_COLUMNS, paginated=True, colour_key="ai_status")
        self.table.page_changed.connect(lambda _l, _o: self.refresh())
        self.table.selection_changed.connect(self._selected)
        self.inspector = ImageInspector(self)
        split = QSplitter()
        split.setOrientation(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.inspector)
        split.setSizes([280, 620])
        self.root.addWidget(split, 1)
        state.open_image.connect(self.open_image)

    def refresh(self) -> None:
        self.call(self.client.submitters, on_result=self._fill_submitters, on_error=lambda _m: None)
        params = {"limit": self.table.limit_box.value(), "offset": self.table.offset}
        if self.submitter_filter.currentText().strip():
            params["submitter"] = self.submitter_filter.currentText().strip()
        if self.batch_filter.text().strip().isdigit():
            params["batch_id"] = int(self.batch_filter.text())
        self.call(self.client.images, on_result=lambda r: self.table.set_rows(r.get("rows", []), r.get("total")), **params)

    def _fill_submitters(self, rows: list[dict]) -> None:
        current = self.submitter_filter.currentText()
        self.submitter_filter.clear()
        self.submitter_filter.addItem("")
        for r in rows:
            self.submitter_filter.addItem(f"{r['name']}")
        self.submitter_filter.setCurrentText(current)

    def _selected(self, rows: list[dict]) -> None:
        if rows:
            self.inspector.load(int(rows[0]["image_id"]))

    def open_image(self, image_id: int) -> None:
        self._loaded = True
        self.inspector.load(image_id)
