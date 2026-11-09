from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found, runner_of
from rankrecovery.api.schemas import ClaimCreate, ClaimDecision, LinkNames, LookupRequest, MergeRequest, Note
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


@router.post("/identity/claims")
def create_claim(body: ClaimCreate, request: Request, principal: Principal = Depends(require(ApiRole.BOT))) -> dict:
    """A player (via the bot) or staff records that old name X belongs to current account Y."""
    try:
        return IdentityService(ctx_of(request)).create_claim(old_username=body.old_username, current_username=body.current_username, actor=principal.name, discord_user_id=body.discord_user_id, discord_username=body.discord_username, note=body.note)
    except KeyError as exc:
        raise not_found(str(exc).strip("'")) from exc
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.get("/identity/claims")
def list_claims(request: Request, status: str = "pending", limit: int = 100, _: Principal = Depends(require(ApiRole.STAFF))) -> list[dict]:
    return IdentityService(ctx_of(request)).list_claims(status=status, limit=limit)


@router.post("/identity/claims/{claim_id}/approve")
def approve_claim(claim_id: int, body: ClaimDecision, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        return IdentityService(ctx_of(request)).decide_claim(claim_id, approve=True, actor=principal.name, note=body.note, force=body.force)
    except KeyError as exc:
        raise not_found("claim") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.post("/identity/claims/{claim_id}/reject")
def reject_claim(claim_id: int, body: ClaimDecision, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        return IdentityService(ctx_of(request)).decide_claim(claim_id, approve=False, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("claim") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.post("/identity/link-names")
def link_names(body: LinkNames, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    """Staff: old name X is current account Y; applied immediately with an audit entry."""
    try:
        return IdentityService(ctx_of(request)).link_by_names(body.old_username, body.current_username, actor=principal.name, note=body.note, force=body.force)
    except KeyError as exc:
        raise not_found(str(exc).strip("'")) from exc
    except ValueError as exc:
        raise bad_request(exc) from exc


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
