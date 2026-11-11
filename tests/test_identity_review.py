"""Manual UUID review: the queue of unresolved names, setting a UUID by hand, skipping."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from rankrecovery.core.enums import IdentityStatus, LookupOutcome
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.db.models import Flag, Player, UuidRank
from rankrecovery.identity.base import LookupResult
from rankrecovery.services.context import AppContext
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.identity import REVIEW_SKIP_FLAG, IdentityService
from rankrecovery.services.images import ImageService
from rankrecovery.services.reconcile import ReconcileService
from tests.helpers import StubExtractor, png_bytes, tab

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "853c80ef-3c37-49fd-aa49-938b674adae6"


class FakeProfile:
    def __init__(self, names: dict[str, str]):
        self.names = names
        self.calls: list[str] = []

    def lookup(self, uuid: str) -> LookupResult:
        self.calls.append(uuid)
        name = self.names.get(uuid)
        if name is None:
            return LookupResult(queried_name=uuid, provider="mojang_profile", outcome=LookupOutcome.NOT_FOUND, message="no profile")
        return LookupResult(queried_name=uuid, provider="mojang_profile", outcome=LookupOutcome.RESOLVED, uuid=uuid, current_name=name)


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "review.db"))
    db.run_migrations()
    rows = {"a.png": [tab("◇ Hero ◇ LowGuy")], "b.png": [tab("◇ Galactic ◇ BigShot")], "c.png": [tab("◇ Legend ◇ Middle")]}
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


def pid_of(ctx, name: str) -> int:
    with ctx.db.read_session() as s:
        return s.execute(select(Player.id).where(Player.name_key == name.lower())).scalar_one()


def test_queue_lists_unresolved_names_highest_rank_first(ctx):
    svc = IdentityService(ctx)
    data = svc.review_queue(limit=2)
    assert data["total"] == 3 and [it["name"] for it in data["items"]] == ["BigShot", "Middle"]
    top = data["items"][0]
    assert top["effective_rank"] == "GALACTIC" and top["images"] == 1 and top["namemc_url"] == "https://namemc.com/search?q=BigShot"
    assert top["identity_status"] == IdentityStatus.NOT_ATTEMPTED.value and top["candidates"] == [] and not top["skipped"]
    assert [it["name"] for it in svc.review_queue(limit=5, offset=2)["items"]] == ["LowGuy"]
    assert [it["name"] for it in svc.review_queue(search="mid")["items"]] == ["Middle"]


def test_set_uuid_by_hand_confirms_name_recomputes_and_leaves_the_queue(ctx):
    svc = IdentityService(ctx)
    profile = FakeProfile({UUID_A: "BigShotNow"})
    result = svc.resolve_manually(pid_of(ctx, "BigShot"), UUID_A.replace("-", ""), actor="staff", note="found on namemc", provider=profile)
    assert result["uuid"] == UUID_A and result["verified"] and result["current_name"] == "BigShotNow" and result["message"] == ""
    assert result["effective_rank"] == "GALACTIC" and result["new_rank"] == "assassin" and profile.calls == [UUID_A]
    with ctx.db.read_session() as s:
        p = s.get(Player, result["player_id"])
        assert p.canonical_uuid == UUID_A and p.identity_status == "manual" and p.identity_source == "manual" and p.current_name == "BigShotNow"
        assert {a.username for a in p.aliases} == {"BigShot", "BigShotNow"}
        assert s.execute(select(UuidRank.highest_rank).where(UuidRank.uuid == UUID_A)).scalar() == "GALACTIC"  # the hand-off table followed
    assert [it["name"] for it in svc.review_queue()["items"]] == ["Middle", "LowGuy"]


def test_set_uuid_keeps_going_when_mojang_cannot_confirm(ctx):
    svc = IdentityService(ctx)
    result = svc.resolve_manually(pid_of(ctx, "Middle"), UUID_B, actor="staff", provider=FakeProfile({}))
    assert result["uuid"] == UUID_B and not result["verified"] and result["current_name"] is None and "no profile" in result["message"]
    with ctx.db.read_session() as s:
        assert s.execute(select(Player.canonical_uuid).where(Player.name_key == "middle")).scalar() == UUID_B


def test_invalid_uuid_and_unknown_player(ctx):
    svc = IdentityService(ctx)
    with pytest.raises(ValueError):
        svc.resolve_manually(pid_of(ctx, "Middle"), "not-a-uuid", actor="staff", provider=FakeProfile({}))
    with pytest.raises(KeyError):
        svc.resolve_manually(99999, UUID_A, actor="staff", provider=FakeProfile({}))
    with pytest.raises(KeyError):
        svc.skip_identity_review(99999, actor="staff")


def test_skip_hides_a_name_until_unskipped_or_resolved(ctx):
    svc = IdentityService(ctx)
    low = pid_of(ctx, "LowGuy")
    svc.skip_identity_review(low, actor="staff", reason="garbled name")
    assert [it["name"] for it in svc.review_queue()["items"]] == ["BigShot", "Middle"]
    shown = svc.review_queue(include_skipped=True)["items"]
    assert [it["name"] for it in shown] == ["BigShot", "Middle", "LowGuy"] and shown[-1]["skipped"]
    with ctx.db.read_session() as s:
        flag = s.execute(select(Flag).where(Flag.entity_id == low, Flag.code == REVIEW_SKIP_FLAG, Flag.is_active.is_(True))).scalars().one()
        assert flag.details == "garbled name"
    assert svc.unskip_identity_review(low, actor="staff") == 1 and svc.unskip_identity_review(low, actor="staff") == 0
    assert svc.review_queue()["total"] == 3
    svc.skip_identity_review(low, actor="staff")
    svc.resolve_manually(low, UUID_A, actor="staff", provider=FakeProfile({UUID_A: "LowGuy"}))
    with ctx.db.read_session() as s:
        assert not s.execute(select(Flag).where(Flag.entity_id == low, Flag.code == REVIEW_SKIP_FLAG, Flag.is_active.is_(True))).scalars().all()  # setting the UUID clears the skip
    assert svc.review_queue()["total"] == 2
