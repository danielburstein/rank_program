"""Mode 3: UUID lookup persistence, identity resolution and merges."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from rankrecovery.core.enums import IdentityStatus, LookupOutcome
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.identity import normalise_uuid
from rankrecovery.db.models import (
    IdentityMerge,
    IdentityResolution,
    LookupAttemptRow,
    LookupCache,
    ManualAssertion,
    Player,
    PlayerSummary,
    RankObservation,
)
from rankrecovery.identity.base import LookupResult
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import upsert_flags
from rankrecovery.services.players import add_alias, find_player_by_username, get_or_create_player, merge_group_ids, resolve_survivor

log = logging.getLogger(__name__)


class DbLookupCache:
    """Mapping-like cache the LookupService can use: (provider, name_key) -> (expires_monotonic, LookupResult)."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str], default: Any = None) -> Any:
        provider, name_key = key
        with self.ctx.db.read_session() as s:
            row = s.execute(select(LookupCache).where(LookupCache.provider == provider, LookupCache.name_key == name_key)).scalars().first()
            if row is None or (row.expires_at and row.expires_at < datetime.now(UTC)):
                return default
            result = LookupResult(queried_name=name_key, provider=provider, outcome=LookupOutcome(row.outcome), uuid=row.uuid, current_name=row.current_name, failure_code=row.failure_code, message="cached", is_historical=row.is_historical, cached=True)
            remaining = (row.expires_at - datetime.now(UTC)).total_seconds() if row.expires_at else 3600.0
            return (time.monotonic() + max(remaining, 0.0), result)

    def __getitem__(self, key: tuple[str, str]) -> Any:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return self.get(key) is not None  # type: ignore[arg-type]

    def __setitem__(self, key: tuple[str, str], value: Any) -> None:
        provider, name_key = key
        expires_monotonic, result = value
        ttl = max(expires_monotonic - time.monotonic(), 0.0)
        with self._lock, self.ctx.db.write_session() as s:
            row = s.execute(select(LookupCache).where(LookupCache.provider == provider, LookupCache.name_key == name_key)).scalars().first()
            if row is None:
                row = LookupCache(provider=provider, name_key=name_key)
                s.add(row)
            row.uuid = result.uuid
            row.current_name = result.current_name
            row.outcome = result.outcome.value
            row.failure_code = result.failure_code
            row.is_historical = result.is_historical
            row.fetched_at = datetime.now(UTC)
            row.expires_at = datetime.now(UTC) + timedelta(seconds=ttl)

    def pop(self, key: tuple[str, str], default: Any = None) -> Any:
        provider, name_key = key
        with self._lock, self.ctx.db.write_session() as s:
            row = s.execute(select(LookupCache).where(LookupCache.provider == provider, LookupCache.name_key == name_key)).scalars().first()
            if row is None:
                return default
            s.delete(row)
            return default


@dataclass(slots=True)
class LookupSummary:
    resolved: int = 0
    not_found: int = 0
    failed: int = 0
    skipped: int = 0
    merged: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


