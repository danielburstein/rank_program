"""Audit log helper."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from rankrecovery.db.models import AuditLog


def audit(session: Session, *, actor: str, action: str, entity_type: str = "", entity_id: int | None = None, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None, note: str = "") -> AuditLog:
    entry = AuditLog(actor=actor or "system", action=action, entity_type=entity_type, entity_id=entity_id, before=before, after=after, note=note)
    session.add(entry)
    return entry
