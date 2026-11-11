"""The ``uuid_ranks`` table: one row per resolved account with its highest rank.

Several name records can resolve to the same UUID (a player renamed, or two
old names belong to one account), and each record carries its own effective
rank. This table collapses them: for every UUID whose identity is verified or
set by hand, the highest effective rank among its records, the new rank that
maps to, every username that led there and the records involved. Players
without a resolved UUID are left out and untouched; so are resolved players
with no effective rank yet.

Rebuilt in full by ``rebuild()``; cheap enough to run after every lookup,
extraction, recompute or conversion.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from rankrecovery.core.enums import IdentityStatus
from rankrecovery.db.models import Player, PlayerAlias, PlayerSummary, UuidRank
from rankrecovery.services.context import AppContext

log = logging.getLogger(__name__)

RESOLVED = (IdentityStatus.VERIFIED.value, IdentityStatus.MANUAL.value)
CSV_COLUMNS = ["uuid", "current_name", "highest_rank", "new_rank", "records", "ranks_seen", "usernames", "identity_sources", "is_bedrock", "computed_at"]


class UuidRankService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def rebuild(self, *, actor: str = "system") -> dict[str, Any]:
        """Replace every row from the current players and summaries. Returns counts."""
        policy = self.ctx.policy()
        mapping = self.ctx.mapping()
        now = datetime.now(UTC)
        summary = {"uuids": 0, "records_merged": 0, "resolved_without_rank": 0, "unresolved_left_as_is": 0, "computed_at": now.isoformat()}
        with self.ctx.db.write_session() as s:
            aliases: dict[int, set[str]] = {}
            for player_id, username in s.execute(select(PlayerAlias.player_id, PlayerAlias.username)).all():
                aliases.setdefault(player_id, set()).add(username)
            groups: dict[str, list[tuple[Player, PlayerSummary | None]]] = {}
            for player, ps in s.execute(select(Player, PlayerSummary).outerjoin(PlayerSummary, PlayerSummary.player_id == Player.id).where(Player.merged_into_id.is_(None))).all():
                if not player.canonical_uuid or player.identity_status not in RESOLVED:
                    summary["unresolved_left_as_is"] += 1
                    continue
                groups.setdefault(player.canonical_uuid, []).append((player, ps))
            s.execute(delete(UuidRank))
            for uuid, members in groups.items():
                ranks = [ps.effective_rank for _, ps in members if ps is not None and ps.effective_rank]
                if not ranks:
                    summary["resolved_without_rank"] += 1
                    continue
                best = policy.max_rank(ranks) or ranks[0]
                names: set[str] = set()
                for player, _ in members:
                    names.add(player.display_name)
                    names.update(aliases.get(player.id, ()))
                    if player.current_name:
                        names.add(player.current_name)
                current = next((p.current_name for p, _ in members if p.current_name), None)
                s.add(
                    UuidRank(
                        uuid=uuid,
                        current_name=current,
                        highest_rank=best,
                        rank_priority=int(policy.priority(best)),
                        new_rank=mapping.new_rank_for(best, policy),
                        usernames=sorted(names, key=str.lower),
                        player_ids=[p.id for p, _ in members],
                        ranks_seen=policy.sort_ranks(ranks),
                        identity_sources=sorted({p.identity_source for p, _ in members if p.identity_source}),
                        is_bedrock=any(p.is_bedrock for p, _ in members),
                        records=len(members),
                        computed_at=now,
                    )
                )
                summary["uuids"] += 1
                summary["records_merged"] += len(members) - 1
        log.info("uuid_ranks rebuilt by %s: %s", actor, summary)
        return summary

    def rows(self, *, limit: int = 500, offset: int = 0, search: str = "", rank: str | None = None) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            stmt = select(UuidRank)
            if rank:
                stmt = stmt.where(UuidRank.highest_rank == rank.upper())
            rows = s.execute(stmt.order_by(UuidRank.rank_priority.desc(), UuidRank.uuid)).scalars().all()
            needle = search.strip().lower()
            if needle:
                rows = [r for r in rows if needle in r.uuid.lower() or any(needle in str(n).lower() for n in (r.usernames or [])) or (r.current_name or "").lower().find(needle) >= 0]
            return [self._row(r) for r in rows[offset : offset + limit]]

    def count(self) -> int:
        with self.ctx.db.read_session() as s:
            return len(s.execute(select(UuidRank.uuid)).scalars().all())

    def csv_text(self) -> str:
        """The whole table as CSV; list columns are joined with ``|``."""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in self.rows(limit=10_000_000):
            writer.writerow({k: ("|".join(str(x) for x in row[k]) if isinstance(row[k], list) else row[k]) for k in CSV_COLUMNS})
        return buf.getvalue()

    @staticmethod
    def _row(r: UuidRank) -> dict[str, Any]:
        return {
            "uuid": r.uuid,
            "current_name": r.current_name,
            "highest_rank": r.highest_rank,
            "rank_priority": r.rank_priority,
            "new_rank": r.new_rank,
            "records": r.records,
            "ranks_seen": list(r.ranks_seen or []),
            "usernames": list(r.usernames or []),
            "player_ids": list(r.player_ids or []),
            "identity_sources": list(r.identity_sources or []),
            "is_bedrock": bool(r.is_bedrock),
            "computed_at": r.computed_at.isoformat() if r.computed_at else None,
        }


def refresh_uuid_ranks(ctx: AppContext, *, actor: str = "system") -> dict[str, Any] | None:
    """Rebuild after a job; never lets a failure here fail the job that called it."""
    try:
        return UuidRankService(ctx).rebuild(actor=actor)
    except Exception:  # noqa: BLE001 - logged, the calling job's own result stands
        log.exception("uuid_ranks rebuild failed")
        return None
