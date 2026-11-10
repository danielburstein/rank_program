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
    """Queue the ingest as a job; the summary is the job's result."""
    svc = FolderIngestService(ctx_of(request))
    folder = svc.folder_path()
    waiting = svc.waiting()
    if not waiting:
        return {"job_id": None, "found": 0, "folder": str(folder)}
    job_id = runner_of(request).submit("ingest_folder", {"submitter": body.submitter, "extract": body.extract, "actor": principal.name}, label=f"ingest {len(waiting)} file(s) from {folder.name}", created_by=principal.name, total=len(waiting))
    return {"job_id": job_id, "found": len(waiting), "folder": str(folder)}


@router.post("/ingest/drive")
def drive_import(body: DriveImportBody, request: Request, principal: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    try:
        kind, drive_id = parse_drive_link(body.url)
    except ValueError as exc:
        raise bad_request(exc) from exc
    job_id = runner_of(request).submit("download_drive", {"url": body.url, "submitter": body.submitter, "ingest": body.ingest, "actor": principal.name}, label=f"download Drive {kind} {drive_id}", created_by=principal.name)
    return {"job_id": job_id, "kind": kind, "drive_id": drive_id}
