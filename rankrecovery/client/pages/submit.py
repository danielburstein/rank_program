"""Submit screenshots to the server (drag-and-drop, files, submitter folders)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.drop_area import DropArea
from rankrecovery.client.widgets.forms import error_box

SUPPORTED = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".avif"}
OUTCOME_COLUMNS = [("filename", "File"), ("submitter", "Submitter"), ("result", "Result"), ("image_id", "Image"), ("ai_status", "AI"), ("date_status", "Date"), ("warnings", "Warnings"), ("error", "Error")]


def collect_files(paths: list[Path], *, submitter_from_subfolder: bool) -> list[tuple[Path, str | None]]:
    """Expand folders recursively; the first-level subfolder name becomes the submitter."""
    out: list[tuple[Path, str | None]] = []
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED:
                    rel = f.relative_to(p)
                    sub = rel.parts[0] if submitter_from_subfolder and len(rel.parts) > 1 else None
                    out.append((f, sub))
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            out.append((p, None))
    return out


class SubmitPage(Page):
    title = "Submit screenshots"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.pending: list[tuple[Path, str | None]] = []
        self.drop = DropArea()
        self.drop.paths_dropped.connect(self.add_paths)
        self.root.addWidget(self.drop)
        form = QFormLayout()
        self.submitter = QLineEdit()
        self.submitter.setPlaceholderText("player who provided these screenshots (leave empty = unknown)")
        self.label = QLineEdit()
        self.label.setPlaceholderText("batch label, e.g. discord-dump-2026-03")
        self.subfolders = QCheckBox("Use subfolder names as submitter (folder/<submitter>/*.png)")
        self.subfolders.setChecked(True)
        self.extract = QCheckBox("Extract after upload")
        self.extract.setChecked(True)
        form.addRow("Submitter", self.submitter)
        form.addRow("Batch label", self.label)
        form.addRow("", self.subfolders)
        form.addRow("", self.extract)
        self.root.addLayout(form)
        buttons = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files…")
        self.add_folder_btn = QPushButton("Add folder…")
        self.clear_btn = QPushButton("Clear list")
        self.upload_btn = QPushButton("Upload")
        self.upload_btn.setObjectName("primary")
        self.add_files_btn.clicked.connect(self._pick_files)
        self.add_folder_btn.clicked.connect(self._pick_folder)
        self.clear_btn.clicked.connect(self._clear)
        self.upload_btn.clicked.connect(self.upload)
        for b in (self.add_files_btn, self.add_folder_btn, self.clear_btn, self.upload_btn):
            buttons.addWidget(b)
        self.pending_label = QLabel("0 files queued")
        buttons.addWidget(self.pending_label)
        buttons.addStretch(1)
        self.root.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.root.addWidget(self.progress)
        self.job_label = QLabel("")
        self.job_label.setObjectName("dim")
        self.root.addWidget(self.job_label)
        self.queue = DataTable([("path", "Queued file"), ("submitter", "Submitter")])
        self.outcomes = DataTable(OUTCOME_COLUMNS, colour_key="result")
        self.root.addWidget(QLabel("Queue"))
        self.root.addWidget(self.queue, 1)
        self.root.addWidget(QLabel("Upload outcomes"))
        self.root.addWidget(self.outcomes, 1)

    # ----------------------------------------------------------------- queue
    def add_paths(self, paths: list[Path]) -> None:
        files = collect_files([Path(p) for p in paths], submitter_from_subfolder=self.subfolders.isChecked())
        known = {p for p, _ in self.pending}
        self.pending.extend((p, s) for p, s in files if p not in known)
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        default = self.submitter.text().strip() or None
        self.queue.set_rows([{"path": str(p), "submitter": s or default or "(unknown)"} for p, s in self.pending])
        self.pending_label.setText(f"{len(self.pending)} files queued")

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select screenshots", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.avif)")
        if files:
            self.add_paths([Path(f) for f in files])

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder (one subfolder per submitter)")
        if folder:
            self.add_paths([Path(folder)])

    def _clear(self) -> None:
        self.pending = []
        self._refresh_queue()

    # ---------------------------------------------------------------- upload
    def upload(self) -> None:
        if not self.pending:
            error_box(self, "Queue is empty.")
            return
        default = self.submitter.text().strip() or None
        label = self.label.text().strip() or f"client upload ({len(self.pending)} files)"
        paths = [p for p, _ in self.pending]
        submitters = {p: (s or default) for p, s in self.pending}
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self.upload_btn.setEnabled(False)
        self.job_label.setText("creating batch…")

        def do_upload(progress_cb=None):  # noqa: ANN001
            batch_id = self.client.create_batch(label=label, submitter=default, source="client")
            result = self.client.upload_images(paths, submitter=default, batch_id=batch_id, extract=self.extract.isChecked(), progress_cb=progress_cb, submitter_for=lambda p: submitters.get(p))
            result["batch_id"] = batch_id
            result["submitters"] = {str(p): s for p, s in submitters.items()}
            return result

        self.call(do_upload, progress_cb=None, on_result=self._done, on_error=self._failed)

    def _failed(self, message: str) -> None:
        self.upload_btn.setEnabled(True)
        self.job_label.setText("upload failed")
        error_box(self, message)

    def _done(self, result: dict) -> None:
        self.upload_btn.setEnabled(True)
        rows = []
        by_name = {Path(p).name: s for p, s in result.get("submitters", {}).items()}
        for o in result.get("outcomes", []):
            if o.get("error"):
                res = "error"
            elif o.get("created"):
                res = "imported"
            else:
                res = "duplicate"
            rows.append({**o, "result": res, "submitter": by_name.get(o.get("filename"), "") or "(unknown)"})
        self.outcomes.set_rows(rows)
        self.progress.setValue(self.progress.maximum())
        imported = sum(1 for r in rows if r["result"] == "imported")
        dup = sum(1 for r in rows if r["result"] == "duplicate")
        err = sum(1 for r in rows if r["result"] == "error")
        self.job_label.setText(f"batch {result.get('batch_id')}: {imported} imported, {dup} duplicates, {err} errors")
        self.pending = []
        self._refresh_queue()
        for job_id in result.get("job_ids", []):
            self.watch_job(int(job_id), label="extract", on_progress=self._job_progress, on_finished=self._job_finished)

    def _job_progress(self, job: dict) -> None:
        self.job_label.setText(f"extraction job {job.get('id')}: {job.get('status')} {job.get('done')}/{job.get('total')} {job.get('progress_message', '')}")

    def _job_finished(self, job: dict) -> None:
        self.job_label.setText(f"extraction job {job.get('id')} {job.get('status')}: {job.get('result') or job.get('error') or ''}")
        self.state.message.emit(f"extraction job {job.get('id')} {job.get('status')}")
