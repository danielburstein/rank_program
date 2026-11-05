from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import ctx_of, not_found
from rankrecovery.api.schemas import ExportWrite
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.export import ExportService

router = APIRouter(tags=["export"])


@router.get("/export/preview")
def export_preview(request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    return ExportService(ctx_of(request)).preview()


@router.post("/export/write")
def export_write(body: ExportWrite, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        return ExportService(ctx_of(request)).write(path=body.path, actor=principal.name, staff_warning_acknowledged=body.staff_warning_acknowledged, options=body.options)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/export/history")
def export_history(request: Request, limit: int = 50, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return ExportService(ctx_of(request)).history(limit=limit)


@router.get("/export/{batch_id}")
def export_detail(batch_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    data = ExportService(ctx_of(request)).batch_detail(batch_id)
    if data is None:
        raise not_found("export batch")
    return data
