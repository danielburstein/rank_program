from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found, runner_of
from rankrecovery.api.schemas import ExtractRequest, JobCreate, RecomputeRequest
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.extraction import ExtractionService

router = APIRouter(tags=["jobs"])


@router.post("/jobs")
def create_job(body: JobCreate, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        job_id = runner_of(request).submit(body.kind, body.params, label=body.label, created_by=principal.name)
    except KeyError as exc:
        raise bad_request(exc) from exc
    return {"job_id": job_id}


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 50, status: str | None = None, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    return runner_of(request).list(limit=limit, status=status)


@router.get("/jobs/kinds")
def job_kinds(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> list[str]:
    return runner_of(request).kinds()


@router.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    try:
        return runner_of(request).get(job_id)
    except KeyError as exc:
        raise not_found("job") from exc


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    if not runner_of(request).cancel(job_id):
        raise not_found("job")
    return {"ok": True}


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: int, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    runner_of(request).pause(job_id, True)
    return {"ok": True}


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: int, request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    runner_of(request).pause(job_id, False)
    return {"ok": True}


@router.post("/extract")
def extract(body: ExtractRequest, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    ctx = ctx_of(request)
    pending = body.image_ids or ExtractionService(ctx).pending_images()
    job_id = runner_of(request).submit("extract_images", {"image_ids": body.image_ids, "force": body.force, "extractor": body.extractor}, label=f"extract {len(pending)} image(s)", created_by=principal.name, total=len(pending))
    return {"job_id": job_id, "pending": len(pending)}


@router.post("/recompute")
def recompute(body: RecomputeRequest, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    job_id = runner_of(request).submit("recompute_players", {"player_ids": body.player_ids, "all": body.all, "then_convert": body.then_convert}, label="recompute players", created_by=principal.name)
    return {"job_id": job_id}
