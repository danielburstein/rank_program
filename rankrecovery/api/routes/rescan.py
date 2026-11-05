from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, runner_of
from rankrecovery.api.schemas import ManualCorrection, RescanCandidates, RescanRun
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.rescan import RescanScope, RescanService, unresolved_candidates_for

router = APIRouter(tags=["rescan"])


@router.post("/rescan/candidates")
def candidates(body: RescanCandidates, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    ctx = ctx_of(request)
    scope = RescanScope(mode=body.mode, player_id=body.player_id, batch_ids=body.batch_ids, submitter_names=body.submitter_names, image_ids=body.image_ids)
    found = [asdict(c) for c in RescanService(ctx).find_candidates(body.username, scope)]
    return {"candidates": found, "unresolved_candidates": unresolved_candidates_for(ctx, body.username), "scope_images": len(RescanService(ctx).scope_image_ids(scope))}


@router.post("/rescan")
def run(body: RescanRun, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    job_id = runner_of(request).submit("rescan_images", body.model_dump(), label=f"rescan for {body.username}", created_by=principal.name)
    return {"job_id": job_id}


@router.post("/rescan/manual-correction")
def manual_correction(body: ManualCorrection, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        return RescanService(ctx_of(request)).save_manual_correction(
            body.image_id,
            username=body.username,
            prefix_text=body.prefix_text,
            rank=body.rank,
            staff=body.staff,
            region=body.region,
            username_bbox=body.username_bbox,
            prefix_bbox=body.prefix_bbox,
            trailing_icon=body.trailing_icon,
            leading_icon=body.leading_icon,
            actor=principal.name,
            note=body.note,
            supersedes_detection_id=body.supersedes_detection_id,
        )
    except (ValueError, KeyError) as exc:
        raise bad_request(exc) from exc
