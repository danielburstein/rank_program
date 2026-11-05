from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import ctx_of
from rankrecovery.api.schemas import SearchBody
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.search import SearchQuery, SearchService

router = APIRouter(tags=["search"])


@router.post("/search")
def search(body: SearchBody, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    return SearchService(ctx_of(request)).search(SearchQuery(**body.model_dump()))


@router.get("/audit")
def audit(request: Request, entity_type: str | None = None, entity_id: int | None = None, limit: int = 200, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    from sqlalchemy import select

    from rankrecovery.db.models import AuditLog

    with ctx_of(request).db.read_session() as s:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        return [{"id": a.id, "actor": a.actor, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id, "before": a.before, "after": a.after, "note": a.note, "created_at": a.created_at.isoformat() if a.created_at else None} for a in s.execute(stmt).scalars()]
