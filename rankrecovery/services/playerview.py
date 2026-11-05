"""Read models for players: the Mode 1 table and the full evidence drill-down."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select

from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import (
    Conversion,
    CsvRow,
    Detection,
    ExportRow,
    Flag,
    IdentityMerge,
    IdentityResolution,
    LookupAttemptRow,
    ManualAssertion,
    Player,
    PlayerAlias,
    PlayerSummary,
    RankObservation,
    SourceImage,
    StaffObservation,
    Submitter,
    VerificationResult,
    VerificationWaiver,
)
from rankrecovery.services.context import AppContext
from rankrecovery.services.players import find_player_by_username, merge_group_ids
from rankrecovery.services.review import flag_dict
from rankrecovery.services.search import csv_row_dict, detection_dict, image_dict, lookup_dict


class PlayerViewService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ table
    def table(self, *, search: str = "", limit: int = 200, offset: int = 0, sort: str = "display_name", descending: bool = False, only_stale: bool = False, only_provisional: bool = False, flag: str | None = None) -> dict[str, Any]:
        with self.ctx.db.read_session() as s:
            stmt = select(Player, PlayerSummary).outerjoin(PlayerSummary, PlayerSummary.player_id == Player.id).where(Player.merged_into_id.is_(None))
            if search:
                key = normalise_username_key(search)
                alias_ids = select(PlayerAlias.player_id).where(PlayerAlias.username_key.like(f"%{key}%"))
                stmt = stmt.where(or_(Player.id.in_(alias_ids), Player.canonical_uuid.like(f"%{search.lower()}%")))
            if only_stale:
                stmt = stmt.where(or_(PlayerSummary.stale.is_(True), PlayerSummary.id.is_(None)))
            if only_provisional:
                stmt = stmt.where(PlayerSummary.has_provisional.is_(True))
            if flag:
                stmt = stmt.where(Player.id.in_(select(Flag.entity_id).where(Flag.entity_type == "player", Flag.code == flag.upper(), Flag.is_active.is_(True))))
            sort_col = {
                "display_name": Player.display_name,
                "effective_rank": PlayerSummary.effective_rank,
                "highest_observed": PlayerSummary.highest_observed,
                "submitters": PlayerSummary.distinct_submitters,
                "images": PlayerSummary.clean_rank_images,
                "identity_status": Player.identity_status,
                "updated": Player.updated_at,
            }.get(sort, Player.display_name)
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
            stmt = stmt.order_by(sort_col.desc() if descending else sort_col.asc()).limit(limit).offset(offset)
            rows = []
            for p, summary in s.execute(stmt).all():
                conv = s.execute(select(Conversion).where(Conversion.player_id == p.id)).scalars().first()
                csv_count = len(s.execute(select(CsvRow.id).where(CsvRow.player_id.in_(merge_group_ids(s, p.id)), CsvRow.is_active.is_(True))).scalars().all())
                resolved = s.execute(select(Flag.code).where(Flag.entity_type == "player", Flag.entity_id == p.id, Flag.is_active.is_(False))).scalars().all()
                rows.append(
                    {
                        "player_id": p.id,
                        "display_name": p.display_name,
                        "uuid": p.canonical_uuid,
                        "aliases": [a.username for a in p.aliases],
                        "is_bedrock": p.is_bedrock,
                        "identity_status": p.identity_status,
                        "review_status": p.review_status,
                        "observed_ranks": summary.observed_ranks if summary else [],
                        "highest_observed": summary.highest_observed if summary else None,
                        "highest_confirmed": summary.highest_confirmed if summary else None,
                        "effective_rank": summary.effective_rank if summary else None,
                        "provisional": summary.provisional if summary else [],
                        "staff_ranks": summary.staff_ranks if summary else [],
                        "custom_prefixes": summary.custom_prefixes if summary else [],
                        "clean_rank_images": summary.clean_rank_images if summary else 0,
                        "distinct_submitters": summary.distinct_submitters if summary else 0,
                        "ai_rank_images": summary.ai_rank_images if summary else 0,
                        "csv_records": csv_count,
                        "active_flags": summary.active_flags if summary else [],
                        "resolved_flags": sorted(set(resolved)),
                        "manual_override": summary.manual_override_active if summary else False,
                        "new_rank": conv.new_rank if conv else None,
                        "export_included": conv.included if conv else False,
                        "stale": summary.stale if summary else True,
                        "compact": summary.compact_text if summary else f"{p.display_name} : not computed",
                    }
                )
            return {"total": total, "rows": rows, "limit": limit, "offset": offset}

    # ----------------------------------------------------------------- lookup
    def lookup_by_name(self, username: str) -> dict[str, Any] | None:
        with self.ctx.db.read_session() as s:
            player = find_player_by_username(s, username)
            if player is None:
                return None
            player_id = player.id
        return self.detail(player_id)

    # ----------------------------------------------------------------- detail
    def detail(self, player_id: int) -> dict[str, Any] | None:
        with self.ctx.db.read_session() as s:
            p = s.get(Player, player_id)
            if p is None:
                return None
            ids = merge_group_ids(s, p.id)
            summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == p.id)).scalars().first()
            conv = s.execute(select(Conversion).where(Conversion.player_id == p.id)).scalars().first()
            submitters = {sub.id: sub.name for sub in s.execute(select(Submitter)).scalars()}
            observations = []
            for o in s.execute(select(RankObservation).where(RankObservation.player_id.in_(ids)).order_by(RankObservation.id)).scalars():
                img = s.get(SourceImage, o.image_id) if o.image_id else None
                observations.append(
                    {
                        "observation_id": o.id,
                        "player_id": o.player_id,
                        "rank": o.rank_canonical,
                        "raw_text": o.raw_text,
                        "basis": o.basis,
                        "source_type": o.source_type,
                        "credibility": o.credibility,
                        "confidence": o.confidence,
                        "review_status": o.review_status,
                        "manually_verified": o.manually_verified,
                        "verified_by": o.verified_by,
                        "verified_note": o.verified_note,
                        "is_active": o.is_active,
                        "deactivated_reason": o.deactivated_reason,
                        "custom_prefix": o.custom_prefix,
                        "detection_id": o.detection_id,
                        "csv_row_id": o.csv_row_id,
                        "manual_assertion_id": o.manual_assertion_id,
                        "image_id": o.image_id,
                        "image_filename": img.original_filename if img else None,
                        "submitter": submitters.get(img.submitter_id) if img and img.submitter_id else ("unknown" if img else None),
                        "image_ai_status": img.ai_status if img else None,
                        "image_date_status": img.date_status if img else None,
                        "created_by": o.created_by,
                        "created_at": o.created_at.isoformat() if o.created_at else None,
                    }
                )
            staff = []
            for st in s.execute(select(StaffObservation).where(StaffObservation.player_id.in_(ids)).order_by(StaffObservation.id)).scalars():
                img = s.get(SourceImage, st.image_id) if st.image_id else None
                staff.append({"staff_observation_id": st.id, "staff": st.staff_canonical, "raw_text": st.raw_text, "has_yinyang": st.has_yinyang, "review_status": st.review_status, "is_active": st.is_active, "image_id": st.image_id, "image_filename": img.original_filename if img else None, "submitter": submitters.get(img.submitter_id) if img and img.submitter_id else None, "detection_id": st.detection_id})
            verification = []
            for vr in s.execute(select(VerificationResult).where(VerificationResult.player_id == p.id).order_by(VerificationResult.id)).scalars():
                verification.append(
                    {
                        "rank": vr.rank_canonical,
                        "status": vr.status,
                        "required": vr.required_sources,
                        "counted": vr.counted_sources,
                        "progress": f"{vr.counted_sources}/{vr.required_sources}",
                        "confirmed_by": vr.confirmed_by,
                        "fallback_rank": vr.fallback_rank,
                        "notes": vr.notes,
                        "sources": [{"observation_id": src.observation_id, "image_id": src.image_id, "submitter": src.submitter_name, "eligible": src.eligible, "counted": src.counted, "source_key": src.source_key, "reasons": src.reasons} for src in vr.sources],
                    }
                )
            flags = [flag_dict(f) for f in s.execute(select(Flag).where(Flag.entity_type == "player", Flag.entity_id.in_(ids)).order_by(Flag.is_active.desc(), Flag.id)).scalars()]
            detections = [detection_dict(d) for d in s.execute(select(Detection).where(Detection.player_id.in_(ids)).order_by(Detection.id)).scalars()]
            images = {}
            for d in detections:
                if d["image_id"] not in images:
                    img = s.get(SourceImage, d["image_id"])
                    if img:
                        images[img.id] = image_dict(img)
            for o in observations:
                if o["image_id"] and o["image_id"] not in images:
                    img = s.get(SourceImage, o["image_id"])
                    if img:
                        images[img.id] = image_dict(img)
            csv_rows = [csv_row_dict(r) for r in s.execute(select(CsvRow).where(CsvRow.player_id.in_(ids)).order_by(CsvRow.id)).scalars()]
            lookups = [lookup_dict(r) for r in s.execute(select(LookupAttemptRow).where(LookupAttemptRow.player_id.in_(ids)).order_by(LookupAttemptRow.id.desc()).limit(50)).scalars()]
            resolutions = [{"id": r.id, "uuid": r.uuid, "status": r.status, "source": r.source, "queried_name": r.queried_name, "current_name": r.current_name, "note": r.note, "is_active": r.is_active, "created_at": r.created_at.isoformat() if r.created_at else None} for r in s.execute(select(IdentityResolution).where(IdentityResolution.player_id.in_(ids)).order_by(IdentityResolution.id)).scalars()]
            merges = [{"id": m.id, "survivor": m.survivor_player_id, "merged": m.merged_player_id, "reason": m.reason, "created_by": m.created_by, "created_at": m.created_at.isoformat() if m.created_at else None, "undone_at": m.undone_at.isoformat() if m.undone_at else None} for m in s.execute(select(IdentityMerge).where(or_(IdentityMerge.survivor_player_id.in_(ids), IdentityMerge.merged_player_id.in_(ids)))).scalars()]
            manual = [{"id": m.id, "kind": m.kind, "value": m.value, "reason": m.reason, "created_by": m.created_by, "created_at": m.created_at.isoformat() if m.created_at else None, "is_active": m.is_active, "details": m.details} for m in s.execute(select(ManualAssertion).where(ManualAssertion.player_id.in_(ids)).order_by(ManualAssertion.id)).scalars()]
            waivers = [{"id": w.id, "rank": w.rank_canonical, "reason": w.reason, "created_by": w.created_by, "is_active": w.is_active, "revoked_by": w.revoked_by} for w in s.execute(select(VerificationWaiver).where(VerificationWaiver.player_id.in_(ids))).scalars()]
            exports = [{"batch_id": e.batch_id, "included": e.included, "skip_reason": e.skip_reason, "command": e.command} for e in s.execute(select(ExportRow).where(ExportRow.player_id.in_(ids)).order_by(ExportRow.id.desc()).limit(20)).scalars()]
            return {
                "player": {
                    "player_id": p.id,
                    "display_name": p.display_name,
                    "aliases": sorted({a.username for pid in ids for a in (s.get(Player, pid).aliases if s.get(Player, pid) else [])}),
                    "uuid": p.canonical_uuid,
                    "current_name": p.current_name,
                    "is_bedrock": p.is_bedrock,
                    "identity_status": p.identity_status,
                    "identity_source": p.identity_source,
                    "review_status": p.review_status,
                    "merged_member_ids": ids,
                },
                "summary": {
                    "effective_rank": summary.effective_rank if summary else None,
                    "highest_observed": summary.highest_observed if summary else None,
                    "highest_confirmed": summary.highest_confirmed if summary else None,
                    "observed_ranks": summary.observed_ranks if summary else [],
                    "staff_ranks": summary.staff_ranks if summary else [],
                    "custom_prefixes": summary.custom_prefixes if summary else [],
                    "provisional": summary.provisional if summary else [],
                    "candidates": summary.candidates if summary else [],
                    "reasoning": summary.reasoning if summary else [],
                    "clean_rank_images": summary.clean_rank_images if summary else 0,
                    "distinct_submitters": summary.distinct_submitters if summary else 0,
                    "ai_rank_images": summary.ai_rank_images if summary else 0,
                    "csv_records": summary.csv_records if summary else 0,
                    "manual_override_active": summary.manual_override_active if summary else False,
                    "policy_fingerprint": summary.policy_fingerprint if summary else None,
                    "verification_settings": summary.verification_settings if summary else {},
                    "computed_at": summary.computed_at.isoformat() if summary and summary.computed_at else None,
                    "stale": summary.stale if summary else True,
                    "compact": summary.compact_text if summary else "",
                },
                "conversion": {"old_rank": conv.old_rank, "new_rank": conv.new_rank, "included": conv.included, "reasons": conv.reasons, "stale": conv.stale, "manual_approval": conv.manual_approval, "mapping_fingerprint": conv.mapping_fingerprint} if conv else None,
                "verification": verification,
                "observations": observations,
                "staff_observations": staff,
                "flags": flags,
                "detections": detections,
                "images": list(images.values()),
                "csv_rows": csv_rows,
                "lookups": lookups,
                "identity_resolutions": resolutions,
                "merges": merges,
                "manual_assertions": manual,
                "waivers": waivers,
                "exports": exports,
            }

    def dashboard(self) -> dict[str, Any]:
        with self.ctx.db.read_session() as s:
            players = s.execute(select(func.count(Player.id)).where(Player.merged_into_id.is_(None))).scalar() or 0
            images = s.execute(select(func.count(SourceImage.id))).scalar() or 0
            summaries = s.execute(select(PlayerSummary)).scalars().all()
            confirmed = sum(1 for x in summaries if x.effective_rank and not x.has_provisional)
            provisional = sum(1 for x in summaries if x.has_provisional)
            stale = sum(1 for x in summaries if x.stale) + max(0, players - len(summaries))
            staff_only = sum(1 for x in summaries if x.staff_ranks and not x.observed_ranks)
            bedrock = s.execute(select(func.count(Player.id)).where(Player.is_bedrock.is_(True), Player.merged_into_id.is_(None))).scalar() or 0
            unresolved = s.execute(select(func.count(Player.id)).where(Player.identity_status.in_(["unresolved", "lookup_failed", "ambiguous", "not_attempted", "bedrock_pending"]), Player.merged_into_id.is_(None))).scalar() or 0
            review = s.execute(select(func.count(Detection.id)).where(Detection.needs_review.is_(True), Detection.status == "needs_review", Detection.superseded_by_id.is_(None))).scalar() or 0
            flags = s.execute(select(Flag.code, func.count(Flag.id)).where(Flag.is_active.is_(True), Flag.entity_type == "player").group_by(Flag.code)).all()
            exportable = s.execute(select(func.count(Conversion.id)).where(Conversion.included.is_(True))).scalar() or 0
            csv_rows = s.execute(select(func.count(CsvRow.id)).where(CsvRow.is_active.is_(True))).scalar() or 0
            return {
                "players": players,
                "images": images,
                "confirmed_players": confirmed,
                "provisional_players": provisional,
                "stale_summaries": stale,
                "staff_only_players": staff_only,
                "bedrock_players": bedrock,
                "unresolved_identities": unresolved,
                "detections_needing_review": review,
                "exportable_players": exportable,
                "trusted_csv_rows": csv_rows,
                "flag_counts": {code: n for code, n in flags},
                "vision_configured": self.ctx.vision_available(),
                "database": self.ctx.db.describe_concurrency(),
            }
