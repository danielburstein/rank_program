from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from rankrecovery.api.auth import Principal, require
from rankrecovery.api.routes._common import ctx_of, not_found
from rankrecovery.api.schemas import DiscordLinkBody
from rankrecovery.core.enums import ApiRole
from rankrecovery.db.models import DiscordLink
from rankrecovery.services.audit import audit
from rankrecovery.services.players import find_player_by_username

router = APIRouter(tags=["discord"])


def _link_dict(link: DiscordLink) -> dict:
    return {"discord_user_id": link.discord_user_id, "discord_username": link.discord_username, "username": link.username, "player_id": link.player_id, "verified": link.verified, "updated_at": link.updated_at.isoformat() if link.updated_at else None}


@router.post("/discord/link")
def link(body: DiscordLinkBody, request: Request, principal: Principal = Depends(require(ApiRole.BOT))) -> dict:
    ctx = ctx_of(request)
    with ctx.db.write_session() as s:
        row = s.execute(select(DiscordLink).where(DiscordLink.discord_user_id == body.discord_user_id)).scalars().first()
        before = _link_dict(row) if row else None
        if row is None:
            row = DiscordLink(discord_user_id=body.discord_user_id)
            s.add(row)
        row.discord_username = body.discord_username
        row.username = body.username
        row.verified = body.verified if principal.at_least(ApiRole.STAFF) else False
        player = find_player_by_username(s, body.username)
        row.player_id = player.id if player else None
        s.flush()
        audit(s, actor=principal.name, action="discord.link", entity_type="discord_link", entity_id=row.id, before=before, after=_link_dict(row))
        return _link_dict(row)


@router.get("/discord/link/{discord_user_id}")
def get_link(discord_user_id: str, request: Request, _: Principal = Depends(require(ApiRole.BOT))) -> dict:
    with ctx_of(request).db.read_session() as s:
        row = s.execute(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id)).scalars().first()
        if row is None:
            raise not_found("link")
        return _link_dict(row)


@router.delete("/discord/link/{discord_user_id}")
def unlink(discord_user_id: str, request: Request, principal: Principal = Depends(require(ApiRole.BOT))) -> dict:
    with ctx_of(request).db.write_session() as s:
        row = s.execute(select(DiscordLink).where(DiscordLink.discord_user_id == discord_user_id)).scalars().first()
        if row is None:
            raise not_found("link")
        audit(s, actor=principal.name, action="discord.unlink", entity_type="discord_link", entity_id=row.id, before=_link_dict(row))
        s.delete(row)
    return {"ok": True}
