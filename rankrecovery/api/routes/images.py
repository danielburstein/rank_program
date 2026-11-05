from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, not_found, runner_of
from rankrecovery.api.schemas import AiReview, DateReview, SubmitterUpdate
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.context import AppContext
from rankrecovery.services.images import ImageService
from rankrecovery.services.rescan import RescanService

log = logging.getLogger(__name__)
router = APIRouter(tags=["images"])


@router.post("/batches")
def create_batch(request: Request, label: str = Form(""), submitter: str | None = Form(None), source: str = Form("client"), notes: str = Form(""), principal: Principal = Depends(require(ApiRole.BOT))) -> dict:
    batch_id = ImageService(ctx_of(request)).create_batch(source=source, label=label, submitter_name=submitter, created_by=principal.name, notes=notes)
    return {"batch_id": batch_id}


@router.post("/images")
async def upload_images(
    request: Request,
    files: list[UploadFile] = File(...),
    submitter: str | None = Form(None),
    batch_id: int | None = Form(None),
    original_paths: list[str] | None = Form(None),
    file_modified: list[str] | None = Form(None),
    discord_user_id: str | None = Form(None),
    extract: bool = Form(True),
    principal: Principal = Depends(require(ApiRole.BOT)),
) -> dict:
    ctx = ctx_of(request)
    svc = ImageService(ctx)
    outcomes = []
    for index, upload in enumerate(files):
        content = await upload.read()
        if not content:
            outcomes.append({"filename": upload.filename, "error": "empty file"})
            continue
        path = original_paths[index] if original_paths and index < len(original_paths) else ""
        modified = None
        if file_modified and index < len(file_modified) and file_modified[index]:
            try:
                modified = datetime.fromisoformat(file_modified[index])
            except ValueError:
                modified = None
        try:
            outcome = svc.import_bytes(content, filename=upload.filename or "upload", submitter_name=submitter, batch_id=batch_id, original_path=path, file_modified=modified, created_by=principal.name, discord_user_id=discord_user_id)
            outcomes.append({"filename": upload.filename, "image_id": outcome.image_id, "created": outcome.created, "sha256": outcome.sha256, "duplicate_of": outcome.duplicate_of, "near_duplicates": outcome.near_duplicates, "ai_status": outcome.ai_status, "date_status": outcome.date_status, "warnings": outcome.warnings})
        except ValueError as exc:
            outcomes.append({"filename": upload.filename, "error": str(exc)})
    job_id = None
    new_ids = [o["image_id"] for o in outcomes if o.get("created")]
    if extract and new_ids:
        job_id = runner_of(request).submit("extract_images", {"image_ids": new_ids}, label=f"extract {len(new_ids)} uploaded image(s)", created_by=principal.name, total=len(new_ids))
    return {"outcomes": outcomes, "job_id": job_id}


@router.get("/images/{image_id}")
def image_context(image_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    try:
        return RescanService(ctx_of(request)).image_context(image_id)
    except KeyError as exc:
        raise not_found("image") from exc


@router.get("/images/{image_id}/content")
def image_content(image_id: int, request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> Response:
    content = ImageService(ctx_of(request)).get_bytes(image_id)
    if content is None:
        raise not_found("image bytes")
    return Response(content=content, media_type="application/octet-stream")


def render_overlay(ctx: AppContext, image_id: int, *, highlight: str | None = None) -> Response:
    from rankrecovery.imaging.decode import decode_image
    from rankrecovery.imaging.overlay import boxes_for_detections, overlay_png

    content = ImageService(ctx).get_bytes(image_id)
    if content is None:
        raise not_found("image bytes")
    context = RescanService(ctx).image_context(image_id)
    img = decode_image(content)
    boxes = boxes_for_detections(context["detections"], highlight_username=highlight)
    return Response(content=overlay_png(img, boxes), media_type="image/png")


@router.get("/images/{image_id}/overlay")
def image_overlay(image_id: int, request: Request, highlight: str | None = None, _: Principal = Depends(require(ApiRole.READONLY))) -> Response:
    try:
        return render_overlay(ctx_of(request), image_id, highlight=highlight)
    except KeyError as exc:
        raise not_found("image") from exc


@router.post("/images/{image_id}/submitter")
def set_submitter(image_id: int, body: SubmitterUpdate, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        ImageService(ctx_of(request)).set_submitter(image_id, body.submitter, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("image") from exc
    return {"ok": True}


@router.post("/images/{image_id}/ai-review")
def ai_review(image_id: int, body: AiReview, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        ImageService(ctx_of(request)).review_ai_status(image_id, flagged=body.flagged, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("image") from exc
    return {"ok": True}


@router.post("/images/{image_id}/date-review")
def date_review(image_id: int, body: DateReview, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    capture = None
    if body.capture_at:
        try:
            capture = datetime.fromisoformat(body.capture_at)
        except ValueError as exc:
            raise bad_request(exc) from exc
    try:
        ImageService(ctx_of(request)).review_date(image_id, capture_at=capture, actor=principal.name, reason=body.reason)
    except KeyError as exc:
        raise not_found("image") from exc
    return {"ok": True}


@router.get("/submitters")
def submitters(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    from sqlalchemy import func, select

    from rankrecovery.db.models import SourceImage, Submitter

    with ctx_of(request).db.read_session() as s:
        rows = s.execute(select(Submitter, func.count(SourceImage.id)).outerjoin(SourceImage, SourceImage.submitter_id == Submitter.id).group_by(Submitter.id).order_by(Submitter.name)).all()
        return [{"id": sub.id, "name": sub.name, "discord_user_id": sub.discord_user_id, "image_count": n} for sub, n in rows]


@router.get("/batches")
def batches(request: Request, limit: int = 100, _: Principal = Depends(require(ApiRole.READONLY))) -> list[dict]:
    from sqlalchemy import select

    from rankrecovery.db.models import ImportBatch

    with ctx_of(request).db.read_session() as s:
        return [{"id": b.id, "source": b.source, "label": b.label, "submitter_id": b.submitter_id, "created_by": b.created_by, "created_at": b.created_at.isoformat() if b.created_at else None, "image_count": b.image_count} for b in s.execute(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit)).scalars()]


@router.get("/images")
def list_images(request: Request, limit: int = 200, offset: int = 0, submitter: str | None = None, batch_id: int | None = None, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    from sqlalchemy import func, select

    from rankrecovery.db.models import SourceImage, Submitter
    from rankrecovery.services.search import image_dict

    with ctx_of(request).db.read_session() as s:
        stmt = select(SourceImage)
        if submitter:
            stmt = stmt.where(SourceImage.submitter_id.in_(select(Submitter.id).where(Submitter.name_key == submitter.lower())))
        if batch_id:
            stmt = stmt.where(SourceImage.import_batch_id == batch_id)
        total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
        rows = s.execute(stmt.order_by(SourceImage.id.desc()).limit(limit).offset(offset)).scalars().all()
        return {"total": total, "rows": [image_dict(i) for i in rows]}

