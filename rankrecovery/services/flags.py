"""Structured flag persistence with resolution history."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from rankrecovery.core.flags import FlagInstance, flag_definition
from rankrecovery.db.models import Flag


def upsert_flags(session: Session, entity_type: str, entity_id: int, flags: list[FlagInstance], *, actor: str = "system", source: str = "auto") -> list[Flag]:
    """Insert flags that are not already active (same code + details)."""
    existing = session.execute(select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.is_active.is_(True))).scalars().all()
    active = {(f.code, f.details): f for f in existing}
    out: list[Flag] = []
    for f in flags:
        key = (f.code, f.details)
        if key in active:
            out.append(active[key])
            continue
        row = Flag(entity_type=entity_type, entity_id=entity_id, code=f.code, severity=f.effective_severity.value, details=f.details, evidence_ref=f.evidence_ref, source=source, created_by=actor)
        session.add(row)
        out.append(row)
        active[key] = row
    return out


def sync_auto_flags(session: Session, entity_type: str, entity_id: int, flags: list[FlagInstance], *, actor: str = "recompute") -> tuple[int, int]:
    """Make the active automatic flags equal ``flags``.

    Flags no longer raised are *resolved* (kept with history), never deleted.
    Manual flags are left untouched. Returns (added, resolved).
    """
    wanted = {(f.code, f.details): f for f in flags}
    existing = session.execute(select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.is_active.is_(True), Flag.source == "auto")).scalars().all()
    resolved = 0
    present: set[tuple[str, str]] = set()
    for row in existing:
        key = (row.code, row.details)
        if key in wanted:
            present.add(key)
            continue
        row.is_active = False
        row.resolved_at = datetime.now(UTC)
        row.resolved_by = actor
        row.resolution_note = "no longer raised after recomputation"
        resolved += 1
    added = 0
    for key, f in wanted.items():
        if key in present:
            continue
        session.add(Flag(entity_type=entity_type, entity_id=entity_id, code=f.code, severity=f.effective_severity.value, details=f.details, evidence_ref=f.evidence_ref, source="auto", created_by=actor))
        added += 1
    return added, resolved


def add_manual_flag(session: Session, entity_type: str, entity_id: int, code: str, details: str, *, actor: str, note: str = "") -> Flag:
    row = Flag(entity_type=entity_type, entity_id=entity_id, code=code, severity=flag_definition(code).severity.value, details=details, source="manual", created_by=actor, notes=note)
    session.add(row)
    return row


def resolve_flag(session: Session, flag_id: int, *, actor: str, note: str) -> Flag:
    row = session.get(Flag, flag_id)
    if row is None:
        raise KeyError(flag_id)
    row.is_active = False
    row.resolved_at = datetime.now(UTC)
    row.resolved_by = actor
    row.resolution_note = note
    return row


def reopen_flag(session: Session, flag_id: int, *, actor: str, note: str) -> Flag:
    row = session.get(Flag, flag_id)
    if row is None:
        raise KeyError(flag_id)
    row.is_active = True
    row.resolved_at = None
    row.resolved_by = None
    row.resolution_note = None
    row.notes = (row.notes + "\n" if row.notes else "") + f"reopened by {actor}: {note}"
    return row


def active_flag_codes(session: Session, entity_type: str, entity_id: int) -> list[str]:
    return list(session.execute(select(Flag.code).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.is_active.is_(True))).scalars().all())


def resolved_flag_codes(session: Session, entity_type: str, entity_id: int, *, human_only: bool = True) -> list[str]:
    """Codes of resolved flags.

    With ``human_only`` (the default used by conversion), flags that the
    recompute step resolved because their condition no longer holds are
    ignored; only flags a person resolved count as "resolved flags".
    """
    stmt = select(Flag.code).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.is_active.is_(False))
    if human_only:
        stmt = stmt.where(Flag.resolved_by != "recompute")
    return list(session.execute(stmt).scalars().all())


def resolve_entity_flags(session: Session, entity_type: str, entity_id: int, *, actor: str, note: str) -> int:
    n = 0
    for row in session.execute(select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.is_active.is_(True))).scalars():
        row.is_active = False
        row.resolved_at = datetime.now(UTC)
        row.resolved_by = actor
        row.resolution_note = note
        n += 1
    return n
