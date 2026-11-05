from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import bad_request, ctx_of, runner_of
from rankrecovery.api.schemas import SettingsReset, SettingsUpdate
from rankrecovery.config.defaults import DEFAULT_SETTINGS, SECRET_SETTINGS
from rankrecovery.core.enums import ApiRole
from rankrecovery.core.flags import FLAG_CATALOGUE
from rankrecovery.core.ranks import PolicyError, RankPolicy
from rankrecovery.db.settings_store import POLICY_KEYS
from rankrecovery.services.reconcile import ReconcileService

router = APIRouter(tags=["settings"])

RECOMPUTE_KEYS = {k for keys in POLICY_KEYS.values() for k in keys} | {"conversion.allowed_flags", "conversion.allow_resolved_flags", "conversion.input_mode"}


@router.get("/settings")
def get_settings(request: Request, _: Principal = Depends(require(ApiRole.STAFF))) -> dict:
    ctx = ctx_of(request)
    values = ctx.settings.get_all()
    return {"values": values, "defaults": {k: v for k, v in DEFAULT_SETTINGS.items()}, "secrets": sorted(SECRET_SETTINGS), "secrets_configured": {k: ctx.settings.is_configured(k) for k in SECRET_SETTINGS}, "vision_configured": ctx.vision_available(), "flag_codes": list(FLAG_CATALOGUE)}


@router.put("/settings")
def update_settings(body: SettingsUpdate, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    ctx = ctx_of(request)
    values = dict(body.values)
    # validate the rank policy before persisting anything
    if "ranks.player_ranks" in values or "ranks.staff_prefixes" in values:
        try:
            RankPolicy.from_config(values.get("ranks.player_ranks", ctx.settings.get("ranks.player_ranks")), values.get("ranks.staff_prefixes", ctx.settings.get("ranks.staff_prefixes")), scuffed_rank=values.get("ranks.scuffed_rank", ctx.settings.get("ranks.scuffed_rank")))
        except PolicyError as exc:
            raise bad_request(exc) from exc
    for key, value in values.items():
        if key in SECRET_SETTINGS and value in ("***", None):
            values.pop(key, None)
    changed = ctx.settings.set_many(values, changed_by=principal.name, reason=body.reason)
    job_id = None
    if any(k in RECOMPUTE_KEYS for k in changed):
        ReconcileService(ctx).mark_all_stale(f"settings changed: {', '.join(k for k in changed if k in RECOMPUTE_KEYS)}")
        job_id = runner_of(request).submit("recompute_players", {"all": True}, label="recompute after settings change", created_by=principal.name)
    return {"changed": changed, "job_id": job_id}


@router.post("/settings/reset")
def reset_settings(body: SettingsReset, request: Request, principal: Principal = Depends(require(ApiRole.ADMIN))) -> dict:
    if not body.confirm:
        raise HTTPException(400, "confirm must be true to restore defaults")
    ctx = ctx_of(request)
    changed: list[str] = []
    for key in body.keys:
        if ctx.settings.reset(key, changed_by=principal.name):
            changed.append(key)
    if body.prefix:
        changed += ctx.settings.reset_group(body.prefix, changed_by=principal.name)
    job_id = None
    if any(k in RECOMPUTE_KEYS for k in changed):
        ReconcileService(ctx).mark_all_stale("settings restored to defaults")
        job_id = runner_of(request).submit("recompute_players", {"all": True}, label="recompute after restore defaults", created_by=principal.name)
    return {"changed": changed, "job_id": job_id}


@router.get("/settings/history")
def settings_history(request: Request, key: str | None = None, limit: int = 200, _: Principal = Depends(require(ApiRole.STAFF))) -> list[dict]:
    return ctx_of(request).settings.history(key, limit=limit)


@router.get("/settings/policy")
def policy_view(request: Request, _: Principal = Depends(require(ApiRole.READONLY))) -> dict:
    ctx = ctx_of(request)
    policy = ctx.policy()
    ranks, staff = policy.to_config()
    return {"ranks": ranks, "staff": staff, "mapping": ctx.mapping().mapping, "fingerprint": policy.fingerprint(), "mapping_fingerprint": ctx.mapping().fingerprint(), "verification": ctx.verification_settings_snapshot(), "versions": {kind: (ctx.settings.current_policy_version(kind).version if ctx.settings.current_policy_version(kind) else None) for kind in POLICY_KEYS}}


@router.get("/settings/policy-versions")
def policy_versions(request: Request, kind: str | None = None, limit: int = 50, _: Principal = Depends(require(ApiRole.STAFF))) -> list[dict]:
    from sqlalchemy import select

    from rankrecovery.db.models import PolicyVersion

    with ctx_of(request).db.read_session() as s:
        stmt = select(PolicyVersion).order_by(PolicyVersion.id.desc()).limit(limit)
        if kind:
            stmt = stmt.where(PolicyVersion.kind == kind)
        return [{"id": v.id, "kind": v.kind, "version": v.version, "fingerprint": v.fingerprint, "payload": v.payload, "created_at": v.created_at.isoformat() if v.created_at else None, "created_by": v.created_by, "reason": v.reason} for v in s.execute(stmt).scalars()]
