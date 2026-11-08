from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found, runner_of
from rankrecovery.api.schemas import LookupRequest, MergeRequest, Note
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.identity import IdentityService

router = APIRouter(tags=["identity"])


@router.post("/identity/lookup")
def lookup(body: LookupRequest, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    job_id = runner_of(request).submit("lookup_uuids", body.model_dump(), label="UUID lookup", created_by=principal.name)
    return {"job_id": job_id}


@router.get("/identity")
def identity_table(request: Request, limit: int = 500, offset: int = 0, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return IdentityService(ctx_of(request)).identity_table(limit=limit, offset=offset)


@router.post("/identity/merge/preview")
def merge_preview(body: MergeRequest, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        return IdentityService(ctx_of(request)).merge_preview(body.survivor_id, body.merged_id)
    except KeyError as exc:
        raise not_found("player") from exc


@router.post("/identity/merge")
def merge(body: MergeRequest, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        merge_id = IdentityService(ctx_of(request)).merge(body.survivor_id, body.merged_id, actor=principal.name, reason=body.reason, allow_uuid_conflict=body.allow_uuid_conflict)
    except KeyError as exc:
        raise not_found("player") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    job_id = runner_of(request).submit("recompute_players", {"player_ids": [body.survivor_id]}, label="recompute after merge", created_by=principal.name)
    return {"merge_id": merge_id, "job_id": job_id}


@router.post("/identity/unmerge/{merge_id}")
def unmerge(merge_id: int, body: Note, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        IdentityService(ctx_of(request)).unmerge(merge_id, actor=principal.name, reason=body.note)
    except KeyError as exc:
        raise not_found("merge") from exc
    job_id = runner_of(request).submit("recompute_players", {}, label="recompute after unmerge", created_by=principal.name)
    return {"ok": True, "job_id": job_id}


@router.post("/identity/import")
async def identity_import(request: Request, file: UploadFile = File(...), create_missing: bool = Form(False), principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    content = await file.read()
    summary = IdentityService(ctx_of(request)).import_identity_records(content, filename=file.filename or "records", actor=principal.name, create_missing=create_missing)
    if summary["matched"] or summary["conflicts"]:
        summary["job_id"] = runner_of(request).submit("recompute_players", {}, label="recompute after identity import", created_by=principal.name)
    return summary


@router.post("/identity/refresh-names")
def refresh_names(request: Request, force: bool = False, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    job_id = runner_of(request).submit("refresh_names", {"force": force}, label="refresh current names", created_by=principal.name)
    return {"job_id": job_id}


@router.get("/identity/proxies")
def proxy_status(request: Request, _: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    from rankrecovery.identity.proxies import parse_proxy_list

    text = ctx_of(request).settings.get_all(include_secrets=True).get("lookup.proxy_list") or ""
    parsed = parse_proxy_list(str(text))
    return {"count": len(parsed.entries), "entries": [e.safe_id for e in parsed.entries], "errors": parsed.errors}
