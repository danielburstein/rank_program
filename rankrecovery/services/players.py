"""Player identity helpers shared by several services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import Player, PlayerAlias, PlayerSummary


def resolve_survivor(session: Session, player: Player) -> Player:
    """Follow merge links to the surviving identity."""
    seen = {player.id}
    current = player
    while current.merged_into_id is not None:
        nxt = session.get(Player, current.merged_into_id)
        if nxt is None or nxt.id in seen:
            break
        seen.add(nxt.id)
        current = nxt
    return current


def find_player_by_username(session: Session, username: str, *, follow_merges: bool = True) -> Player | None:
    key = normalise_username_key(username)
    if not key:
        return None
    alias = session.execute(select(PlayerAlias).where(PlayerAlias.username_key == key).order_by(PlayerAlias.id)).scalars().first()
    if alias is None:
        player = session.execute(select(Player).where(Player.name_key == key).order_by(Player.id)).scalars().first()
        if player is None:
            return None
    else:
        player = session.get(Player, alias.player_id)
        if player is None:
            return None
    return resolve_survivor(session, player) if follow_merges else player


def find_player_by_uuid(session: Session, uuid: str) -> Player | None:
    player = session.execute(select(Player).where(Player.canonical_uuid == uuid, Player.merged_into_id.is_(None)).order_by(Player.id)).scalars().first()
    return player


def get_or_create_player(session: Session, username: str, *, is_bedrock: bool = False, source: str = "extraction") -> tuple[Player, bool]:
    existing = find_player_by_username(session, username)
    if existing is not None:
        return existing, False
    key = normalise_username_key(username)
    player = Player(display_name=username, name_key=key, is_bedrock=is_bedrock)
    session.add(player)
    session.flush()
    session.add(PlayerAlias(player_id=player.id, username=username, username_key=key, source=source))
    session.flush()
    return player, True


def add_alias(session: Session, player: Player, username: str, *, source: str) -> bool:
    key = normalise_username_key(username)
    if not key:
        return False
    exists = session.execute(select(PlayerAlias).where(PlayerAlias.player_id == player.id, PlayerAlias.username_key == key)).scalars().first()
    if exists:
        return False
    session.add(PlayerAlias(player_id=player.id, username=username, username_key=key, source=source))
    return True


def merge_group_ids(session: Session, survivor_id: int) -> list[int]:
    """The survivor plus every player merged into it (transitively)."""
    ids = [survivor_id]
    frontier = [survivor_id]
    while frontier:
        rows = session.execute(select(Player.id).where(Player.merged_into_id.in_(frontier))).scalars().all()
        rows = [r for r in rows if r not in ids]
        ids.extend(rows)
        frontier = rows
    return ids


def mark_summary_stale(session: Session, player_id: int, *, reason: str = "") -> None:
    summary = session.execute(select(PlayerSummary).where(PlayerSummary.player_id == player_id)).scalars().first()
    if summary is not None:
        summary.stale = True
