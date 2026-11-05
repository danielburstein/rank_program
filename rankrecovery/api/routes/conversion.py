from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import ctx_of, runner_of
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.conversion import ConversionService

router = APIRouter(tags=["conversion"])


@router.post("/conversion/run")
def run_conversion(request: Request, sync: bool = False, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    if sync:
        return ConversionService(ctx_of(request)).recompute()
    job_id = runner_of(request).submit("convert_players", {}, label="Mode 2 conversion", created_by=principal.name)
    return {"job_id": job_id}


@router.get("/conversion/preview")
def conversion_preview(request: Request, included: bool | None = None, limit: int = 500, offset: int = 0, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return ConversionService(ctx_of(request)).preview(limit=limit, offset=offset, included=included)
