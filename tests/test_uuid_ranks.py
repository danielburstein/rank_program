"""uuid_ranks: one row per resolved UUID carrying the highest rank across its name records."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from rankrecovery.core.enums import IdentityStatus
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.db.models import Player, UuidRank
from rankrecovery.services.context import AppContext
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.images import ImageService
from rankrecovery.services.reconcile import ReconcileService
from rankrecovery.services.uuidranks import UuidRankService, refresh_uuid_ranks
from tests.helpers import StubExtractor, png_bytes, tab

UUID_X = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_Y = "853c80ef-3c37-49fd-aa49-938b674adae6"
UUID_Z = "289a7ee7-6ae4-4638-8f58-a7f8ffaeea24"


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "ranks.db"))
    db.run_migrations()
    rows = {
        "a.png": [tab("◇ Hero ◇ OldName")],
        "b.png": [tab("◇ Galactic ◇ NewName")],
        "c.png": [tab("◇ Immortal ◇ Unresolved")],
        "d.png": [tab("◇ Titan ◇ Ambiguous")],
        "e.png": [tab("◇ Legend ◇ Lonely")],
    }
    stub = StubExtractor(rows)
    context = AppContext(db, extractor_factory=lambda _c, _o: stub)
    images = ImageService(context)
    for seed, name in enumerate(rows, start=1):
        ExtractionService(context).extract_image(images.import_bytes(png_bytes(seed), filename=name, submitter_name="staff").image_id)
    recon = ReconcileService(context)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    yield context
    db.dispose()


def set_identity(ctx, name: str, uuid: str | None, status: str, source: str = "mojang", current: str | None = None) -> int:
    with ctx.db.write_session() as s:
        player = s.execute(select(Player).where(Player.name_key == name.lower())).scalars().first()
        player.canonical_uuid = uuid
        player.identity_status = status
        player.identity_source = source
        player.current_name = current
        return player.id


def test_rebuild_collapses_records_per_uuid_to_the_highest_rank(ctx):
    old_id = set_identity(ctx, "OldName", UUID_X, IdentityStatus.VERIFIED.value, "crafty", current="NewName")
    new_id = set_identity(ctx, "NewName", UUID_X, IdentityStatus.VERIFIED.value, "mojang", current="NewName")
    set_identity(ctx, "Ambiguous", UUID_Y, IdentityStatus.AMBIGUOUS.value)  # has a uuid but is not resolved: left out
    set_identity(ctx, "Lonely", UUID_Z, IdentityStatus.MANUAL.value, "manual")
    # "Unresolved" keeps no uuid at all

    summary = UuidRankService(ctx).rebuild(actor="test")
    assert summary == {**summary, "uuids": 2, "records_merged": 1, "resolved_without_rank": 0, "unresolved_left_as_is": 2}
    rows = {r["uuid"]: r for r in UuidRankService(ctx).rows()}
    assert set(rows) == {UUID_X, UUID_Z}
    x = rows[UUID_X]
    assert x["highest_rank"] == "GALACTIC" and x["new_rank"] == "assassin" and x["ranks_seen"] == ["HERO", "GALACTIC"]
    assert x["records"] == 2 and sorted(x["player_ids"]) == sorted([old_id, new_id]) and x["usernames"] == ["NewName", "OldName"]
    assert x["current_name"] == "NewName" and x["identity_sources"] == ["crafty", "mojang"]
    z = rows[UUID_Z]
    assert z["highest_rank"] == "LEGEND" and z["new_rank"] == "thug" and z["records"] == 1 and z["usernames"] == ["Lonely"]
    assert [r["uuid"] for r in UuidRankService(ctx).rows()] == [UUID_X, UUID_Z]  # highest rank first
    with ctx.db.read_session() as s:
        assert s.execute(select(Player.canonical_uuid).where(Player.name_key == "unresolved")).scalar() is None  # untouched


def test_rebuild_replaces_rows_and_skips_resolved_players_without_a_rank(ctx):
    svc = UuidRankService(ctx)
    set_identity(ctx, "Lonely", UUID_Z, IdentityStatus.VERIFIED.value)
    assert svc.rebuild()["uuids"] == 1
    with ctx.db.write_session() as s:
        s.execute(select(Player)).scalars()  # noqa: F841 - keep session shape
        from rankrecovery.db.models import PlayerSummary

        ps = s.execute(select(PlayerSummary).join(Player).where(Player.name_key == "lonely")).scalars().first()
        ps.effective_rank = None
    summary = svc.rebuild()
    assert summary["uuids"] == 0 and summary["resolved_without_rank"] == 1
    with ctx.db.read_session() as s:
        assert s.execute(select(UuidRank)).scalars().all() == []


def test_csv_and_search(ctx):
    set_identity(ctx, "OldName", UUID_X, IdentityStatus.VERIFIED.value, current="NewName")
    set_identity(ctx, "Lonely", UUID_Z, IdentityStatus.VERIFIED.value)
    svc = UuidRankService(ctx)
    svc.rebuild()
    text = svc.csv_text()
    header, *lines = text.strip().split("\n")
    assert header == "uuid,current_name,highest_rank,new_rank,records,ranks_seen,usernames,identity_sources,is_bedrock,computed_at"
    assert len(lines) == 2 and lines[0].startswith(f"{UUID_Z},,LEGEND,thug,")  # highest rank first
    assert lines[1].startswith(f"{UUID_X},NewName,HERO,thug,1,HERO,NewName|OldName,mojang,False,")
    assert [r["uuid"] for r in svc.rows(search="lonely")] == [UUID_Z]
    assert [r["uuid"] for r in svc.rows(rank="hero")] == [UUID_X]
    assert svc.rows(search="nobody") == [] and svc.count() == 2


def test_refresh_never_raises(ctx, monkeypatch):
    monkeypatch.setattr(UuidRankService, "rebuild", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert refresh_uuid_ranks(ctx) is None