class IdentityService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # --------------------------------------------------------------- lookups
    def build_lookup_service(self, *, client_factory: Callable[..., Any] | None = None) -> Any:
        from rankrecovery.identity.lookup import build_lookup_service

        settings = self.ctx.settings.get_all(include_secrets=True)
        proxy_text = settings.get("lookup.proxy_list") or None
        return build_lookup_service(settings, proxy_text=proxy_text if isinstance(proxy_text, str) else None, cache=DbLookupCache(self.ctx), client_factory=client_factory)

    def candidate_player_ids(self, *, only_unresolved: bool = True, only_failed: bool = False, only_proxy_failures: bool = False, player_ids: list[int] | None = None) -> list[int]:
        with self.ctx.db.read_session() as s:
            stmt = select(Player).where(Player.merged_into_id.is_(None))
            if player_ids:
                stmt = stmt.where(Player.id.in_(player_ids))
            players = s.execute(stmt.order_by(Player.id)).scalars().all()
            out: list[int] = []
            for p in players:
                if p.identity_status in (IdentityStatus.VERIFIED.value, IdentityStatus.MANUAL.value) and only_unresolved and not only_failed:
                    continue
                if only_failed and p.identity_status != IdentityStatus.LOOKUP_FAILED.value:
                    continue
                if only_proxy_failures:
                    last = s.execute(select(LookupAttemptRow).where(LookupAttemptRow.player_id == p.id).order_by(LookupAttemptRow.id.desc())).scalars().first()
                    if last is None or not (last.failure_code or "").startswith("UUID_LOOKUP_PROXY"):
                        continue
                out.append(p.id)
            return out

    def lookup_players(self, player_ids: list[int], *, service: Any | None = None, force: bool = False, workers: int | None = None, job_id: int | None = None, progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None, actor: str = "lookup") -> LookupSummary:
        service = service or self.build_lookup_service()
        summary = LookupSummary()
        with self.ctx.db.read_session() as s:
            targets: list[tuple[int, str, bool]] = []
            for pid in player_ids:
                p = s.get(Player, pid)
                if p is None or p.merged_into_id is not None:
                    continue
                # query the most recently seen alias first; others are tried only if all fail
                name = p.display_name
                targets.append((pid, name, p.is_bedrock))
        done = 0

        def handle(pid: int, name: str, is_bedrock: bool, result: LookupResult) -> None:
            nonlocal done
            self._persist_result(pid, name, result, job_id=job_id, actor=actor)
            if result.resolved:
                summary.resolved += 1
            elif result.outcome == LookupOutcome.NOT_FOUND:
                summary.not_found += 1
            elif result.outcome == LookupOutcome.SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1
            summary.details.append({"player_id": pid, "name": name, "outcome": result.outcome.value, "uuid": result.uuid, "failure_code": result.failure_code, "cached": result.cached})
            done += 1
            if progress:
                progress(done, len(targets))

        names = [t[1] for t in targets]
        by_name = {t[1].lower(): t for t in targets}
        results: list[LookupResult] = service.resolve_many(names, workers=workers, on_result=None, cancel=cancel, force=force) if names else []
        for result in results:
            pid, name, is_bedrock = by_name[result.queried_name.lower()]
            handle(pid, name, is_bedrock, result)
        summary.merged = self.auto_merge_shared_uuids(actor=actor)
        return summary

    def _persist_result(self, player_id: int, name: str, result: LookupResult, *, job_id: int | None, actor: str) -> None:
        with self.ctx.db.write_session() as s:
            player = s.get(Player, player_id)
            if player is None:
                return
            attempt_ids: list[int] = []
            for a in result.attempts:
                row = LookupAttemptRow(
                    player_id=player_id,
                    queried_name=a.queried_name,
                    provider=a.provider,
                    attempt_no=a.attempt_no,
                    started_at=a.started_at,
                    elapsed_ms=a.elapsed_ms,
                    http_status=a.http_status,
                    outcome=a.outcome.value,
                    failure_code=a.failure_code,
                    error_message=a.error_message[:2000],
                    proxy_id=a.proxy_id,
                    resolved_uuid=a.resolved_uuid,
                    resolved_name=a.resolved_name,
                    response_valid=a.response_valid,
                    raw_excerpt=a.raw_excerpt[:500],
                    job_id=job_id,
                )
                s.add(row)
                s.flush()
                attempt_ids.append(row.id)
            if result.cached and not result.attempts:
                row = LookupAttemptRow(player_id=player_id, queried_name=name, provider=result.provider, attempt_no=0, elapsed_ms=0, http_status=None, outcome=result.outcome.value, failure_code=result.failure_code, error_message="served from cache", job_id=job_id, resolved_uuid=result.uuid, resolved_name=result.current_name, response_valid=result.resolved)
                s.add(row)
                s.flush()
                attempt_ids.append(row.id)
            last_attempt = attempt_ids[-1] if attempt_ids else None
            flags: list[FlagInstance] = []
            if result.resolved:
                uuid = normalise_uuid(result.uuid)
                if player.canonical_uuid and player.canonical_uuid != uuid:
                    flags.append(FlagInstance("IDENTITY_CONFLICT", f"lookup returned {uuid} but player is linked to {player.canonical_uuid} ({player.identity_source})"))
                    player.identity_status = IdentityStatus.AMBIGUOUS.value
                    s.add(IdentityResolution(player_id=player_id, uuid=uuid, status=IdentityStatus.AMBIGUOUS.value, source=result.provider, queried_name=name, current_name=result.current_name, lookup_attempt_id=last_attempt, note="conflicts with existing identity; review required"))
                else:
                    for old in s.execute(select(IdentityResolution).where(IdentityResolution.player_id == player_id, IdentityResolution.is_active.is_(True))).scalars():
                        old.is_active = False
                    status = IdentityStatus.VERIFIED.value
                    note = "historical-name provider match" if result.is_historical else "current-name match; the name may have belonged to someone else historically"
                    player.canonical_uuid = uuid
                    player.current_name = result.current_name
                    player.identity_status = status
                    player.identity_source = result.provider
                    s.add(IdentityResolution(player_id=player_id, uuid=uuid, status=status, source=result.provider, queried_name=name, current_name=result.current_name, lookup_attempt_id=last_attempt, note=note))
                    if result.current_name and result.current_name.lower() != name.lower():
                        add_alias(s, player, result.current_name, source="lookup")
            elif result.outcome == LookupOutcome.NOT_FOUND:
                player.identity_status = IdentityStatus.UNRESOLVED.value
                flags.append(FlagInstance("UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED", f"{name}: {result.message or 'no current account with this name'}"))
                s.add(IdentityResolution(player_id=player_id, uuid=None, status=IdentityStatus.UNRESOLVED.value, source=result.provider, queried_name=name, lookup_attempt_id=last_attempt, note=result.message))
            elif result.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY":
                player.identity_status = IdentityStatus.AMBIGUOUS.value
                for old in s.execute(select(IdentityResolution).where(IdentityResolution.player_id == player_id, IdentityResolution.is_active.is_(True))).scalars():
                    old.is_active = False
                for cand in result.candidates:
                    note = f"candidate from {cand.get('source', result.provider)}: now named {cand.get('current_name') or '?'}" + (" (current holder of the name)" if cand.get("current_holder") else "") + (f", record dated {cand['changed_at']}" if cand.get("changed_at") else "")
                    s.add(IdentityResolution(player_id=player_id, uuid=cand.get("uuid"), status=IdentityStatus.AMBIGUOUS.value, source=str(cand.get("source") or result.provider), queried_name=name, current_name=cand.get("current_name"), lookup_attempt_id=last_attempt, note=note))
                flags.append(FlagInstance("UUID_LOOKUP_AMBIGUOUS_IDENTITY", f"{name}: {result.message}"[:1000]))
                flags.append(FlagInstance("IDENTITY_AMBIGUOUS", f"{len(result.candidates)} candidate accounts; set the UUID manually after review"))
            elif result.outcome == LookupOutcome.SKIPPED:
                if player.is_bedrock:
                    player.identity_status = IdentityStatus.BEDROCK_PENDING.value
                flags.append(FlagInstance(result.failure_code or "UUID_LOOKUP_PROVIDER_UNSUPPORTED", result.message))
            else:
                player.identity_status = IdentityStatus.LOOKUP_FAILED.value
                flags.append(FlagInstance(result.failure_code or "UUID_LOOKUP_CONNECTION_ERROR", f"{name}: {result.message}"))
                s.add(IdentityResolution(player_id=player_id, uuid=None, status=IdentityStatus.LOOKUP_FAILED.value, source=result.provider, queried_name=name, lookup_attempt_id=last_attempt, note=result.message))
            # resolve previous lookup flags, raise current ones
            from rankrecovery.db.models import Flag

            for f in s.execute(select(Flag).where(Flag.entity_type == "player", Flag.entity_id == player_id, Flag.is_active.is_(True), Flag.code.like("UUID_LOOKUP_%"))).scalars():
                f.is_active = False
                f.resolved_at = datetime.now(UTC)
                f.resolved_by = actor
                f.resolution_note = "superseded by a newer lookup"
            upsert_flags(s, "player", player_id, flags, actor=actor, source="manual")
            summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == player_id)).scalars().first()
            if summary:
                summary.stale = True

    # ------------------------------------------------------------- manual uuid
    def set_manual_uuid(self, player_id: int, uuid: str | None, *, actor: str, reason: str) -> None:
        norm = normalise_uuid(uuid) if uuid else None
        if uuid and norm is None:
            raise ValueError("invalid UUID")
        with self.ctx.db.write_session() as s:
            player = s.get(Player, player_id)
            if player is None:
                raise KeyError(player_id)
            before = {"uuid": player.canonical_uuid, "status": player.identity_status}
            assertion = ManualAssertion(player_id=player_id, kind="uuid", value=norm or "", reason=reason, created_by=actor)
            s.add(assertion)
            s.flush()
            for old in s.execute(select(IdentityResolution).where(IdentityResolution.player_id == player_id, IdentityResolution.is_active.is_(True))).scalars():
                old.is_active = False
            player.canonical_uuid = norm
            player.identity_status = IdentityStatus.MANUAL.value if norm else IdentityStatus.UNRESOLVED.value
            player.identity_source = "manual"
            s.add(IdentityResolution(player_id=player_id, uuid=norm, status=player.identity_status, source="manual", manual_assertion_id=assertion.id, note=reason))
            audit(s, actor=actor, action="player.manual_uuid", entity_type="player", entity_id=player_id, before=before, after={"uuid": norm, "status": player.identity_status}, note=reason)
            summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == player_id)).scalars().first()
            if summary:
                summary.stale = True
        self.auto_merge_shared_uuids(actor=actor)

    def add_alias(self, player_id: int, username: str, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            player = s.get(Player, player_id)
            if player is None:
                raise KeyError(player_id)
            add_alias(s, player, username, source="manual")
            s.add(ManualAssertion(player_id=player_id, kind="alias", value=username, reason=reason, created_by=actor))
            audit(s, actor=actor, action="player.add_alias", entity_type="player", entity_id=player_id, after={"alias": username}, note=reason)

    # ----------------------------------------------------------------- merges
    def merge_preview(self, survivor_id: int, merged_id: int) -> dict[str, Any]:
        with self.ctx.db.read_session() as s:
            a = s.get(Player, survivor_id)
            b = s.get(Player, merged_id)
            if a is None or b is None:
                raise KeyError("player not found")
            return {
                "survivor": self._snapshot(s, a),
                "merged": self._snapshot(s, b),
                "uuid_conflict": bool(a.canonical_uuid and b.canonical_uuid and a.canonical_uuid != b.canonical_uuid),
                "after": {"aliases": sorted({x.username for x in a.aliases} | {x.username for x in b.aliases}), "uuid": a.canonical_uuid or b.canonical_uuid},
            }

    @staticmethod
    def _snapshot(s: Any, p: Player) -> dict[str, Any]:
        summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == p.id)).scalars().first()
        return {
            "id": p.id,
            "display_name": p.display_name,
            "uuid": p.canonical_uuid,
            "identity_status": p.identity_status,
            "aliases": [a.username for a in p.aliases],
            "effective_rank": summary.effective_rank if summary else None,
            "observations": len(s.execute(select(RankObservation.id).where(RankObservation.player_id == p.id)).scalars().all()),
        }

    def merge(self, survivor_id: int, merged_id: int, *, actor: str, reason: str, allow_uuid_conflict: bool = False) -> int:
        if survivor_id == merged_id:
            raise ValueError("cannot merge a player into itself")
        with self.ctx.db.write_session() as s:
            a = s.get(Player, survivor_id)
            b = s.get(Player, merged_id)
            if a is None or b is None:
                raise KeyError("player not found")
            a = resolve_survivor(s, a)
            if b.merged_into_id is not None:
                raise ValueError("player is already merged")
            if a.canonical_uuid and b.canonical_uuid and a.canonical_uuid != b.canonical_uuid and not allow_uuid_conflict:
                raise ValueError("players have different verified UUIDs; refusing to merge without explicit override")
            snapshot = {"survivor": self._snapshot(s, a), "merged": self._snapshot(s, b)}
            b.merged_into_id = a.id
            for alias in list(b.aliases):
                add_alias(s, a, alias.username, source="merge")
            if a.canonical_uuid is None and b.canonical_uuid:
                a.canonical_uuid = b.canonical_uuid
                a.identity_status = b.identity_status
                a.identity_source = b.identity_source
            if a.is_bedrock or b.is_bedrock:
                a.is_bedrock = True
            merge = IdentityMerge(survivor_player_id=a.id, merged_player_id=b.id, reason=reason, created_by=actor, snapshot=snapshot)
            s.add(merge)
            s.flush()
            old_summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == b.id)).scalars().first()
            if old_summary:
                s.delete(old_summary)
            from rankrecovery.services.flags import resolve_entity_flags

            resolve_entity_flags(s, "player", b.id, actor="recompute", note=f"identity merged into player {a.id}")
            summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == a.id)).scalars().first()
            if summary:
                summary.stale = True
            audit(s, actor=actor, action="player.merge", entity_type="player", entity_id=a.id, before=snapshot, after={"merged_player_id": b.id}, note=reason)
            return merge.id

    def unmerge(self, merge_id: int, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            merge = s.get(IdentityMerge, merge_id)
            if merge is None:
                raise KeyError(merge_id)
            b = s.get(Player, merge.merged_player_id)
            if b is not None:
                b.merged_into_id = None
            merge.undone_at = datetime.now(UTC)
            for pid in (merge.survivor_player_id, merge.merged_player_id):
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if summary:
                    summary.stale = True
            audit(s, actor=actor, action="player.unmerge", entity_type="player", entity_id=merge.survivor_player_id, after={"merged_player_id": merge.merged_player_id}, note=reason)

    def auto_merge_shared_uuids(self, *, actor: str = "system") -> int:
        """Merge players that share a verified UUID. Same UUID means the same account."""
        merged = 0
        with self.ctx.db.read_session() as s:
            rows = s.execute(select(Player.id, Player.canonical_uuid).where(Player.merged_into_id.is_(None), Player.canonical_uuid.is_not(None), Player.identity_status.in_([IdentityStatus.VERIFIED.value, IdentityStatus.MANUAL.value])).order_by(Player.id)).all()
        groups: dict[str, list[int]] = {}
        for pid, uuid in rows:
            groups.setdefault(uuid, []).append(pid)
        for uuid, ids in groups.items():
            if len(ids) < 2:
                continue
            survivor = ids[0]
            for other in ids[1:]:
                try:
                    self.merge(survivor, other, actor=actor, reason=f"same verified UUID {uuid}")
                    merged += 1
                except ValueError as exc:
                    log.warning("auto-merge skipped for %s/%s: %s", survivor, other, exc)
        return merged

    # ----------------------------------------------------- server-record import
    def import_identity_records(self, content: bytes, *, filename: str, actor: str, create_missing: bool = False) -> dict[str, Any]:
        """Import username<->UUID pairs from the old server's own records.

        Accepts ``usercache.json`` (list of {name, uuid}), any JSON list/dict with
        name+uuid fields, or a CSV with username and uuid columns. Pairs become
        verified identities with source ``server_records``; a pair that
        contradicts an existing verified UUID is flagged for review instead of
        overwriting it.
        """
        records, errors = _parse_identity_records(content)
        summary: dict[str, Any] = {"filename": filename, "records": len(records), "invalid": len(errors), "matched": 0, "already_known": 0, "conflicts": 0, "unmatched": 0, "created": 0, "errors": errors[:50]}
        touched: set[int] = set()
        with self.ctx.db.write_session() as s:
            for rec in records:
                username, uuid = rec["username"], rec["uuid"]
                player = find_player_by_username(s, username)
                if player is None:
                    if not create_missing:
                        summary["unmatched"] += 1
                        continue
                    player, _ = get_or_create_player(s, username, source="server_records")
                    summary["created"] += 1
                add_alias(s, player, username, source="server_records")
                if player.canonical_uuid and player.canonical_uuid != uuid and player.identity_status in (IdentityStatus.VERIFIED.value, IdentityStatus.MANUAL.value):
                    summary["conflicts"] += 1
                    note = f"{filename}: {username} -> {uuid} contradicts verified UUID {player.canonical_uuid} ({player.identity_source})"
                    s.add(IdentityResolution(player_id=player.id, uuid=uuid, status=IdentityStatus.AMBIGUOUS.value, source="server_records", queried_name=username, note=note))
                    upsert_flags(s, "player", player.id, [FlagInstance("IDENTITY_CONFLICT", note[:1000])], actor=actor, source="manual")
                    touched.add(player.id)
                    continue
                if player.canonical_uuid == uuid:
                    summary["already_known"] += 1
                    continue
                for old in s.execute(select(IdentityResolution).where(IdentityResolution.player_id == player.id, IdentityResolution.is_active.is_(True))).scalars():
                    old.is_active = False
                player.canonical_uuid = uuid
                player.identity_status = IdentityStatus.VERIFIED.value
                player.identity_source = "server_records"
                s.add(IdentityResolution(player_id=player.id, uuid=uuid, status=IdentityStatus.VERIFIED.value, source="server_records", queried_name=username, note=f"imported from {filename}" + (f" ({rec['extra']})" if rec.get("extra") else "")))
                summary["matched"] += 1
                touched.add(player.id)
            for pid in touched:
                ps = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if ps:
                    ps.stale = True
            audit(s, actor=actor, action="identity.import_records", entity_type="identity_import", entity_id=None, after={k: v for k, v in summary.items() if k != "errors"})
        summary["merged"] = self.auto_merge_shared_uuids(actor=actor)
        return summary

    # ------------------------------------------------------ current-name refresh
    def refresh_current_names(self, player_ids: list[int] | None = None, *, force: bool = False, provider: Any | None = None, job_id: int | None = None, progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None, actor: str = "lookup") -> dict[str, int]:
        """Fill ``current_name`` from Mojang for players whose UUID is known (Java accounts only)."""
        from rankrecovery.core.identity import is_floodgate_uuid
        from rankrecovery.identity.providers import MojangProfileProvider
        from rankrecovery.identity.ratelimit import RateLimiter

        settings = self.ctx.settings.get_all(include_secrets=True)
        provider = provider or MojangProfileProvider(settings)
        limiter = RateLimiter(float(settings.get("lookup.profile_requests_per_minute", 60)))
        with self.ctx.db.read_session() as s:
            stmt = select(Player).where(Player.merged_into_id.is_(None), Player.canonical_uuid.is_not(None))
            if player_ids:
                stmt = stmt.where(Player.id.in_(player_ids))
            targets = [(p.id, p.canonical_uuid) for p in s.execute(stmt.order_by(Player.id)).scalars() if (force or not p.current_name) and not is_floodgate_uuid(p.canonical_uuid)]
        counts = {"checked": 0, "updated": 0, "unchanged": 0, "not_found": 0, "failed": 0}
        for n, (pid, uuid) in enumerate(targets, start=1):
            if cancel is not None and cancel.is_set():
                break
            limiter.acquire(provider.name, cancel)
            result = provider.lookup(uuid)
            counts["checked"] += 1
            with self.ctx.db.write_session() as s:
                for a in result.attempts:
                    s.add(LookupAttemptRow(player_id=pid, queried_name=a.queried_name, provider=a.provider, attempt_no=a.attempt_no, started_at=a.started_at, elapsed_ms=a.elapsed_ms, http_status=a.http_status, outcome=a.outcome.value, failure_code=a.failure_code, error_message=a.error_message[:2000], resolved_uuid=a.resolved_uuid, resolved_name=a.resolved_name, response_valid=a.response_valid, raw_excerpt=a.raw_excerpt[:500], job_id=job_id))
                player = s.get(Player, pid)
                if player is None:
                    continue
                if result.resolved and result.current_name:
                    if player.current_name != result.current_name:
                        player.current_name = result.current_name
                        add_alias(s, player, result.current_name, source="lookup")
                        counts["updated"] += 1
                    else:
                        counts["unchanged"] += 1
                elif result.outcome == LookupOutcome.NOT_FOUND:
                    counts["not_found"] += 1
                else:
                    counts["failed"] += 1
            if progress:
                progress(n, len(targets))
        return counts

    def identity_table(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            out = []
            for p in s.execute(select(Player).where(Player.merged_into_id.is_(None)).order_by(Player.id).limit(limit).offset(offset)).scalars():
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == p.id)).scalars().first()
                last = s.execute(select(LookupAttemptRow).where(LookupAttemptRow.player_id == p.id).order_by(LookupAttemptRow.id.desc())).scalars().first()
                out.append(
                    {
                        "player_id": p.id,
                        "display_name": p.display_name,
                        "aliases": [a.username for a in p.aliases],
                        "uuid": p.canonical_uuid,
                        "current_name": p.current_name,
                        "identity_status": p.identity_status,
                        "identity_source": p.identity_source,
                        "is_bedrock": p.is_bedrock,
                        "effective_rank": summary.effective_rank if summary else None,
                        "last_lookup": {"provider": last.provider, "outcome": last.outcome, "failure_code": last.failure_code, "http_status": last.http_status, "at": last.started_at.isoformat() if last.started_at else None} if last else None,
                    }
                )
            return out


