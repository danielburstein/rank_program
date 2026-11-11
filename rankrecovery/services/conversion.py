"""Mode 2: old-to-new rank conversion with transparent flag filtering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from rankrecovery.core.conversion import decide_conversion
from rankrecovery.db.models import Conversion, Player, PlayerSummary
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import active_flag_codes, resolved_flag_codes


class ConversionService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def recompute(self, player_ids: list[int] | None = None, *, progress: Callable[[int, int], None] | None = None, cancel: Callable[[], bool] | None = None) -> dict[str, int]:
        policy = self.ctx.policy()
        mapping = self.ctx.mapping()
        flag_policy = self.ctx.flag_policy()
        mapping_version = self.ctx.settings.current_policy_version("mapping")
        input_mode = str(self.ctx.settings.get("conversion.input_mode", "mode1"))
        with self.ctx.db.read_session() as s:
            stmt = select(Player.id).where(Player.merged_into_id.is_(None))
            if player_ids:
                stmt = stmt.where(Player.id.in_(player_ids))
            ids = list(s.execute(stmt.order_by(Player.id)).scalars().all())
        counts = {"included": 0, "excluded": 0, "total": len(ids)}
        for n, pid in enumerate(ids, start=1):
            if cancel and cancel():
                break
            with self.ctx.db.write_session() as s:
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                conv = s.execute(select(Conversion).where(Conversion.player_id == pid)).scalars().first()
                if conv is None:
                    conv = Conversion(player_id=pid)
                    s.add(conv)
                active = active_flag_codes(s, "player", pid)
                resolved = resolved_flag_codes(s, "player", pid)
                effective = summary.effective_rank if summary else None
                verification_ok = bool(summary and summary.effective_rank)
                decision = decide_conversion(
                    effective_old_rank=effective,
                    verification_ok=verification_ok,
                    active_flags=active,
                    resolved_flags=resolved,
                    mapping=mapping,
                    policy=policy,
                    flag_policy=flag_policy,
                    manual_approval=conv.manual_approval,
                )
                reasons = list(decision.reasons)
                if summary is None:
                    reasons.append("no rank summary computed yet")
                elif summary.stale:
                    reasons.append("rank summary is stale; recompute Mode 1 first")
                conv.old_rank = decision.old_rank
                conv.new_rank = decision.new_rank
                conv.included = decision.included and summary is not None and not summary.stale
                conv.reasons = reasons
                conv.inherited_flags = active
                conv.excluded_higher_ranks = [p["rank"] for p in (summary.provisional if summary else [])]
                conv.mapping_fingerprint = mapping.fingerprint()
                conv.mapping_version_id = mapping_version.id if mapping_version else None
                conv.flag_policy = {"allowed_flags": sorted(flag_policy.allowed_active_flags), "allow_resolved": flag_policy.allow_resolved_flags}
                conv.input_mode = input_mode
                conv.computed_at = datetime.now(UTC)
                conv.stale = False
                counts["included" if conv.included else "excluded"] += 1
            if progress:
                progress(n, len(ids))
        return counts

    def stale_player_ids(self) -> list[int]:
        """Players whose conversion is missing or was computed before their rank summary last changed."""
        with self.ctx.db.read_session() as s:
            rows = s.execute(select(Player.id, Conversion.id, Conversion.stale).outerjoin(Conversion, Conversion.player_id == Player.id).where(Player.merged_into_id.is_(None)).order_by(Player.id)).all()
            return [pid for pid, conv_id, stale in rows if conv_id is None or stale]

    def refresh_stale(self, *, progress: Callable[[int, int], None] | None = None, cancel: Callable[[], bool] | None = None) -> dict[str, int]:
        """Recompute only the conversions that are missing or out of date. Cheap when nothing changed."""
        ids = self.stale_player_ids()
        if not ids:
            return {"included": 0, "excluded": 0, "total": 0}
        return self.recompute(ids, progress=progress, cancel=cancel)

    def set_manual_approval(self, player_id: int, approved: bool, *, actor: str, reason: str) -> None:
        from rankrecovery.services.audit import audit

        with self.ctx.db.write_session() as s:
            conv = s.execute(select(Conversion).where(Conversion.player_id == player_id)).scalars().first()
            if conv is None:
                conv = Conversion(player_id=player_id)
                s.add(conv)
            before = {"manual_approval": conv.manual_approval}
            conv.manual_approval = approved
            conv.stale = True
            audit(s, actor=actor, action="conversion.manual_approval", entity_type="player", entity_id=player_id, before=before, after={"manual_approval": approved}, note=reason)

    def preview(self, *, limit: int = 500, offset: int = 0, included: bool | None = None) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            stmt = select(Conversion, Player).join(Player, Player.id == Conversion.player_id)
            if included is not None:
                stmt = stmt.where(Conversion.included.is_(included))
            rows = s.execute(stmt.order_by(Player.display_name).limit(limit).offset(offset)).all()
            return [
                {
                    "player_id": p.id,
                    "display_name": p.display_name,
                    "uuid": p.canonical_uuid,
                    "old_rank": c.old_rank,
                    "new_rank": c.new_rank,
                    "included": c.included,
                    "reasons": c.reasons,
                    "inherited_flags": c.inherited_flags,
                    "excluded_higher_ranks": c.excluded_higher_ranks,
                    "manual_approval": c.manual_approval,
                    "stale": c.stale,
                    "mapping_fingerprint": c.mapping_fingerprint,
                    "computed_at": c.computed_at.isoformat() if c.computed_at else None,
                }
                for c, p in rows
            ]
