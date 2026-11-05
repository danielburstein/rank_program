"""Mode 5: universal search across every table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select

from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import (
    Conversion,
    CsvRow,
    Detection,
    ExportRow,
    Flag,
    LookupAttemptRow,
    Player,
    PlayerAlias,
    PlayerSummary,
    SourceImage,
    StaffObservation,
    Submitter,
)
from rankrecovery.services.context import AppContext


@dataclass(slots=True)
class SearchQuery:
    text: str = ""
    exact: bool = False
    entity_types: list[str] = field(default_factory=lambda: ["player", "image", "detection", "csv_row", "lookup"])
    rank: str | None = None  # old rank, alias or new rank
    highest_rank: str | None = None
    provisional_only: bool = False
    staff: str | None = None
    custom_prefix: str | None = None
    flag_code: str | None = None
    flag_active: bool | None = True
    submitter: str | None = None
    ai_status: str | None = None
    date_status: str | None = None
    identity_status: str | None = None
    export_included: bool | None = None
    lookup_failure_code: str | None = None
    http_status: int | None = None
    min_sources: int | None = None
    limit: int = 200
    offset: int = 0


class SearchService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def search(self, q: SearchQuery) -> dict[str, list[dict[str, Any]]]:
        results: dict[str, list[dict[str, Any]]] = {}
        with self.ctx.db.read_session() as s:
            if "player" in q.entity_types:
                results["players"] = self._players(s, q)
            if "image" in q.entity_types:
                results["images"] = self._images(s, q)
            if "detection" in q.entity_types:
                results["detections"] = self._detections(s, q)
            if "csv_row" in q.entity_types:
                results["csv_rows"] = self._csv_rows(s, q)
            if "lookup" in q.entity_types:
                results["lookups"] = self._lookups(s, q)
        return results

    # ------------------------------------------------------------------ players
    def _players(self, s: Any, q: SearchQuery) -> list[dict[str, Any]]:
        stmt = select(Player).where(Player.merged_into_id.is_(None))
        policy = self.ctx.policy()
        if q.text:
            key = normalise_username_key(q.text)
            if q.exact:
                alias_ids = select(PlayerAlias.player_id).where(PlayerAlias.username_key == key)
                stmt = stmt.where(or_(Player.id.in_(alias_ids), Player.canonical_uuid == q.text.lower().strip(), Player.name_key == key))
            else:
                like = f"%{key}%"
                alias_ids = select(PlayerAlias.player_id).where(PlayerAlias.username_key.like(like))
                stmt = stmt.where(or_(Player.id.in_(alias_ids), Player.canonical_uuid.like(f"%{q.text.lower().strip()}%"), Player.name_key.like(like)))
        if q.identity_status:
            stmt = stmt.where(Player.identity_status == q.identity_status)
        if q.rank or q.highest_rank or q.provisional_only or q.min_sources is not None:
            stmt = stmt.join(PlayerSummary, PlayerSummary.player_id == Player.id)
            if q.rank:
                canonical = policy.canonical_rank(q.rank)
                conv_ids = select(Conversion.player_id).where(func.lower(Conversion.new_rank) == q.rank.lower())
                stmt = stmt.where(or_(PlayerSummary.effective_rank == canonical, PlayerSummary.observed_ranks.like(f'%"{canonical}"%') if canonical else False, Player.id.in_(conv_ids)))
            if q.highest_rank:
                stmt = stmt.where(PlayerSummary.highest_observed == policy.canonical_rank(q.highest_rank))
            if q.provisional_only:
                stmt = stmt.where(PlayerSummary.has_provisional.is_(True))
            if q.min_sources is not None:
                stmt = stmt.where(PlayerSummary.distinct_submitters >= q.min_sources)
        if q.staff:
            canonical_staff = policy.canonical_staff(q.staff) or q.staff.upper()
            stmt = stmt.where(Player.id.in_(select(StaffObservation.player_id).where(StaffObservation.staff_canonical == canonical_staff)))
        if q.custom_prefix:
            stmt = stmt.where(Player.id.in_(select(PlayerSummary.player_id).where(PlayerSummary.custom_prefixes.like(f"%{q.custom_prefix.upper()}%"))))
        if q.flag_code:
            flag_stmt = select(Flag.entity_id).where(Flag.entity_type == "player", Flag.code == q.flag_code.upper())
            if q.flag_active is not None:
                flag_stmt = flag_stmt.where(Flag.is_active.is_(q.flag_active))
            stmt = stmt.where(Player.id.in_(flag_stmt))
        if q.export_included is not None:
            stmt = stmt.where(Player.id.in_(select(ExportRow.player_id).where(ExportRow.included.is_(q.export_included))))
        if q.submitter:
            sub_ids = select(Submitter.id).where(Submitter.name_key.like(f"%{normalise_username_key(q.submitter)}%"))
            img_ids = select(SourceImage.id).where(SourceImage.submitter_id.in_(sub_ids))
            stmt = stmt.where(Player.id.in_(select(Detection.player_id).where(Detection.image_id.in_(img_ids))))
        rows = s.execute(stmt.order_by(Player.display_name).limit(q.limit).offset(q.offset)).scalars().all()
        out = []
        for p in rows:
            summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == p.id)).scalars().first()
            out.append(
                {
                    "player_id": p.id,
                    "display_name": p.display_name,
                    "aliases": [a.username for a in p.aliases],
                    "uuid": p.canonical_uuid,
                    "identity_status": p.identity_status,
                    "is_bedrock": p.is_bedrock,
                    "effective_rank": summary.effective_rank if summary else None,
                    "highest_observed": summary.highest_observed if summary else None,
                    "provisional": summary.provisional if summary else [],
                    "staff_ranks": summary.staff_ranks if summary else [],
                    "active_flags": summary.active_flags if summary else [],
                    "compact": summary.compact_text if summary else "",
                    "stale": summary.stale if summary else True,
                }
            )
        return out

    # ------------------------------------------------------------------- images
    def _images(self, s: Any, q: SearchQuery) -> list[dict[str, Any]]:
        stmt = select(SourceImage)
        if q.text:
            t = q.text.strip()
            if q.exact:
                stmt = stmt.where(or_(SourceImage.original_filename == t, SourceImage.sha256 == t.lower(), SourceImage.phash == t.lower()))
            else:
                like = f"%{t}%"
                stmt = stmt.where(or_(SourceImage.original_filename.like(like), SourceImage.sha256.like(f"{t.lower()}%"), SourceImage.extracted_text.like(like), SourceImage.original_path.like(like)))
        if q.submitter:
            sub_ids = select(Submitter.id).where(Submitter.name_key.like(f"%{normalise_username_key(q.submitter)}%"))
            stmt = stmt.where(SourceImage.submitter_id.in_(sub_ids))
        if q.ai_status:
            stmt = stmt.where(SourceImage.ai_status == q.ai_status)
        if q.date_status:
            stmt = stmt.where(SourceImage.date_status == q.date_status)
        if q.flag_code:
            flag_stmt = select(Flag.entity_id).where(Flag.entity_type == "image", Flag.code == q.flag_code.upper())
            if q.flag_active is not None:
                flag_stmt = flag_stmt.where(Flag.is_active.is_(q.flag_active))
            stmt = stmt.where(SourceImage.id.in_(flag_stmt))
        rows = s.execute(stmt.order_by(SourceImage.id.desc()).limit(q.limit).offset(q.offset)).scalars().all()
        return [image_dict(i) for i in rows]

    # --------------------------------------------------------------- detections
    def _detections(self, s: Any, q: SearchQuery) -> list[dict[str, Any]]:
        stmt = select(Detection).where(Detection.superseded_by_id.is_(None))
        if q.text:
            t = q.text.strip()
            if q.exact:
                stmt = stmt.where(or_(Detection.raw_text == t, Detection.username_key == normalise_username_key(t), Detection.raw_prefix == t))
            else:
                like = f"%{t}%"
                stmt = stmt.where(or_(Detection.raw_text.like(like), Detection.username_key.like(f"%{normalise_username_key(t)}%"), Detection.raw_prefix.like(like)))
        if q.rank:
            stmt = stmt.where(Detection.rank_canonical == self.ctx.policy().canonical_rank(q.rank))
        if q.staff:
            stmt = stmt.where(Detection.staff_canonical == (self.ctx.policy().canonical_staff(q.staff) or q.staff.upper()))
        if q.custom_prefix:
            stmt = stmt.where(Detection.custom_prefix.like(f"%{q.custom_prefix.upper()}%"))
        if q.flag_code:
            flag_stmt = select(Flag.entity_id).where(Flag.entity_type == "detection", Flag.code == q.flag_code.upper())
            if q.flag_active is not None:
                flag_stmt = flag_stmt.where(Flag.is_active.is_(q.flag_active))
            stmt = stmt.where(Detection.id.in_(flag_stmt))
        rows = s.execute(stmt.order_by(Detection.id.desc()).limit(q.limit).offset(q.offset)).scalars().all()
        return [detection_dict(d) for d in rows]

    # ----------------------------------------------------------------- csv rows
    def _csv_rows(self, s: Any, q: SearchQuery) -> list[dict[str, Any]]:
        stmt = select(CsvRow)
        if q.text:
            t = q.text.strip()
            key = normalise_username_key(t)
            if q.exact:
                stmt = stmt.where(or_(CsvRow.username_key == key, CsvRow.uuid == t.lower(), CsvRow.transaction_id == t))
            else:
                stmt = stmt.where(or_(CsvRow.username_key.like(f"%{key}%"), CsvRow.uuid.like(f"%{t.lower()}%"), CsvRow.transaction_id.like(f"%{t}%")))
        if q.rank:
            stmt = stmt.where(CsvRow.rank_canonical == self.ctx.policy().canonical_rank(q.rank))
        rows = s.execute(stmt.order_by(CsvRow.id.desc()).limit(q.limit).offset(q.offset)).scalars().all()
        return [csv_row_dict(r) for r in rows]

    # ------------------------------------------------------------------ lookups
    def _lookups(self, s: Any, q: SearchQuery) -> list[dict[str, Any]]:
        stmt = select(LookupAttemptRow)
        if q.text:
            key = normalise_username_key(q.text)
            stmt = stmt.where(or_(func.lower(LookupAttemptRow.queried_name) == key if q.exact else func.lower(LookupAttemptRow.queried_name).like(f"%{key}%"), LookupAttemptRow.resolved_uuid.like(f"%{q.text.lower()}%")))
        if q.lookup_failure_code:
            stmt = stmt.where(LookupAttemptRow.failure_code == q.lookup_failure_code.upper())
        if q.http_status is not None:
            stmt = stmt.where(LookupAttemptRow.http_status == q.http_status)
        rows = s.execute(stmt.order_by(LookupAttemptRow.id.desc()).limit(q.limit).offset(q.offset)).scalars().all()
        return [lookup_dict(r) for r in rows]


# --------------------------------------------------------------------------- dict helpers


def image_dict(i: SourceImage) -> dict[str, Any]:
    return {
        "image_id": i.id,
        "filename": i.original_filename,
        "path": i.original_path,
        "sha256": i.sha256,
        "phash": i.phash,
        "format": i.format,
        "width": i.width,
        "height": i.height,
        "frames": i.frame_count,
        "submitter_id": i.submitter_id,
        "submitter": i.submitter.name if i.submitter else None,
        "batch_id": i.import_batch_id,
        "imported_at": i.imported_at.isoformat() if i.imported_at else None,
        "file_modified_at": i.file_modified_at.isoformat() if i.file_modified_at else None,
        "capture_at": i.capture_at.isoformat() if i.capture_at else None,
        "capture_source": i.capture_source,
        "capture_reason": i.capture_reason,
        "date_status": i.date_status,
        "ai_status": i.ai_status,
        "ai_details": i.ai_details,
        "duplicate_group": i.duplicate_group,
        "availability": i.availability,
        "last_run_id": i.last_extraction_run_id,
    }


def detection_dict(d: Detection) -> dict[str, Any]:
    return {
        "detection_id": d.id,
        "run_id": d.run_id,
        "image_id": d.image_id,
        "player_id": d.player_id,
        "region": d.region,
        "source_type": d.source_type,
        "raw_text": d.raw_text,
        "raw_prefix": d.raw_prefix,
        "normalised_prefix": d.normalised_prefix,
        "prefix_kind": d.prefix_kind,
        "raw_username": d.raw_username,
        "username": d.normalised_username,
        "username_valid": d.username_valid,
        "is_bedrock": d.is_bedrock,
        "partial": d.partial,
        "unreadable_username": d.unreadable_username,
        "status": d.status,
        "confidence": d.confidence,
        "bbox": d.bbox,
        "prefix_bbox": d.prefix_bbox,
        "username_bbox": d.username_bbox,
        "leading_icon": d.leading_icon,
        "trailing_icon": d.trailing_icon,
        "rank": d.rank_canonical,
        "staff": d.staff_canonical,
        "custom_prefix": d.custom_prefix,
        "trailing_icon_rank": d.trailing_icon_rank,
        "misread_of": d.misread_of,
        "needs_review": d.needs_review,
        "notes": d.notes,
        "manual": d.manual,
        "created_by": d.created_by,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def csv_row_dict(r: CsvRow) -> dict[str, Any]:
    return {
        "csv_row_id": r.id,
        "batch_id": r.batch_id,
        "row_number": r.row_number,
        "raw_row": r.raw_row,
        "username": r.username,
        "uuid": r.uuid,
        "rank_raw": r.rank_raw,
        "rank": r.rank_canonical,
        "purchase_date": r.purchase_date,
        "transaction_id": r.transaction_id,
        "validation_status": r.validation_status,
        "validation_errors": r.validation_errors,
        "player_id": r.player_id,
        "is_active": r.is_active,
        "deactivated_reason": r.deactivated_reason,
    }


def lookup_dict(r: LookupAttemptRow) -> dict[str, Any]:
    return {
        "attempt_id": r.id,
        "player_id": r.player_id,
        "queried_name": r.queried_name,
        "provider": r.provider,
        "attempt_no": r.attempt_no,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "elapsed_ms": r.elapsed_ms,
        "http_status": r.http_status,
        "outcome": r.outcome,
        "failure_code": r.failure_code,
        "error_message": r.error_message,
        "proxy_id": r.proxy_id,
        "resolved_uuid": r.resolved_uuid,
        "resolved_name": r.resolved_name,
        "response_valid": r.response_valid,
    }
