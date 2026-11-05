from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Query as Q

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found
from rankrecovery.api.schemas import (
    AliasRequest,
    ConversionApproval,
    ManualUuid,
    RankOverride,
    ReviewStatusBody,
    StaffAssertion,
    WaiverRequest,
)
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.conversion import ConversionService
from rankrecovery.services.identity import IdentityService
from rankrecovery.services.playerview import PlayerViewService
from rankrecovery.services.reconcile import ReconcileService
from rankrecovery.services.review import ReviewService

router = APIRouter(tags=["players"])


@router.get("/dashboard")
def dashboard(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    return PlayerViewService(ctx_of(request)).dashboard()


@router.get("/players")
def players_table(
    request: Request,
    search: str = "",
    limit: int = Q(200, le=2000),
    offset: int = 0,
    sort: str = "display_name",
    descending: bool = False,
    only_stale: bool = False,
    only_provisional: bool = False,
    flag: str | None = None,
    _: Principal = Depends(require(ApiRole.READONLY)),
) -> dict:
    return PlayerViewService(ctx_of(request)).table(search=search, limit=limit, offset=offset, sort=sort, descending=descending, only_stale=only_stale, only_provisional=only_provisional, flag=flag)


@router.get("/players/by-name/{username}")
def player_by_name(username: str, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    data = PlayerViewService(ctx_of(request)).lookup_by_name(username)
    if data is None:
        raise not_found("player")
    return data


@router.get("/players/{player_id}")
def player_detail(player_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    data = PlayerViewService(ctx_of(request)).detail(player_id)
    if data is None:
        raise not_found("player")
    return data


@router.post("/players/{player_id}/recompute")
def recompute_player(player_id: int, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    ctx = ctx_of(request)
    ReconcileService(ctx).recompute_player(player_id)
    ConversionService(ctx).recompute([player_id])
    data = PlayerViewService(ctx).detail(player_id)
    if data is None:
        raise not_found("player")
    return data


@router.post("/players/{player_id}/override-rank")
def override_rank(player_id: int, body: RankOverride, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    try:
        assertion_id = ReviewService(ctx).manual_rank_override(player_id, body.rank, actor=principal.name, reason=body.reason)
    except (ValueError, KeyError) as exc:
        raise bad_request(exc) from exc
    ReconcileService(ctx).recompute_player(player_id)
    return {"assertion_id": assertion_id}


@router.post("/players/{player_id}/staff")
def staff_assertion(player_id: int, body: StaffAssertion, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    try:
        assertion_id = ReviewService(ctx).manual_staff_assertion(player_id, body.staff, actor=principal.name, reason=body.reason)
    except (ValueError, KeyError) as exc:
        raise bad_request(exc) from exc
    ReconcileService(ctx).recompute_player(player_id)
    return {"assertion_id": assertion_id}


@router.post("/players/{player_id}/waivers")
def add_waiver(player_id: int, body: WaiverRequest, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    try:
        waiver_id = ReviewService(ctx).add_waiver(player_id, body.rank, actor=principal.name, reason=body.reason)
    except ValueError as exc:
        raise bad_request(exc) from exc
    ReconcileService(ctx).recompute_player(player_id)
    return {"waiver_id": waiver_id}


@router.delete("/waivers/{waiver_id}")
def revoke_waiver(waiver_id: int, request: Request, reason: str = "revoked", principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    try:
        ReviewService(ctx).revoke_waiver(waiver_id, actor=principal.name, reason=reason)
    except KeyError as exc:
        raise not_found("waiver") from exc
    return {"ok": True}


@router.post("/players/{player_id}/uuid")
def manual_uuid(player_id: int, body: ManualUuid, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    try:
        IdentityService(ctx).set_manual_uuid(player_id, body.uuid, actor=principal.name, reason=body.reason)
    except (ValueError, KeyError) as exc:
        raise bad_request(exc) from exc
    ReconcileService(ctx).recompute_player(player_id)
    return {"ok": True}


@router.post("/players/{player_id}/aliases")
def add_alias(player_id: int, body: AliasRequest, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        IdentityService(ctx_of(request)).add_alias(player_id, body.username, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("player") from exc
    return {"ok": True}


@router.post("/players/{player_id}/review-status")
def review_status(player_id: int, body: ReviewStatusBody, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ReviewService(ctx_of(request)).set_review_status(player_id, body.status, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("player") from exc
    return {"ok": True}


@router.post("/players/{player_id}/conversion-approval")
def conversion_approval(player_id: int, body: ConversionApproval, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    ConversionService(ctx).set_manual_approval(player_id, body.approved, actor=principal.name, reason=body.reason)
    ConversionService(ctx).recompute([player_id])
    return {"ok": True}


@router.get("/players/{player_id}/overlay/{image_id}")
def player_overlay(player_id: int, image_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> Response:
    from rankrecovery.api.routes.images import render_overlay

    data = PlayerViewService(ctx_of(request)).detail(player_id)
    if data is None:
        raise not_found("player")
    return render_overlay(ctx_of(request), image_id, highlight=data["player"]["display_name"])
