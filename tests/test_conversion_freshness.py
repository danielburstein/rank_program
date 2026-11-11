"""The new rank shown for a player must always follow their current effective old rank.

Seen live: a player whose conversion was computed while they were INFINITE
(-> assassin) later gained SCUFFED evidence; the stored conversion was never
refreshed, so /rank showed "Effective old rank SCUFFED · New rank assassin".
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.db.models import Conversion, Player
from rankrecovery.jobs.handlers import register_default_handlers
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext
from rankrecovery.services.conversion import ConversionService
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.images import ImageService
from rankrecovery.services.playerview import PlayerViewService
from rankrecovery.services.reconcile import ReconcileService
from tests.helpers import StubExtractor, png_bytes, tab

MAIN_CONVERSION = {
    "HERO": "thug",
    "LEGEND": "thug",
    "TITAN": "thug",
    "GOD": "bandit",
    "IMMORTAL": "bandit",
    "IMMORTAL+": "rogue",
    "GALACTIC": "assassin",
    "INFINITE": "assassin",
    "SCUFFED": "Warlord",
}


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "conv.db"))
    db.run_migrations()
    rows = {
        "first.png": [tab("◇ Infinite ◇ Strawberry"), tab("◇ God ◇ Shush")],
        "later.png": [tab("◇ Scuffed ◇ Strawberry")],
    }
    stub = StubExtractor(rows)
    context = AppContext(db, extractor_factory=lambda _c, _o: stub)
    yield context
    db.dispose()


def extract(ctx, name: str, seed: int) -> None:
    image_id = ImageService(ctx).import_bytes(png_bytes(seed), filename=name, submitter_name="staff").image_id
    ExtractionService(ctx).extract_image(image_id)
    recon = ReconcileService(ctx)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)


def pid_of(ctx, name: str) -> int:
    with ctx.db.read_session() as s:
        return s.execute(select(Player.id).where(Player.name_key == name.lower())).scalar_one()


def test_default_mapping_is_the_main_conversion():
    assert DEFAULT_SETTINGS["ranks.mapping"] == MAIN_CONVERSION


def test_every_rank_maps_as_specified(ctx):
    policy, mapping = ctx.policy(), ctx.mapping()
    assert {rank: mapping.new_rank_for(rank, policy) for rank in MAIN_CONVERSION} == MAIN_CONVERSION
    assert mapping.new_rank_for("DEFAULT", policy) is None and mapping.new_rank_for(None, policy) is None


def test_new_rank_follows_the_effective_rank_even_with_an_outdated_conversion_row(ctx):
    extract(ctx, "first.png", 1)
    ConversionService(ctx).recompute()
    view = PlayerViewService(ctx)
    sid = pid_of(ctx, "Strawberry")
    assert view.detail(sid)["conversion"]["new_rank"] == "assassin"  # INFINITE at this point

    extract(ctx, "later.png", 2)  # SCUFFED evidence arrives; the stored conversion row is now out of date
    with ctx.db.read_session() as s:
        conv = s.execute(select(Conversion).where(Conversion.player_id == sid)).scalars().one()
        assert conv.stale and conv.new_rank == "assassin"  # the row itself still says assassin
    d = view.detail(sid)
    assert d["summary"]["effective_rank"] == "SCUFFED"
    assert d["summary"]["new_rank"] == "Warlord" and d["conversion"]["new_rank"] == "Warlord" and d["conversion"]["old_rank"] == "SCUFFED"
    assert d["conversion"]["stale"] and not d["conversion"]["included"] and "being refreshed" in d["conversion"]["reasons"][-1]
    row = next(r for r in view.table()["rows"] if r["display_name"] == "Strawberry")
    assert row["new_rank"] == "Warlord" and row["export_included"] is False


def test_player_without_a_conversion_row_still_shows_the_mapped_rank(ctx):
    extract(ctx, "first.png", 1)  # no conversion has ever been computed
    view = PlayerViewService(ctx)
    d = view.detail(pid_of(ctx, "Shush"))
    assert d["conversion"] is None and d["summary"]["effective_rank"] == "GOD" and d["summary"]["new_rank"] == "bandit"
    assert next(r for r in view.table()["rows"] if r["display_name"] == "Shush")["new_rank"] == "bandit"


def test_rank_embed_shows_the_mapped_rank_in_both_cases(ctx):
    from rankrecovery.bot.formatting import rank_embed

    extract(ctx, "first.png", 1)
    ConversionService(ctx).recompute([pid_of(ctx, "Strawberry")])
    extract(ctx, "later.png", 2)
    view = PlayerViewService(ctx)

    def fields(name: str) -> dict[str, str]:
        return {f.name: f.value for f in rank_embed(view.detail(pid_of(ctx, name)), staff_view=True).fields}

    strawberry = fields("Strawberry")
    assert strawberry["Effective old rank"] == "SCUFFED" and strawberry["New rank"] == "Warlord"
    shush = fields("Shush")
    assert shush["Effective old rank"] == "GOD" and shush["New rank"] == "bandit"


def test_refresh_stale_only_touches_missing_or_outdated_conversions(ctx):
    extract(ctx, "first.png", 1)
    svc = ConversionService(ctx)
    assert sorted(svc.stale_player_ids()) == sorted([pid_of(ctx, "Strawberry"), pid_of(ctx, "Shush")])  # never computed
    assert svc.refresh_stale()["total"] == 2 and svc.stale_player_ids() == []
    assert svc.refresh_stale() == {"included": 0, "excluded": 0, "total": 0}  # nothing to do, and it does not fall back to "everyone"
    extract(ctx, "later.png", 2)
    assert svc.stale_player_ids() == [pid_of(ctx, "Strawberry")]
    assert svc.refresh_stale()["total"] == 1
    with ctx.db.read_session() as s:
        conv = s.execute(select(Conversion).where(Conversion.player_id == pid_of(ctx, "Strawberry"))).scalars().one()
        assert not conv.stale and conv.old_rank == "SCUFFED" and conv.new_rank == "Warlord"


def test_extraction_job_leaves_conversions_fresh(ctx):
    images = ImageService(ctx)
    images.import_bytes(png_bytes(1), filename="first.png", submitter_name="staff")
    runner = JobRunner(ctx, workers=1)
    register_default_handlers(runner)
    runner.start()
    try:
        assert runner.wait(runner.submit("extract_images", {}), timeout=30)["status"] == "completed"
        assert ConversionService(ctx).stale_player_ids() == []
        images.import_bytes(png_bytes(2), filename="later.png", submitter_name="staff")
        assert runner.wait(runner.submit("extract_images", {}), timeout=30)["status"] == "completed"
        assert ConversionService(ctx).stale_player_ids() == []
        with ctx.db.read_session() as s:
            conv = s.execute(select(Conversion).where(Conversion.player_id == pid_of(ctx, "Strawberry"))).scalars().one()
            assert (conv.old_rank, conv.new_rank, conv.stale) == ("SCUFFED", "Warlord", False)
        job = runner.wait(runner.submit("convert_players", {"only_stale": True}), timeout=30)
        assert job["status"] == "completed" and job["result"]["total"] == 0
    finally:
        runner.stop()


def test_server_start_refreshes_conversions_left_outdated(ctx):
    from fastapi.testclient import TestClient

    from rankrecovery.api.app import create_app

    extract(ctx, "first.png", 1)
    ConversionService(ctx).recompute()
    extract(ctx, "later.png", 2)  # leaves Strawberry's conversion out of date, as older versions did
    assert ConversionService(ctx).stale_player_ids() == [pid_of(ctx, "Strawberry")]
    app = create_app(ctx, job_workers=1)
    with TestClient(app) as client:
        jobs = [j for j in client.app.state.runner.list(limit=10) if j["kind"] == "convert_players"]
        assert len(jobs) == 1 and jobs[0]["created_by"] == "startup"
        assert client.app.state.runner.wait(jobs[0]["id"], timeout=30)["status"] == "completed"
        assert ConversionService(ctx).stale_player_ids() == []
