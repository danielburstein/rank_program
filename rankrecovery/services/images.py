"""Screenshot import: hashing, metadata, AI indicators, dates, duplicates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from rankrecovery.core.ai_indicators import scan_metadata
from rankrecovery.core.dates import DateEvidence, decide_image_date
from rankrecovery.core.enums import AiStatus, DateSource, DateStatus
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import Flag, ImageMetadata, ImagePath, ImportBatch, SourceImage, Submitter
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import upsert_flags

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".avif"}


@dataclass(slots=True)
class ImportOutcome:
    image_id: int
    created: bool
    sha256: str
    duplicate_of: int | None = None
    near_duplicates: list[int] = field(default_factory=list)
    ai_status: str = ""
    date_status: str = ""
    warnings: list[str] = field(default_factory=list)


class ImageService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ------------------------------------------------------------- submitters
    def get_or_create_submitter(self, session: Any, name: str | None, *, discord_user_id: str | None = None) -> Submitter | None:
        if not name or not name.strip():
            return None
        key = normalise_username_key(name)
        sub = session.execute(select(Submitter).where(Submitter.name_key == key)).scalars().first()
        if sub is None:
            sub = Submitter(name=name.strip(), name_key=key, discord_user_id=discord_user_id)
            session.add(sub)
            session.flush()
        elif discord_user_id and not sub.discord_user_id:
            sub.discord_user_id = discord_user_id
        return sub

    def create_batch(self, *, source: str, label: str, submitter_name: str | None, created_by: str, notes: str = "") -> int:
        with self.ctx.db.write_session() as s:
            sub = self.get_or_create_submitter(s, submitter_name)
            batch = ImportBatch(source=source, label=label, submitter_id=sub.id if sub else None, created_by=created_by, notes=notes)
            s.add(batch)
            s.flush()
            return batch.id

    # ----------------------------------------------------------------- import
    def import_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        submitter_name: str | None,
        batch_id: int | None = None,
        original_path: str = "",
        file_modified: datetime | None = None,
        created_by: str = "",
        discord_user_id: str | None = None,
    ) -> ImportOutcome:
        from rankrecovery.imaging.decode import decode_image, image_facts
        from rankrecovery.imaging.hashing import hamming_distance, perceptual_hash, sha256_hex
        from rankrecovery.imaging.metadata import extract_metadata

        sha = sha256_hex(content)
        settings = self.ctx.settings
        warnings: list[str] = []

        with self.ctx.db.write_session() as s:
            sub = self.get_or_create_submitter(s, submitter_name, discord_user_id=discord_user_id)
            existing = s.execute(select(SourceImage).where(SourceImage.sha256 == sha)).scalars().first()
            if existing is not None:
                s.add(ImagePath(image_id=existing.id, path=original_path or filename, filename=filename, import_batch_id=batch_id, submitter_id=sub.id if sub else None))
                if existing.submitter_id is None and sub is not None:
                    audit(s, actor=created_by, action="image.submitter_set", entity_type="image", entity_id=existing.id, before={"submitter_id": None}, after={"submitter_id": sub.id}, note="first known submitter for previously unknown-submitter image")
                    existing.submitter_id = sub.id
                elif sub is not None and existing.submitter_id != sub.id:
                    upsert_flags(s, "image", existing.id, [FlagInstance("DUPLICATE_IMAGE", f"identical content re-submitted by {sub.name}; counts once")], actor=created_by)
                    warnings.append("identical image already imported under another submitter; it counts as one source")
                return ImportOutcome(existing.id, False, sha, duplicate_of=existing.id, ai_status=existing.ai_status, date_status=existing.date_status, warnings=warnings)

            try:
                facts = image_facts(content)
            except Exception as exc:  # malformed image
                raise ValueError(f"cannot decode {filename}: {exc}") from exc
            try:
                metadata = extract_metadata(content, filename=filename)
            except Exception as exc:  # pragma: no cover - defensive
                metadata = {"METADATA:error": str(exc)}
            try:
                ph = perceptual_hash(decode_image(content))
            except Exception:
                ph = None
                warnings.append("perceptual hash failed")

            ai = scan_metadata(
                metadata,
                indicators=list(settings.get("ai.metadata_indicators") or []),
                filename=filename,
                filename_patterns=list(settings.get("ai.filename_patterns") or []),
                inspect_filename=bool(settings.get("ai.inspect_filenames", True)),
            )
            date_decision = decide_image_date(metadata=metadata, filename=filename, file_modified=file_modified, policy=self.ctx.date_policy())

            image = SourceImage(
                sha256=sha,
                phash=ph,
                original_filename=filename,
                original_path=original_path,
                byte_size=len(content),
                format=facts.format or "",
                width=int(facts.width),
                height=int(facts.height),
                mode=facts.mode or "",
                frame_count=int(facts.frame_count),
                content=content if bool(settings.get("images.store_bytes_in_database", True)) else None,
                storage_ref=None if bool(settings.get("images.store_bytes_in_database", True)) else original_path,
                import_batch_id=batch_id,
                submitter_id=sub.id if sub else None,
                file_modified_at=file_modified,
                capture_at=date_decision.evidence.value,
                capture_source=date_decision.evidence.source.value,
                capture_reason=date_decision.reason,
                date_status=date_decision.status.value,
                ai_status=ai.status.value,
                ai_details="; ".join(f"{h.field}={h.matched}" for h in ai.hits),
                duplicate_group=sha,
            )
            s.add(image)
            s.flush()
            s.add(ImagePath(image_id=image.id, path=original_path or filename, filename=filename, import_batch_id=batch_id, submitter_id=sub.id if sub else None))

            ai_fields = {h.field for h in ai.hits}
            for key, value in metadata.items():
                category = "other"
                reason = ""
                if key in ai_fields:
                    category = "ai"
                    reason = "matched AI indicator " + next(h.matched for h in ai.hits if h.field == key)
                elif key == date_decision.evidence.key:
                    category = "date"
                    reason = f"selected as {date_decision.evidence.source.value} capture date"
                elif "date" in key.lower() or "time" in key.lower():
                    category = "date"
                s.add(ImageMetadata(image_id=image.id, key=key[:128], value=str(value)[:4000], category=category, reason=reason))

            flags: list[FlagInstance] = []
            if ai.status == AiStatus.AI_INDICATOR_FOUND:
                flags.append(FlagInstance("AI_INDICATOR_FOUND", image.ai_details))
            elif ai.status == AiStatus.AI_FILENAME_INDICATOR:
                flags.append(FlagInstance("AI_FILENAME_INDICATOR", image.ai_details))
            if date_decision.status == DateStatus.OUT_OF_DATE:
                flags.append(FlagInstance("OUT_OF_DATE", date_decision.reason))
            elif date_decision.status == DateStatus.UNKNOWN:
                flags.append(FlagInstance("DATE_UNKNOWN", date_decision.reason))
            if sub is None:
                flags.append(FlagInstance("SUBMITTER_UNKNOWN", "no submitter recorded at import"))

            # ---- perceptual duplicates
            near: list[int] = []
            if ph:
                dup_dist = int(settings.get("images.phash_duplicate_distance", 6))
                near_dist = int(settings.get("images.phash_near_duplicate_distance", 12))
                others = s.execute(select(SourceImage.id, SourceImage.phash, SourceImage.duplicate_group).where(SourceImage.id != image.id, SourceImage.phash.is_not(None))).all()
                best: tuple[int, int, str] | None = None
                for oid, oph, ogroup in others:
                    d = hamming_distance(ph, oph)
                    if d <= dup_dist and (best is None or d < best[0]):
                        best = (d, oid, ogroup)
                    elif d <= near_dist:
                        near.append(oid)
                if best is not None:
                    image.duplicate_group = best[2]
                    flags.append(FlagInstance("DUPLICATE_IMAGE", f"perceptual duplicate of image {best[1]} (distance {best[0]}); counts as one source", evidence_ref=f"image:{best[1]}"))
                for oid in near:
                    flags.append(FlagInstance("NEAR_DUPLICATE_IMAGE", f"similar to image {oid}; review whether it is the same capture", evidence_ref=f"image:{oid}"))
            upsert_flags(s, "image", image.id, flags, actor=created_by or "import")
            if batch_id:
                batch = s.get(ImportBatch, batch_id)
                if batch:
                    batch.image_count += 1
            audit(s, actor=created_by, action="image.import", entity_type="image", entity_id=image.id, after={"filename": filename, "submitter": sub.name if sub else None})
            return ImportOutcome(image.id, True, sha, near_duplicates=near, ai_status=image.ai_status, date_status=image.date_status, warnings=warnings)

    def import_path(self, path: str | Path, *, submitter_name: str | None, batch_id: int | None, created_by: str = "") -> ImportOutcome:
        p = Path(path)
        content = p.read_bytes()
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
        return self.import_bytes(content, filename=p.name, submitter_name=submitter_name, batch_id=batch_id, original_path=str(p.resolve()), file_modified=mtime, created_by=created_by)

    @staticmethod
    def iter_folder(root: str | Path, *, recursive: bool = True, submitter_from_subfolder: bool = True) -> list[tuple[Path, str | None]]:
        """List (path, submitter) pairs. With ``submitter_from_subfolder`` the first-level folder name is the submitter."""
        root = Path(root)
        out: list[tuple[Path, str | None]] = []
        iterator = root.rglob("*") if recursive else root.glob("*")
        for p in sorted(iterator):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            submitter = None
            if submitter_from_subfolder:
                rel = p.relative_to(root)
                if len(rel.parts) > 1:
                    submitter = rel.parts[0]
            out.append((p, submitter))
        return out

    # ------------------------------------------------------------------ bytes
    def get_bytes(self, image_id: int) -> bytes | None:
        with self.ctx.db.read_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                return None
            if image.content is not None:
                return bytes(image.content)
            if image.storage_ref and Path(image.storage_ref).exists():
                return Path(image.storage_ref).read_bytes()
            if image.original_path and Path(image.original_path).exists():
                return Path(image.original_path).read_bytes()
            with self.ctx.db.write_session() as ws:
                row = ws.get(SourceImage, image_id)
                if row is not None:
                    row.availability = "missing"
            return None

    # ----------------------------------------------------------------- reviews
    def set_submitter(self, image_id: int, submitter_name: str | None, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                raise KeyError(image_id)
            sub = self.get_or_create_submitter(s, submitter_name)
            before = {"submitter_id": image.submitter_id}
            image.submitter_id = sub.id if sub else None
            audit(s, actor=actor, action="image.submitter_corrected", entity_type="image", entity_id=image_id, before=before, after={"submitter_id": image.submitter_id}, note=reason)
            self._mark_dependents_stale(s, image_id)

    def review_ai_status(self, image_id: int, *, flagged: bool, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                raise KeyError(image_id)
            before = {"ai_status": image.ai_status}
            image.ai_status = AiStatus.MANUALLY_FLAGGED.value if flagged else AiStatus.MANUALLY_CLEARED.value
            audit(s, actor=actor, action="image.ai_review", entity_type="image", entity_id=image_id, before=before, after={"ai_status": image.ai_status}, note=reason)
            if not flagged:
                for f in s.execute(select(Flag).where(Flag.entity_type == "image", Flag.entity_id == image_id, Flag.code.in_(["AI_INDICATOR_FOUND", "AI_FILENAME_INDICATOR"]), Flag.is_active.is_(True))).scalars():
                    f.is_active = False
                    f.resolved_at = datetime.now(UTC)
                    f.resolved_by = actor
                    f.resolution_note = reason
            else:
                upsert_flags(s, "image", image_id, [FlagInstance("AI_INDICATOR_FOUND", f"manually flagged: {reason}")], actor=actor, source="manual")
            self._mark_dependents_stale(s, image_id)

    def review_date(self, image_id: int, *, capture_at: datetime | None, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                raise KeyError(image_id)
            before = {"capture_at": image.capture_at.isoformat() if image.capture_at else None, "date_status": image.date_status}
            manual = DateEvidence(capture_at, DateSource.MANUAL, raw=capture_at.isoformat() if capture_at else "", key="manual_review", reliable=True) if capture_at else None
            metadata = {m.key: m.value for m in image.metadata_entries}
            decision = decide_image_date(metadata=metadata, filename=image.original_filename, file_modified=image.file_modified_at, policy=self.ctx.date_policy(), manual=manual)
            image.capture_at = decision.evidence.value
            image.capture_source = decision.evidence.source.value
            image.capture_reason = f"{decision.reason} (manual review: {reason})"
            image.date_status = decision.status.value
            for f in s.execute(select(Flag).where(Flag.entity_type == "image", Flag.entity_id == image_id, Flag.code.in_(["OUT_OF_DATE", "DATE_UNKNOWN"]), Flag.is_active.is_(True))).scalars():
                f.is_active = False
                f.resolved_at = datetime.now(UTC)
                f.resolved_by = actor
                f.resolution_note = reason
            if decision.status == DateStatus.OUT_OF_DATE:
                upsert_flags(s, "image", image_id, [FlagInstance("OUT_OF_DATE", decision.reason)], actor=actor, source="manual")
            elif decision.status == DateStatus.UNKNOWN:
                upsert_flags(s, "image", image_id, [FlagInstance("DATE_UNKNOWN", decision.reason)], actor=actor, source="manual")
            audit(s, actor=actor, action="image.date_review", entity_type="image", entity_id=image_id, before=before, after={"capture_at": image.capture_at.isoformat() if image.capture_at else None, "date_status": image.date_status}, note=reason)
            self._mark_dependents_stale(s, image_id)

    def _mark_dependents_stale(self, s: Any, image_id: int) -> None:
        from rankrecovery.db.models import PlayerSummary, RankObservation, StaffObservation

        player_ids = set(s.execute(select(RankObservation.player_id).where(RankObservation.image_id == image_id)).scalars().all())
        player_ids |= set(s.execute(select(StaffObservation.player_id).where(StaffObservation.image_id == image_id)).scalars().all())
        if player_ids:
            for summary in s.execute(select(PlayerSummary).where(PlayerSummary.player_id.in_(player_ids))).scalars():
                summary.stale = True
