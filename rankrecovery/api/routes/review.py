from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found
from rankrecovery.api.schemas import BulkReview, FlagCreate, Note, ReviewDetection, VerifyObservation
from rankrecovery.core.enums import ApiRole
from rankrecovery.core.flags import FLAG_CATALOGUE
from rankrecovery.services.review import ReviewService

router = APIRouter(tags=["review"])


@router.get("/detections/pending")
def pending_detections(request: Request, username: str | None = None, limit: int = 25, offset: int = 0, _: Principal = Depends(require(ApiRole.STAFF))) -> list[dict]:
    """Detections waiting for a human decision, oldest first, with their image and player context."""
    from sqlalchemy import select

    from rankrecovery.core.usernames import normalise_username_key
    from rankrecovery.db.models import Detection, Flag, Player, SourceImage, Submitter
    from rankrecovery.services.players import find_player_by_username, merge_group_ids
    from rankrecovery.services.search import detection_dict

    ctx = ctx_of(request)
    with ctx.db.read_session() as s:
        stmt = select(Detection).where(Detection.needs_review.is_(True), Detection.status == "needs_review", Detection.superseded_by_id.is_(None))
        if username:
            player = find_player_by_username(s, username)
            ids = merge_group_ids(s, player.id) if player else []
            stmt = stmt.where(Detection.player_id.in_(ids) if ids else Detection.username_key == normalise_username_key(username))
        rows = s.execute(stmt.order_by(Detection.id).limit(limit).offset(offset)).scalars().all()
        out = []
        for d in rows:
            item = detection_dict(d)
            img = s.get(SourceImage, d.image_id)
            sub = s.get(Submitter, img.submitter_id) if img and img.submitter_id else None
            player = s.get(Player, d.player_id) if d.player_id else None
            item["image_filename"] = img.original_filename if img else None
            item["submitter"] = sub.name if sub else "unknown"
            item["player_display_name"] = player.display_name if player else d.normalised_username
            item["flags"] = [f"{f.code}: {f.details}" if f.details else f.code for f in s.execute(select(Flag).where(Flag.entity_type == "detection", Flag.entity_id == d.id, Flag.is_active.is_(True))).scalars()]
            out.append(item)
        return out


@router.post("/detections/{detection_id}/review")
def review_detection(detection_id: int, body: ReviewDetection, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    ctx = ctx_of(request)
    try:
        ReviewService(ctx).review_detection(detection_id, accept=body.accept, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("detection") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    from rankrecovery.db.models import Detection
    from rankrecovery.services.conversion import ConversionService
    from rankrecovery.services.reconcile import ReconcileService

    with ctx.db.read_session() as s:
        d = s.get(Detection, detection_id)
        player_id = d.player_id if d else None
    if player_id:
        ReconcileService(ctx).recompute_player(player_id)
        ConversionService(ctx).recompute([player_id])
    return {"ok": True, "player_id": player_id}


@router.post("/detections/bulk-review")
def bulk_review(body: BulkReview, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    n = ReviewService(ctx_of(request)).bulk_review_detections(body.detection_ids, accept=body.accept, actor=principal.name, note=body.note)
    return {"reviewed": n}


@router.post("/observations/{observation_id}/verify")
def verify_observation(observation_id: int, body: VerifyObservation, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ReviewService(ctx_of(request)).verify_observation(observation_id, verified=body.verified, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("observation") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    return {"ok": True}


@router.post("/observations/{observation_id}/deactivate")
def deactivate_observation(observation_id: int, body: Note, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        ReviewService(ctx_of(request)).deactivate_observation(observation_id, actor=principal.name, reason=body.note)
    except KeyError as exc:
        raise not_found("observation") from exc
    return {"ok": True}


@router.get("/flags")
def list_flags(entity_type: str, entity_id: int, request: Request, include_resolved: bool = True, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return ReviewService(ctx_of(request)).flags_for(entity_type, entity_id, include_resolved=include_resolved)


@router.get("/flags/catalogue")
def flag_catalogue(_: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return [{"code": d.code, "severity": d.severity.value, "description": d.description, "blocks_export_by_default": d.blocks_export_by_default} for d in FLAG_CATALOGUE.values()]


@router.post("/flags")
def add_flag(body: FlagCreate, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    flag_id = ReviewService(ctx_of(request)).add_flag(body.entity_type, body.entity_id, body.code.upper(), body.details, actor=principal.name, note=body.note)
    return {"flag_id": flag_id}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: int, body: Note, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ReviewService(ctx_of(request)).resolve_flag(flag_id, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("flag") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    return {"ok": True}


@router.post("/flags/{flag_id}/reopen")
def reopen_flag(flag_id: int, body: Note, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ReviewService(ctx_of(request)).reopen_flag(flag_id, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("flag") from exc
    return {"ok": True}
