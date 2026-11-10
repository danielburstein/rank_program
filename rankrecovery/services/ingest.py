"""Bulk submissions from a folder on the server.

Staff drop screenshots, or whole folders of them, into ``newuploads`` (the
panel's File Manager supports drag and drop), then run ``ingest``. Every
readable image in the folder and its sub-folders becomes a submission exactly
as if it had been uploaded through Discord or the client, and is then removed;
a sub-folder is deleted once it is empty. Folders named in
``ingest.skip_folders`` (``rejected`` by default) are passed over at any depth.
Files that cannot be decoded move to ``newuploads/rejected`` (keeping their
sub-folder path) so the next run does not trip over them; non-image files are
left where they are and reported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rankrecovery.imaging.decode import SUPPORTED_EXTENSIONS
from rankrecovery.services.context import AppContext
from rankrecovery.services.images import ImageService

log = logging.getLogger(__name__)

REJECTED_DIR = "rejected"


@dataclass
class IngestSummary:
    folder: str
    seen: int = 0
    imported: int = 0
    duplicates: int = 0
    rejected: int = 0
    ignored: int = 0
    folders_removed: int = 0
    image_ids: list[int] = field(default_factory=list)
    batch_id: int | None = None
    job_id: int | None = None
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def text(self) -> str:
        parts = [f"{self.imported} new image(s) submitted from {self.folder}"]
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate(s) already known")
        if self.rejected:
            parts.append(f"{self.rejected} unreadable file(s) moved to {REJECTED_DIR}/")
        if self.ignored:
            parts.append(f"{self.ignored} non-image file(s) left in place")
        if self.folders_removed:
            parts.append(f"{self.folders_removed} emptied sub-folder(s) removed")
        lines = [" · ".join(parts)]
        if self.job_id is not None:
            lines.append(f"extraction queued as job #{self.job_id}")
        lines += [f"  {d}" for d in self.details[:20]]
        return "\n".join(lines)


def unique_path(target: Path) -> Path:
    """``name.png`` -> ``name (2).png`` while the name is taken."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 10_000):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(target)


class FolderIngestService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def folder_path(self, *, create: bool = True) -> Path:
        raw = str(self.ctx.settings.get("ingest.folder") or "newuploads").strip() or "newuploads"
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def skip_folders(self) -> set[str]:
        """Folder names passed over at any depth (``rejected`` by default), compared case-insensitively."""
        raw = self.ctx.settings.get("ingest.skip_folders")
        names = raw if isinstance(raw, list) else [REJECTED_DIR]
        return {str(n).strip().lower() for n in names if str(n).strip()} | {REJECTED_DIR}

    def _skipped(self, path: Path, folder: Path) -> bool:
        parts = path.relative_to(folder).parts
        return any(part.lower() in self.skip_folders() or part.startswith(".") for part in parts)

    def waiting(self) -> list[Path]:
        """Files in the folder and its sub-folders, in path order; skipped folders and hidden entries are not touched."""
        folder = self.folder_path()
        return sorted((p for p in folder.rglob("*") if p.is_file() and not self._skipped(p, folder)), key=lambda p: str(p.relative_to(folder)).lower())

    def _remove_empty_subfolders(self, folder: Path) -> int:
        """Delete sub-folders that the run emptied, deepest first; the drop folder itself and skipped folders stay."""
        removed = 0
        for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if self._skipped(sub, folder) or sub == folder:
                continue
            try:
                if not any(sub.iterdir()):
                    sub.rmdir()
                    removed += 1
            except OSError:  # pragma: no cover - permissions
                continue
        return removed

    def ingest(self, *, submitter: str | None = None, actor: str = "console", extract: bool = True, delete: bool = True, progress: Callable[[int, int, str], None] | None = None) -> IngestSummary:
        """Import every waiting file. ``progress(done, total, label)`` is called after each file; it may raise to cancel."""
        folder = self.folder_path()
        files = self.waiting()
        summary = IngestSummary(folder=str(folder))
        if not files:
            return summary
        images = ImageService(self.ctx)
        submitter = (submitter or str(self.ctx.settings.get("ingest.submitter") or "")).strip() or None
        summary.batch_id = images.create_batch(source="folder", label=f"{folder.name} {datetime.now(UTC):%Y-%m-%d %H:%M}", submitter_name=submitter, created_by=actor, notes=f"{len(files)} file(s) found in {folder}")
        rejected_dir = folder / REJECTED_DIR
        total = len(files)
        for path in files:
            if progress is not None and summary.seen:
                progress(summary.seen, total, f"{summary.imported} imported, {summary.duplicates} duplicates, {summary.rejected} rejected")
            summary.seen += 1
            rel = path.relative_to(folder)
            label = rel.as_posix()
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                summary.ignored += 1
                summary.details.append(f"{label}: not an image file, left in place")
                continue
            try:
                content = path.read_bytes()
            except OSError as exc:
                summary.rejected += 1
                summary.details.append(f"{label}: could not read ({exc})")
                continue
            try:
                # The file's modification time is the upload time, not the capture time, so it is not offered as a date.
                outcome = images.import_bytes(content, filename=path.name, submitter_name=submitter, batch_id=summary.batch_id, original_path=f"{folder.name}/{label}", file_modified=None, created_by=actor)
            except ValueError as exc:
                summary.rejected += 1
                try:
                    target_dir = rejected_dir / rel.parent
                    target_dir.mkdir(parents=True, exist_ok=True)
                    path.replace(unique_path(target_dir / path.name))
                    summary.details.append(f"{label}: {exc}; moved to {REJECTED_DIR}/{rel.parent.as_posix() + '/' if rel.parent.parts else ''}")
                except OSError as move_exc:
                    summary.details.append(f"{label}: {exc}; could not move it ({move_exc})")
                continue
            if outcome.created:
                summary.imported += 1
                summary.image_ids.append(outcome.image_id)
            else:
                summary.duplicates += 1
                summary.details.append(f"{label}: identical to image #{outcome.duplicate_of}, counts once")
            if delete:
                try:
                    path.unlink()
                except OSError as exc:  # pragma: no cover - permissions
                    summary.details.append(f"{label}: imported but could not be removed ({exc})")
        if progress is not None:
            progress(summary.seen, total, f"{summary.imported} imported, {summary.duplicates} duplicates, {summary.rejected} rejected")
        if delete:
            summary.folders_removed = self._remove_empty_subfolders(folder)
        runner = getattr(self.ctx, "job_runner", None)
        if extract and summary.image_ids and runner is not None:
            summary.job_id = runner.submit("extract_images", {"image_ids": summary.image_ids}, label=f"extract {len(summary.image_ids)} image(s) from {folder.name}", created_by=actor, total=len(summary.image_ids))
        log.info("folder ingest: %d seen, %d imported, %d duplicates, %d rejected, %d ignored, %d sub-folders removed (%s)", summary.seen, summary.imported, summary.duplicates, summary.rejected, summary.ignored, summary.folders_removed, folder)
        return summary
