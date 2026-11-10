"""Bulk submissions: the newuploads folder and Google Drive links."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, runner_of
from rankrecovery.api.schemas import DriveImportBody, IngestFolderBody
from rankrecovery.core.enums import ApiRole
from rankrecovery.services.drive import parse_drive_link
from rankrecovery.services.ingest import FolderIngestService

router = APIRouter(tags=["ingest"])


@router.get("/ingest/folder")
def folder_status(request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    svc = FolderIngestService(ctx_of(request))
    folder = svc.folder_path()
    waiting = svc.waiting()
    return {"folder": str(folder), "count": len(waiting), "waiting": [p.relative_to(folder).as_posix() for p in waiting[:200]]}


@router.post("/ingest/folder")
def ingest_folder(body: IngestFolderBody, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    return FolderIngestService(ctx_of(request)).ingest(submitter=body.submitter, actor=principal.name, extract=body.extract).as_dict()


@router.post("/ingest/drive")
def drive_import(body: DriveImportBody, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        kind, drive_id = parse_drive_link(body.url)
    except ValueError as exc:
        raise bad_request(exc) from exc
    job_id = runner_of(request).submit("download_drive", {"url": body.url, "submitter": body.submitter, "ingest": body.ingest, "actor": principal.name}, label=f"download Drive {kind} {drive_id}", created_by=principal.name)
    return {"job_id": job_id, "kind": kind, "drive_id": drive_id}
