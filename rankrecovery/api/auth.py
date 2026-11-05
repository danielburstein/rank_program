"""API-key authentication with roles.

Keys are random, shown once at creation and stored as SHA-256 hashes.
Roles: admin > staff > bot > readonly.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from rankrecovery.core.enums import ApiRole
from rankrecovery.db.models import ApiKey
from rankrecovery.services.context import AppContext

ROLE_RANK = {ApiRole.READONLY: 0, ApiRole.BOT: 1, ApiRole.STAFF: 2, ApiRole.ADMIN: 3}
BOOTSTRAP_ENV = "RANKRECOVERY_ADMIN_KEY"


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    role: ApiRole
    key_id: int | None = None

    def at_least(self, role: ApiRole) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> str:
    return "rr_" + secrets.token_urlsafe(32)


def create_api_key(ctx: AppContext, *, name: str, role: ApiRole, created_by: str = "") -> tuple[str, int]:
    raw = generate_key()
    with ctx.db.write_session() as s:
        row = ApiKey(name=name, key_hash=hash_key(raw), key_prefix=raw[:10], role=role.value, created_by=created_by)
        s.add(row)
        s.flush()
        return raw, row.id


def revoke_api_key(ctx: AppContext, key_id: int) -> bool:
    with ctx.db.write_session() as s:
        row = s.get(ApiKey, key_id)
        if row is None:
            return False
        row.revoked_at = datetime.now(UTC)
        return True


def list_api_keys(ctx: AppContext) -> list[dict]:
    with ctx.db.read_session() as s:
        return [
            {"id": k.id, "name": k.name, "prefix": k.key_prefix, "role": k.role, "created_at": k.created_at.isoformat() if k.created_at else None, "revoked": k.revoked_at is not None, "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
            for k in s.execute(select(ApiKey).order_by(ApiKey.id)).scalars()
        ]


def authenticate(ctx: AppContext, raw: str | None) -> Principal | None:
    if not raw:
        return None
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if bootstrap and secrets.compare_digest(raw, bootstrap):
        return Principal("env-admin", ApiRole.ADMIN)
    digest = hash_key(raw)
    with ctx.db.write_session() as s:
        row = s.execute(select(ApiKey).where(ApiKey.key_hash == digest)).scalars().first()
        if row is None or row.revoked_at is not None:
            return None
        row.last_used_at = datetime.now(UTC)
        return Principal(row.name, ApiRole(row.role), row.id)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def current_principal(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key"), authorization: str | None = Header(default=None)) -> Principal:
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    principal = authenticate(get_ctx(request), raw)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
    return principal


def require(role: ApiRole):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.at_least(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role.value}")
        return principal

    return dependency
