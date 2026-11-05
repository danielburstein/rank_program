"""Build evidence from the database, run the engine, persist derived results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from rankrecovery.core.enums import AiStatus, Credibility, DateStatus, ReviewStatus, SourceType
from rankrecovery.core.evidence import (
    UNKNOWN_SUBMITTER_KEY,
    ImageFacts,
    PlayerEvidence,
    RankObservationFact,
    StaffObservationFact,
    Waiver,
)
from rankrecovery.core.reconciliation import PlayerSummary as EngineSummary
from rankrecovery.core.reconciliation import ReconciliationEngine
from rankrecovery.db.models import (
    Conversion,
    CsvRow,
    Detection,
    ExportBatch,
    Flag,
    Player,
    PlayerSummary,
    RankObservation,
    SourceImage,
    StaffObservation,
    Submitter,
    VerificationResult,
    VerificationSource,
    VerificationWaiver,
)
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import sync_auto_flags
from rankrecovery.services.players import merge_group_ids

log = logging.getLogger(__name__)


class ReconcileService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ------------------------------------------------------------- evidence
    def build_evidence(self, s: Any, player: Player) -> PlayerEvidence:
        ids = merge_group_ids(s, player.id)
        images: dict[int, ImageFacts] = {}
        submitters = {sub.id: sub for sub in s.execute(select(Submitter)).scalars()}

        def facts(image_id: int | None) -> ImageFacts | None:
            if image_id is None:
                return None
            if image_id in images:
                return images[image_id]
            img = s.get(SourceImage, image_id)
            if img is None:
                return None
            sub = submitters.get(img.submitter_id) if img.submitter_id else None
            f = ImageFacts(
                image_id=img.id,
                submitter_key=f"submitter:{sub.id}" if sub else UNKNOWN_SUBMITTER_KEY,
                submitter_name=sub.name if sub else "unknown",
                ai_status=AiStatus(img.ai_status),
                date_status=DateStatus(img.date_status),
                duplicate_group=img.duplicate_group or img.sha256,
                filename=img.original_filename,
                ai_details=img.ai_details,
                date_details=img.capture_reason if img.date_status == DateStatus.OUT_OF_DATE.value else "",
            )
            images[image_id] = f
            return f

        ev = PlayerEvidence(player_id=player.id, usernames=[a.username for a in player.aliases] or [player.display_name], is_bedrock=player.is_bedrock)
        for o in s.execute(select(RankObservation).where(RankObservation.player_id.in_(ids)).order_by(RankObservation.id)).scalars():
            ev.rank_observations.append(
                RankObservationFact(
                    id=o.id,
                    rank=o.rank_canonical,
                    raw_text=o.raw_text,
                    source=SourceType(o.source_type),
                    image=facts(o.image_id),
                    credibility=Credibility(o.credibility),
                    confidence=o.confidence,
                    review_status=ReviewStatus(o.review_status),
                    manually_verified=o.manually_verified,
                    is_active=o.is_active,
                    csv_row_id=o.csv_row_id,
                    csv_batch_key=self._csv_batch_key(s, o.csv_row_id),
                    manual_assertion_id=o.manual_assertion_id,
                    detection_id=o.detection_id,
                    custom_prefix=o.custom_prefix,
                    basis=o.basis,
                )
            )
        for st in s.execute(select(StaffObservation).where(StaffObservation.player_id.in_(ids)).order_by(StaffObservation.id)).scalars():
            ev.staff_observations.append(StaffObservationFact(id=st.id, staff_name=st.staff_canonical, raw_text=st.raw_text, image=facts(st.image_id), has_yinyang=st.has_yinyang, detection_id=st.detection_id, is_active=st.is_active))
        for w in s.execute(select(VerificationWaiver).where(VerificationWaiver.player_id.in_(ids), VerificationWaiver.is_active.is_(True))).scalars():
            ev.waivers.append(Waiver(w.rank_canonical, w.reason, w.created_by))
        name_only = s.execute(
            select(Detection.image_id).where(Detection.player_id.in_(ids), Detection.rank_canonical.is_(None), Detection.staff_canonical.is_(None), Detection.status.in_(["candidate", "needs_review"]), Detection.superseded_by_id.is_(None))
        ).scalars().all()
        for image_id in set(name_only):
            f = facts(image_id)
            if f:
                ev.name_only_images.append(f)
        ev.unresolved_review_count = len(
            s.execute(select(Detection.id).where(Detection.player_id.in_(ids), Detection.needs_review.is_(True), Detection.status == "needs_review", Detection.superseded_by_id.is_(None))).scalars().all()
        )
        for member in ids:
            p = s.get(Player, member)
            if p is not None and p.identity_status == "ambiguous":
                ev.identity_flags.append("IDENTITY_AMBIGUOUS")
        identity_codes = s.execute(select(Flag.code).where(Flag.entity_type == "player", Flag.entity_id.in_(ids), Flag.is_active.is_(True), Flag.source == "manual", Flag.code.in_(["IDENTITY_CONFLICT", "CSV_IDENTITY_CONFLICT", "CSV_CONFLICT", "IDENTITY_AMBIGUOUS"]))).scalars().all()
        ev.identity_flags.extend(c for c in identity_codes if c not in ev.identity_flags)
        return ev

    @staticmethod
    def _csv_batch_key(s: Any, csv_row_id: int | None) -> str | None:
        if csv_row_id is None:
            return None
        row = s.get(CsvRow, csv_row_id)
        return f"batch{row.batch_id}" if row else None

    # -------------------------------------------------------------- compute
    def recompute_player(self, player_id: int, *, actor: str = "recompute") -> EngineSummary | None:
        engine = ReconciliationEngine(self.ctx.policy(), self.ctx.verification_policy())
        policy_version = self.ctx.settings.current_policy_version("rank_policy")
        with self.ctx.db.write_session() as s:
            player = s.get(Player, player_id)
            if player is None:
                return None
            if player.merged_into_id is not None:
                # merged identities carry no summary of their own
                existing = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == player_id)).scalars().first()
                if existing is not None:
                    s.delete(existing)
                return None
            evidence = self.build_evidence(s, player)
            summary = engine.summarise(evidence)
            self._persist(s, player, summary, policy_version.id if policy_version else None, actor)
            return summary

    def _persist(self, s: Any, player: Player, summary: EngineSummary, policy_version_id: int | None, actor: str) -> None:
        row = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == player.id)).scalars().first()
        if row is None:
            row = PlayerSummary(player_id=player.id)
            s.add(row)
        previous_effective = row.effective_rank
        row.effective_rank = summary.effective_rank
        row.highest_observed = summary.highest_observed
        row.highest_confirmed = summary.highest_confirmed
        row.observed_ranks = summary.observed_ranks
        row.staff_ranks = summary.staff_ranks
        row.custom_prefixes = summary.custom_prefixes
        row.provisional = [{"rank": c.rank, "counted": c.counted_sources, "required": c.required_sources, "fallback": c.fallback_rank} for c in summary.provisional]
        row.candidates = [
            {
                "rank": c.rank,
                "status": c.status.value,
                "required": c.required_sources,
                "counted": c.counted_sources,
                "source_keys": c.source_keys,
                "confirmed_by": c.confirmed_by,
                "fallback": c.fallback_rank,
                "notes": c.notes,
                "evidence": [
                    {"observation_id": e.observation_id, "image_id": e.image_id, "submitter": e.submitter_name, "eligible": e.eligible, "counted": e.counted_as_source, "reasons": e.reasons, "source": e.source.value, "basis": e.basis}
                    for e in c.evidence
                ],
            }
            for c in summary.candidates
        ]
        row.active_flags = [f.render() for f in summary.flags]
        row.reasoning = summary.reasoning
        row.clean_rank_images = summary.clean_rank_images
        row.distinct_submitters = summary.distinct_submitters
        row.ai_rank_images = summary.ai_rank_images
        row.csv_records = summary.csv_records
        row.manual_override_active = summary.manual_override_active
        row.has_provisional = bool(summary.provisional)
        row.policy_fingerprint = summary.policy_fingerprint
        row.policy_version_id = policy_version_id
        row.verification_settings = self.ctx.verification_settings_snapshot()
        row.compact_text = f"{player.display_name} : {summary.compact()}"
        row.computed_at = datetime.now(UTC)
        row.stale = False

        for old in s.execute(select(VerificationResult).where(VerificationResult.player_id == player.id)).scalars():
            s.delete(old)
        s.flush()
        submitter_ids = {f"submitter:{sub.id}": sub.id for sub in s.execute(select(Submitter)).scalars()}
        for c in summary.candidates:
            vr = VerificationResult(player_id=player.id, rank_canonical=c.rank, required_sources=c.required_sources, counted_sources=c.counted_sources, status=c.status.value, confirmed_by=c.confirmed_by, fallback_rank=c.fallback_rank, notes=c.notes)
            s.add(vr)
            s.flush()
            for e in c.evidence:
                s.add(VerificationSource(result_id=vr.id, observation_id=e.observation_id, image_id=e.image_id, submitter_id=submitter_ids.get(e.submitter_key), submitter_key=e.submitter_key, submitter_name=e.submitter_name, eligible=e.eligible, counted=e.counted_as_source, source_key=e.source_key, reasons=e.reasons))

        sync_auto_flags(s, "player", player.id, summary.flags, actor=actor)
        if player.review_status == "pending" and not summary.flags:
            player.review_status = "clear"
        conv = s.execute(select(Conversion).where(Conversion.player_id == player.id)).scalars().first()
        if conv is not None:
            conv.stale = True  # flags or candidates may have changed even when the rank did not
        if previous_effective != summary.effective_rank:
            for export in s.execute(select(ExportBatch).where(ExportBatch.stale.is_(False))).scalars():
                export.stale = True
                export.stale_reason = f"effective rank of player {player.id} changed from {previous_effective} to {summary.effective_rank}"

    # ------------------------------------------------------------ bulk ops
    def recompute_many(self, player_ids: list[int], *, progress: Callable[[int, int], None] | None = None, cancel: Callable[[], bool] | None = None) -> int:
        done = 0
        for pid in player_ids:
            if cancel and cancel():
                break
            self.recompute_player(pid)
            done += 1
            if progress:
                progress(done, len(player_ids))
        return done

    def stale_player_ids(self, *, include_missing: bool = True) -> list[int]:
        with self.ctx.db.read_session() as s:
            players = s.execute(select(Player.id).where(Player.merged_into_id.is_(None))).scalars().all()
            summaries = {row.player_id: row.stale for row in s.execute(select(PlayerSummary)).scalars()}
            out = []
            for pid in players:
                if pid not in summaries:
                    if include_missing:
                        out.append(pid)
                elif summaries[pid]:
                    out.append(pid)
            return out

    def all_player_ids(self) -> list[int]:
        with self.ctx.db.read_session() as s:
            return list(s.execute(select(Player.id).where(Player.merged_into_id.is_(None)).order_by(Player.id)).scalars().all())

    def mark_all_stale(self, reason: str) -> int:
        with self.ctx.db.write_session() as s:
            n = 0
            for row in s.execute(select(PlayerSummary)).scalars():
                row.stale = True
                n += 1
            for export in s.execute(select(ExportBatch).where(ExportBatch.stale.is_(False))).scalars():
                export.stale = True
                export.stale_reason = reason
            return n