def merged_member_ids(s: Any, survivor_id: int) -> list[int]:
    return merge_group_ids(s, survivor_id)


def _parse_identity_records(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse usercache.json-style JSON or a CSV with username/uuid columns."""
    import csv
    import io
    import json

    from rankrecovery.core.usernames import validate_username

    text = content.decode("utf-8-sig", errors="replace")
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    stripped = text.lstrip()
    rows: list[dict[str, Any]] = []
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return [], [f"invalid JSON: {exc}"]
        if isinstance(data, dict):
            # {"uuid": "name"} maps or {"players": [...]} wrappers
            inner = next((v for v in data.values() if isinstance(v, list)), None)
            rows = inner if inner is not None else [{"uuid": k, "name": v} for k, v in data.items() if isinstance(v, str)]
        elif isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
    else:
        reader = csv.DictReader(io.StringIO(text))
        rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    name_keys = ("name", "username", "userName", "user_name", "player", "ign", "lastKnownName", "last_known_name")
    uuid_keys = ("uuid", "id", "UUID", "player_uuid", "uniqueId", "unique_id")
    for index, row in enumerate(rows, start=1):
        name = next((str(row[k]) for k in name_keys if row.get(k)), "")
        raw_uuid = next((str(row[k]) for k in uuid_keys if row.get(k)), "")
        uuid = normalise_uuid(raw_uuid) if raw_uuid else None
        check = validate_username(name)
        if not name or not check.valid:
            errors.append(f"row {index}: invalid username {name!r}")
            continue
        if uuid is None:
            errors.append(f"row {index}: invalid uuid {raw_uuid!r} for {name}")
            continue
        extra = row.get("expiresOn") or row.get("last_seen") or ""
        records.append({"username": check.normalised, "uuid": uuid, "extra": str(extra)})
    return records, errors
