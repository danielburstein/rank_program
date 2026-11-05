from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from rankrecovery.api.auth import Principal, create_api_key, list_api_keys, require, revoke_api_key
from rankrecovery.api.routes._common import ctx_of, runner_of
from rankrecovery.api.schemas import ApiKeyCreate, RestoreBody
from rankrecovery.core.enums import ApiRole
from rankrecovery.db.backup import backup_sqlite, restore_sqlite

router = APIRouter(tags=["admin"])


@router.post("/admin/keys")
def create_key(body: ApiKeyCreate, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    try:
        role = ApiRole(body.role)
    except ValueError as exc:
        raise HTTPException(400, f"unknown role {body.role}") from exc
    raw, key_id = create_api_key(ctx_of(request), name=body.name, role=role, created_by=principal.name)
    return {"id": key_id, "key": raw, "role": role.value, "note": "store this key now; it cannot be shown again"}


@router.get("/admin/keys")
def keys(request: Request, _: Principal = Depends(require(ApiRole.ADMIN))) -> list[dict]:
    return list_api_keys(ctx_of(request))


@router.delete("/admin/keys/{key_id}")
def revoke(key_id: int, request: Request, _: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    if not revoke_api_key(ctx_of(request), key_id):
        raise HTTPException(404, "key not found")
    return {"ok": True}


@router.post("/admin/backup")
def backup(request: Request, label: str = "", _: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    target_dir = Path(ctx.settings.get("database.backup_dir", "backups"))
    try:
        path = backup_sqlite(ctx.db, target_dir, label=label)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": str(path)}


@router.get("/admin/backups")
def backups(request: Request, _: Principal = Depends(require(ApiRole.ADMIN))) -> list[dict]:
    target_dir = Path(ctx_of(request).settings.get("database.backup_dir", "backups"))
    if not target_dir.exists():
        return []
    return [{"path": str(p), "size": p.stat().st_size, "modified": p.stat().st_mtime} for p in sorted(target_dir.glob("*.db"), reverse=True)]


@router.post("/admin/restore")
def restore(body: RestoreBody, request: Request, _: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    if not body.confirm:
        raise HTTPException(400, "confirm must be true; restoring replaces the live database")
    runner = runner_of(request)
    if any(j["status"] in ("running", "queued") for j in runner.list(limit=50)):
        raise HTTPException(409, "jobs are running; wait for them to finish before restoring")
    try:
        path = restore_sqlite(ctx_of(request).db, body.backup_file)
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"restored": str(path), "note": "restart the server to reload connections"}


@router.get("/admin/logs")
def logs(request: Request, limit: int = 200, level: str | None = None, _: Principal = Depends(require(ApiRole.STAFF))) -> list[dict]:
    return request.app.state.logbuffer.tail(limit=limit, level=level)
