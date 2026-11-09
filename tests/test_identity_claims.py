from __future__ import annotations

import pytest

from rankrecovery.core.enums import LookupOutcome
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.identity.base import LookupResult
from rankrecovery.services.context import AppContext
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.identity import IdentityService
from rankrecovery.services.images import ImageService
from rankrecovery.services.playerview import PlayerViewService
from rankrecovery.services.reconcile import ReconcileService
from tests.helpers import StubExtractor, png_bytes, tab

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "853c80ef-3c37-49fd-aa49-938b674adae6"


class FakeLookup:
    def __init__(self, table):
        self.table = table

    def resolve(self, name, **kwargs):
        hit = self.table.get(name.lower())
        if hit is None:
            return LookupResult(name, "mojang", LookupOutcome.NOT_FOUND, failure_code="UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED")
        return LookupResult(name, "mojang", LookupOutcome.RESOLVED, uuid=hit[0], current_name=hit[1])


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "claims.db"))
    db.run_migrations()
    stub = StubExtractor({"old.png": [tab("◇ Galactic ◇ OldName")], "new.png": [tab("◇ Hero ◇ NewName")]})
    context = AppContext(db, extractor_factory=lambda _c, _o: stub)
    images = ImageService(context)
    for seed, name in ((1, "old.png"), (2, "new.png")):
        ExtractionService(context).extract_image(images.import_bytes(png_bytes(seed), filename=name, submitter_name="staff").image_id)
    recon = ReconcileService(context)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    yield context
    db.dispose()


def test_claim_then_approve_moves_rank_to_current_name(ctx):
    ident = IdentityService(ctx)
    view = PlayerViewService(ctx)
    lookup = FakeLookup({"newname": (UUID_A, "NewName")})
    claim = ident.create_claim(old_username="OldName", current_username="NewName", actor="bot", discord_user_id="1", discord_username="dan#1", service=lookup)
    assert claim["status"] == "pending" and claim["uuid"] == UUID_A
    assert "IDENTITY_CLAIM_PENDING" in {f["code"] for f in view.lookup_by_name("OldName")["flags"] if f["is_active"]}
    assert [c["claim_id"] for c in ident.list_claims()] == [claim["claim_id"]]
    result = ident.decide_claim(claim["claim_id"], approve=True, actor="staff", note="checked in game")
    assert result["status"] == "approved"
    d = view.lookup_by_name("NewName")
    assert d["player"]["uuid"] == UUID_A and d["player"]["identity_status"] == "manual"
    assert "OldName" in d["player"]["aliases"] and "NewName" in d["player"]["aliases"]
    assert d["summary"]["effective_rank"] == "GALACTIC" and "HERO" in d["summary"]["observed_ranks"]  # both records merged
    assert ident.list_claims() == [] and ident.list_claims(status="approved")[0]["decided_by"] == "staff"
    with pytest.raises(ValueError):
        ident.decide_claim(claim["claim_id"], approve=True, actor="staff")


def test_claim_rejected_and_unknown_names(ctx):
    ident = IdentityService(ctx)
    lookup = FakeLookup({"newname": (UUID_A, "NewName")})
    with pytest.raises(KeyError):
        ident.create_claim(old_username="NeverSeen", current_username="NewName", actor="bot", service=lookup)
    with pytest.raises(ValueError):
        ident.create_claim(old_username="OldName", current_username="GhostName", actor="bot", service=lookup)
    claim = ident.create_claim(old_username="OldName", current_username="NewName", actor="bot", service=lookup)
    ident.decide_claim(claim["claim_id"], approve=False, actor="staff", note="not them")
    d = PlayerViewService(ctx).lookup_by_name("OldName")
    assert d["player"]["uuid"] is None and not any(f["is_active"] and f["code"] == "IDENTITY_CLAIM_PENDING" for f in d["flags"])


def test_direct_link_refuses_conflicting_verified_uuid_unless_forced(ctx):
    ident = IdentityService(ctx)
    ident.set_manual_uuid(PlayerViewService(ctx).lookup_by_name("OldName")["player"]["player_id"], UUID_B, actor="t", reason="csv says so")
    lookup = FakeLookup({"newname": (UUID_A, "NewName")})
    with pytest.raises(ValueError):
        ident.link_by_names("OldName", "NewName", actor="staff", service=lookup)
    result = ident.link_by_names("OldName", "NewName", actor="staff", service=lookup, force=True)
    assert result["uuid"] == UUID_A
    assert PlayerViewService(ctx).lookup_by_name("OldName")["player"]["uuid"] == UUID_A
