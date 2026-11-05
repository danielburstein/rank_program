"""Manual review actions: overrides, verification decisions, flags, waivers.

Every action writes an audit entry and creates superseding records instead
of rewriting original extraction or CSV output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from rankrecovery.core.enums import Credibility, ReviewStatus, SourceType
from rankrecovery.db.models import (
    Detection,
    Flag,
    ManualAssertion,
    Player,
    PlayerSummary,
    RankObservation,
    StaffObservation,
    VerificationWaiver,
)
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import add_manual_flag, reopen_flag, resolve_flag


class ReviewService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ----------------------------------------------------------- detections
    def review_detection(self, detection_id: int, *, accept: bool, actor: str, note: str) -> None:
        if not note.strip():
            raise ValueError("a review note is required")
        with self.ctx.db.write_session() as s:
            d = s.get(Detection, detection_id)
            if d is None:
                raise KeyError(detection_id)
            before = {"status": d.status, "needs_review": d.needs_review}
            d.status = "accepted" if accept else "rejected"
            d.needs_review = False
            for obs in s.execute(select(RankObservation).where(RankObservation.detection_id == detection_id)).scalars():
                obs.review_status = (ReviewStatus.ACCEPTED if accept else ReviewStatus.REJECTED).value
                obs.manually_verified = accept
                obs.verified_by = actor
                obs.verified_note = note
            for st in s.execute(select(StaffObservation).where(StaffObservation.detection_id == detection_id)).scalars():
                st.review_status = (ReviewStatus.ACCEPTED if accept else ReviewStatus.REJECTED).value
                st.is_active = accept
            audit(s, actor=actor, action="detection.review", entity_type="detection", entity_id=detection_id, before=before, after={"status": d.status}, note=note)
            self._stale(s, d.player_id)

    def verify_observation(self, observation_id: int, *, verified: bool, actor: str, note: str) -> None:
        """Mark a detection as manually verified (parsing reliability only; never adds a source)."""
        if not note.strip():
            raise ValueError("a review note is required")
        with self.ctx.db.write_session() as s:
            obs = s.get(RankObservation, observation_id)
            if obs is None:
                raise KeyError(observation_id)
            before = {"manually_verified": obs.manually_verified, "review_status": obs.review_status}
            obs.manually_verified = verified
            obs.verified_by = actor
            obs.verified_note = note
            if verified and obs.review_status == ReviewStatus.PENDING.value:
                obs.review_status = ReviewStatus.ACCEPTED.value
            audit(s, actor=actor, action="observation.verify", entity_type="observation", entity_id=observation_id, before=before, after={"manually_verified": verified, "review_status": obs.review_status}, note=note)
            self._stale(s, obs.player_id)

    def deactivate_observation(self, observation_id: int, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            obs = s.get(RankObservation, observation_id)
            if obs is None:
                raise KeyError(observation_id)
            obs.is_active = False
            obs.deactivated_reason = reason
            audit(s, actor=actor, action="observation.deactivate", entity_type="observation", entity_id=observation_id, note=reason)
            self._stale(s, obs.player_id)

    # ----------------------------------------------------------- overrides
    def manual_rank_override(self, player_id: int, rank: str, *, actor: str, reason: str) -> int:
        policy = self.ctx.policy()
        canonical = policy.canonical_rank(rank)
        if canonical is None:
            raise ValueError(f"unknown rank {rank!r}")
        if not reason.strip():
            raise ValueError("a reason is required for a manual override")
        with self.ctx.db.write_session() as s:
            player = s.get(Player, player_id)
            if player is None:
                raise KeyError(player_id)
            assertion = ManualAssertion(player_id=player_id, kind="rank", value=canonical, reason=reason, created_by=actor)
            s.add(assertion)
            s.flush()
            obs = RankObservation(player_id=player_id, manual_assertion_id=assertion.id, source_type=SourceType.MANUAL_OVERRIDE.value, basis="manual", raw_text=rank[:128], rank_canonical=canonical, rank_priority=policy.priority(canonical), credibility=Credibility.MANUAL.value, confidence=1.0, review_status="not_required", manually_verified=True, verified_by=actor, verified_note=reason, created_by=actor)
            s.add(obs)
            audit(s, actor=actor, action="player.manual_rank_override", entity_type="player", entity_id=player_id, after={"rank": canonical}, note=reason)
            self._stale(s, player_id)
            s.flush()
            return assertion.id

    def manual_staff_assertion(self, player_id: int, staff: str, *, actor: str, reason: str) -> int:
        policy = self.ctx.policy()
        canonical = policy.canonical_staff(staff)
        if canonical is None:
            raise ValueError(f"unknown staff prefix {staff!r}")
        with self.ctx.db.write_session() as s:
            assertion = ManualAssertion(player_id=player_id, kind="staff", value=canonical, reason=reason, created_by=actor)
            s.add(assertion)
            s.flush()
            s.add(StaffObservation(player_id=player_id, raw_text=staff[:128], staff_canonical=canonical, review_status="accepted", created_by=actor))
            audit(s, actor=actor, action="player.manual_staff", entity_type="player", entity_id=player_id, after={"staff": canonical}, note=reason)
            self._stale(s, player_id)
            return assertion.id

    # ------------------------------------------------------------- waivers
    def add_waiver(self, player_id: int, rank: str, *, actor: str, reason: str) -> int:
        canonical = self.ctx.policy().canonical_rank(rank)
        if canonical is None:
            raise ValueError(f"unknown rank {rank!r}")
        if not reason.strip():
            raise ValueError("a reason is required to waive the submitter requirement")
        with self.ctx.db.write_session() as s:
            w = VerificationWaiver(player_id=player_id, rank_canonical=canonical, reason=reason, created_by=actor)
            s.add(w)
            s.flush()
            s.add(ManualAssertion(player_id=player_id, kind="waiver", value=canonical, reason=reason, created_by=actor, details={"waiver_id": w.id}))
            audit(s, actor=actor, action="player.waiver_added", entity_type="player", entity_id=player_id, after={"rank": canonical}, note=reason)
            self._stale(s, player_id)
            return w.id

    def revoke_waiver(self, waiver_id: int, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            w = s.get(VerificationWaiver, waiver_id)
            if w is None:
                raise KeyError(waiver_id)
            w.is_active = False
            w.revoked_at = datetime.now(UTC)
            w.revoked_by = actor
            audit(s, actor=actor, action="player.waiver_revoked", entity_type="player", entity_id=w.player_id, after={"rank": w.rank_canonical}, note=reason)
            self._stale(s, w.player_id)

    # --------------------------------------------------------------- flags
    def add_flag(self, entity_type: str, entity_id: int, code: str, details: str, *, actor: str, note: str) -> int:
        with self.ctx.db.write_session() as s:
            row = add_manual_flag(s, entity_type, entity_id, code, details, actor=actor, note=note)
            s.flush()
            audit(s, actor=actor, action="flag.add", entity_type=entity_type, entity_id=entity_id, after={"code": code, "details": details}, note=note)
            if entity_type == "player":
                self._stale(s, entity_id)
            return row.id

    def resolve_flag(self, flag_id: int, *, actor: str, note: str) -> None:
        if not note.strip():
            raise ValueError("a resolution note is required")
        with self.ctx.db.write_session() as s:
            row = resolve_flag(s, flag_id, actor=actor, note=note)
            audit(s, actor=actor, action="flag.resolve", entity_type=row.entity_type, entity_id=row.entity_id, after={"code": row.code}, note=note)
            if row.entity_type == "player":
                self._stale(s, row.entity_id)

    def reopen_flag(self, flag_id: int, *, actor: str, note: str) -> None:
        with self.ctx.db.write_session() as s:
            row = reopen_flag(s, flag_id, actor=actor, note=note)
            audit(s, actor=actor, action="flag.reopen", entity_type=row.entity_type, entity_id=row.entity_id, after={"code": row.code}, note=note)
            if row.entity_type == "player":
                self._stale(s, row.entity_id)

    def set_review_status(self, player_id: int, status: str, *, actor: str, note: str) -> None:
        with self.ctx.db.write_session() as s:
            p = s.get(Player, player_id)
            if p is None:
                raise KeyError(player_id)
            before = {"review_status": p.review_status}
            p.review_status = status
            audit(s, actor=actor, action="player.review_status", entity_type="player", entity_id=player_id, before=before, after={"review_status": status}, note=note)

    def bulk_review_detections(self, detection_ids: list[int], *, accept: bool, actor: str, note: str) -> int:
        n = 0
        for did in detection_ids:
            try:
                self.review_detection(did, accept=accept, actor=actor, note=note)
                n += 1
            except KeyError:
                continue
        return n

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _stale(s: Any, player_id: int | None) -> None:
        if player_id is None:
            return
        summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == player_id)).scalars().first()
        if summary is not None:
            summary.stale = True

    def flags_for(self, entity_type: str, entity_id: int, *, include_resolved: bool = True) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            stmt = select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id)
            if not include_resolved:
                stmt = stmt.where(Flag.is_active.is_(True))
            return [flag_dict(f) for f in s.execute(stmt.order_by(Flag.is_active.desc(), Flag.id)).scalars()]


def flag_dict(f: Flag) -> dict[str, Any]:
    return {
        "id": f.id,
        "entity_type": f.entity_type,
        "entity_id": f.entity_id,
        "code": f.code,
        "severity": f.severity,
        "details": f.details,
        "evidence_ref": f.evidence_ref,
        "source": f.source,
        "created_by": f.created_by,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "is_active": f.is_active,
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
        "resolved_by": f.resolved_by,
        "resolution_note": f.resolution_note,
        "notes": f.notes,
    }
