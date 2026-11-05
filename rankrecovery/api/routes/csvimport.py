from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found, runner_of
from rankrecovery.api.schemas import CsvManualRow, CsvRowAction
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.csvimport import CsvImportService

router = APIRouter(tags=["csv"])


def _preview_dict(preview) -> dict:
    return {
        "filename": preview.filename,
        "sha256": preview.sha256,
        "encoding": preview.encoding,
        "delimiter": preview.delimiter,
        "headers": preview.headers,
        "mapping": preview.mapping,
        "already_imported_batch_id": preview.already_imported_batch_id,
        "summary": preview.summary,
        "rows": [{"row_number": r.row_number, "raw": r.raw, "username": r.username, "uuid": r.uuid, "rank_raw": r.rank_raw, "rank": r.rank_canonical, "purchase_date": r.purchase_date, "transaction_id": r.transaction_id, "status": r.status, "errors": r.errors, "warnings": r.warnings} for r in preview.rows],
    }


@router.post("/csv/preview")
async def csv_preview(request: Request, file: UploadFile = File(...), mapping: str | None = Form(None), delimiter: str | None = Form(None), _: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    content = await file.read()
    mapping_dict = json.loads(mapping) if mapping else None
    try:
        preview = CsvImportService(ctx_of(request)).preview(content, filename=file.filename or "upload.csv", mapping=mapping_dict, delimiter=delimiter)
    except Exception as exc:
        raise bad_request(exc) from exc
    return _preview_dict(preview)


@router.post("/csv/import")
async def csv_import(request: Request, file: UploadFile = File(...), mapping: str | None = Form(None), delimiter: str | None = Form(None), reject_invalid: bool = Form(True), notes: str = Form(""), recompute: bool = Form(True), principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    content = await file.read()
    mapping_dict = json.loads(mapping) if mapping else None
    try:
        result = CsvImportService(ctx_of(request)).commit(content, filename=file.filename or "upload.csv", mapping=mapping_dict, delimiter=delimiter, reject_invalid=reject_invalid, actor=principal.name, notes=notes)
    except Exception as exc:
        raise bad_request(exc) from exc
    if recompute and result.get("players_touched"):
        result["job_id"] = runner_of(request).submit("recompute_players", {"player_ids": result["players_touched"]}, label="recompute after CSV import", created_by=principal.name)
    return result


@router.post("/csv/manual-row")
def csv_manual_row(body: CsvManualRow, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    result = CsvImportService(ctx_of(request)).add_manual_row(username=body.username, uuid=body.uuid, rank=body.rank, purchase_date=body.purchase_date, transaction_id=body.transaction_id, actor=principal.name, reason=body.reason)
    if result.get("players_touched"):
        result["job_id"] = runner_of(request).submit("recompute_players", {"player_ids": result["players_touched"]}, label="recompute after manual purchase row", created_by=principal.name)
    return result


@router.get("/csv/batches")
def csv_batches(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    from sqlalchemy import select

    from rankrecovery.db.models import CsvBatch

    with ctx_of(request).db.read_session() as s:
        return [{"id": b.id, "filename": b.filename, "sha256": b.sha256, "row_count": b.row_count, "valid_count": b.valid_count, "rejected_count": b.rejected_count, "duplicate_count": b.duplicate_count, "source": b.source, "imported_by": b.imported_by, "imported_at": b.imported_at.isoformat() if b.imported_at else None, "notes": b.notes, "mapping": b.header_mapping} for b in s.execute(select(CsvBatch).order_by(CsvBatch.id.desc())).scalars()]


@router.get("/csv/batches/{batch_id}/rows")
def csv_rows(batch_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    from sqlalchemy import select

    from rankrecovery.db.models import CsvRow
    from rankrecovery.services.search import csv_row_dict

    with ctx_of(request).db.read_session() as s:
        rows = s.execute(select(CsvRow).where(CsvRow.batch_id == batch_id).order_by(CsvRow.row_number)).scalars().all()
        if not rows:
            raise not_found("batch rows")
        return [csv_row_dict(r) for r in rows]


@router.post("/csv/rows/{row_id}/deactivate")
def csv_deactivate(row_id: int, body: CsvRowAction, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        CsvImportService(ctx_of(request)).deactivate_row(row_id, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("csv row") from exc
    return {"ok": True}


@router.post("/csv/rows/{row_id}/reactivate")
def csv_reactivate(row_id: int, body: CsvRowAction, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        CsvImportService(ctx_of(request)).reactivate_row(row_id, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("csv row") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    return {"ok": True}
