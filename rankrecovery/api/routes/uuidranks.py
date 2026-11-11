"""The uuid_ranks table: one row per resolved UUID with its highest rank."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import ctx_of
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.uuidranks import UuidRankService

router = APIRouter(tags=["uuid-ranks"])


@router.get("/uuid-ranks")
def list_uuid_ranks(request: Request, limit: int = 500, offset: int = 0, search: str = "", rank: str | None = None, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    svc = UuidRankService(ctx_of(request))
    return {"total": svc.count(), "rows": svc.rows(limit=limit, offset=offset, search=search, rank=rank)}


@router.get("/uuid-ranks/export.csv")
def export_uuid_ranks(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> Response:
    return Response(content=UuidRankService(ctx_of(request)).csv_text(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="uuid_ranks.csv"'})


@router.post("/uuid-ranks/rebuild")
def rebuild_uuid_ranks(request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    return UuidRankService(ctx_of(request)).rebuild(actor=principal.name)
