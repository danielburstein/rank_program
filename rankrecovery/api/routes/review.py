from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found
from rankrecovery.api.schemas import BulkReview, FlagCreate, Note, ReviewDetection, VerifyObservation
from rankrecovery.core.enums import ApiRole
from rankrecovery.core.flags import FLAG_CATALOGUE
from rankrecovery.services.review import ReviewService

router = APIRouter(tags=["review"])


@router.post("/detections/{detection_id}/review")
def review_detection(detection_id: int, body: ReviewDetection, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ReviewService(ctx_of(request)).review_detection(detection_id, accept=body.accept, actor=principal.name, note=body.note)
    except KeyError as exc:
        raise not_found("detection") from exc
    except ValueError as exc:
        raise bad_request(exc) from exc
    return {"ok": True}


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
