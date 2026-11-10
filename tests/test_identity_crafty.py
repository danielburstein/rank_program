"""crafty.gg previous-holder lookups and the Mojang-first / most-recent-holder policy."""

from __future__ import annotations

import httpx

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.core.enums import LookupOutcome
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.identity.base import LookupResult
from rankrecovery.identity.lookup import build_lookup_service
from rankrecovery.identity.providers import CraftyHistoricalProvider

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"  # most recent previous holder
UUID_B = "853c80ef-3c37-49fd-aa49-938b674adae6"  # older previous holder
UUID_C = "289a7ee7-6ae4-4638-8f58-a7f8ffaeea24"  # current holder


def crafty_body(name: str, current: dict | None = None, history: list[tuple[str, str, str | None]] = ()) -> dict:
    return {
        "success": True,
        "data": {
            "username": name,
            "known": True,
            "current_player": current,
            "availability": {"status": "taken" if current else "available", "uuid": (current or {}).get("uuid")},
            "historical_players": [{"uuid": u, "username": n, "changed_at": t, "views_lifetime": 1} for u, n, t in history],
        },
    }


def crafty_player(uuid: str, name: str) -> dict:
    return {"id": "x", "uuid": uuid, "username": name, "updated_at": "2026-10-26T22:06:25.078+01:00"}


def crafty_404(r):
    return httpx.Response(404, json={"success": False, "message": "Username not found"})


def mojang_ok(name: str, uuid: str):
    return lambda r: httpx.Response(200, json={"id": uuid.replace("-", ""), "name": name})


def mojang_404(r):
    return httpx.Response(404, json={"errorMessage": "Couldn't find any profile"})


def make_service(mojang_handler, crafty_handler, **overrides):
    settings = dict(DEFAULT_SETTINGS)
    settings["lookup.max_retries"] = 0
    settings.update(overrides)
    calls: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "api.crafty.gg":
            return crafty_handler(request)
        return mojang_handler(request)

    svc = build_lookup_service(settings, client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(route), headers={"User-Agent": "test"}), sleep=lambda s: None)
    return svc, calls


def provider(handler) -> CraftyHistoricalProvider:
    return CraftyHistoricalProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(handler)))


def test_defaults_use_crafty_as_fallback_and_take_most_recent_holder():
    assert DEFAULT_SETTINGS["lookup.historical_provider"] == "crafty"
    assert DEFAULT_SETTINGS["lookup.history_mode"] == "fallback"
    assert DEFAULT_SETTINGS["lookup.history_pick"] == "most_recent"


def test_provider_free_name_with_one_previous_holder():
    urls: list[str] = []

    def handler(r):
        urls.append(str(r.url))
        return httpx.Response(200, json=crafty_body("OldName", None, [(UUID_A, "NewName", "2019-03-01T10:00:00.000Z")]))

    res = provider(handler).lookup("OldName")
    assert urls == ["https://api.crafty.gg/api/v2/usernames/OldName"]
    assert res.outcome == LookupOutcome.RESOLVED and res.uuid == UUID_A and res.current_name == "NewName" and res.is_historical
    assert res.candidates[0]["changed_at"].startswith("2019-03-01") and "previously used" in res.message


def test_provider_taken_name_lists_current_and_previous_holders():
    body = crafty_body("OldName", crafty_player(UUID_C, "OldName"), [(UUID_A, "Newer", "2016-01-10T21:32:10.000Z"), (UUID_B, "Older", "2015-06-08T02:18:20.000Z")])
    res = provider(lambda r: httpx.Response(200, json=body)).lookup("OldName")
    assert res.outcome == LookupOutcome.FAILED and res.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY"
    by_uuid = {c["uuid"]: c for c in res.candidates}
    assert set(by_uuid) == {UUID_A, UUID_B, UUID_C} and by_uuid[UUID_C]["current_holder"] and "current_holder" not in by_uuid[UUID_A]


def test_provider_not_found_and_invalid_payloads():
    assert provider(crafty_404).lookup("Nobody").outcome == LookupOutcome.NOT_FOUND
    empty = provider(lambda r: httpx.Response(200, json=crafty_body("Nobody"))).lookup("Nobody")
    assert empty.outcome == LookupOutcome.NOT_FOUND and empty.failure_code == "UUID_LOOKUP_NOT_FOUND"
    assert provider(lambda r: httpx.Response(200, json={"weird": 1})).lookup("Nobody").failure_code == "UUID_LOOKUP_INVALID_RESPONSE"
    assert provider(lambda r: httpx.Response(200, json={"success": True, "data": {"username": "x", "historical_players": "no"}})).lookup("x").failure_code == "UUID_LOOKUP_INVALID_RESPONSE"


def test_mojang_hit_is_final_by_default():
    svc, calls = make_service(mojang_ok("OldName", UUID_C), lambda r: httpx.Response(200, json=crafty_body("OldName", crafty_player(UUID_C, "OldName"), [(UUID_A, "Newer", "2016-01-10T21:32:10.000Z")])))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_C and not res.is_historical and len(res.attempts) == 1
    assert "api.crafty.gg" not in calls


def test_free_name_goes_to_the_most_recent_previous_holder():
    history = [(UUID_B, "Older", "2015-06-08T02:18:20.000Z"), (UUID_A, "Newer", "2019-03-01T10:00:00.000Z")]
    svc, calls = make_service(mojang_404, lambda r: httpx.Response(200, json=crafty_body("OldName", None, history)))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_A and res.current_name == "Newer" and res.is_historical and res.provider == "crafty"
    assert [c["uuid"] for c in res.candidates] == [UUID_A, UUID_B]
    assert "most recent holder" in res.message and UUID_B in res.message
    assert [a.provider for a in res.attempts] == ["mojang", "crafty"] and calls == ["api.mojang.com", "api.crafty.gg"]


def test_free_name_with_single_previous_holder_and_with_none():
    svc, _ = make_service(mojang_404, lambda r: httpx.Response(200, json=crafty_body("OldName", None, [(UUID_A, "Newer", "2019-03-01T10:00:00.000Z")])))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_A and res.is_historical and len(res.candidates) == 1
    svc, _ = make_service(mojang_404, crafty_404)
    res = svc.resolve("Ghost")
    assert res.outcome == LookupOutcome.NOT_FOUND and res.failure_code == "UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED" and "crafty" in res.message


def test_pick_ambiguous_leaves_several_holders_to_a_person():
    history = [(UUID_B, "Older", "2015-06-08T02:18:20.000Z"), (UUID_A, "Newer", "2019-03-01T10:00:00.000Z")]
    svc, _ = make_service(mojang_404, lambda r: httpx.Response(200, json=crafty_body("OldName", None, history)), **{"lookup.history_pick": "ambiguous"})
    res = svc.resolve("OldName")
    assert res.outcome == LookupOutcome.FAILED and res.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY" and len(res.candidates) == 2


def test_undated_holders_cannot_be_ordered_so_stay_ambiguous():
    svc, _ = make_service(mojang_404, lambda r: httpx.Response(200, json=crafty_body("OldName", None, [(UUID_B, "Older", None), (UUID_A, "Newer", None)])))
    res = svc.resolve("OldName")
    assert res.outcome == LookupOutcome.FAILED and res.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY"


def test_always_mode_cross_checks_and_prefers_the_current_holder():
    body = crafty_body("OldName", crafty_player(UUID_C, "OldName"), [(UUID_A, "Newer", "2016-01-10T21:32:10.000Z")])
    svc, calls = make_service(mojang_ok("OldName", UUID_C), lambda r: httpx.Response(200, json=body), **{"lookup.history_mode": "always"})
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_C and not res.is_historical and len(res.candidates) == 2 and len(res.attempts) == 2
    svc, _ = make_service(mojang_ok("OldName", UUID_C), lambda r: httpx.Response(200, json=body), **{"lookup.history_mode": "always", "lookup.history_pick": "ambiguous"})
    assert svc.resolve("OldName").failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY"


def test_persisting_a_most_recent_holder_sets_the_uuid_and_flags_the_reuse(tmp_path):
    from sqlalchemy import select

    from rankrecovery.db.models import Flag, Player
    from rankrecovery.services.context import AppContext
    from rankrecovery.services.identity import IdentityService
    from rankrecovery.services.players import get_or_create_player

    db = Database(sqlite_url(tmp_path / "crafty.db"))
    db.run_migrations()
    ctx = AppContext(db)
    with db.write_session() as s:
        player, _ = get_or_create_player(s, "OldName")
        pid = player.id

    class Fake:
        def resolve_many(self, names, **kwargs):
            cands = [{"uuid": UUID_A, "current_name": "Newer", "matched_name": "OldName", "changed_at": "2019-03-01T10:00:00.000Z", "source": "crafty"}, {"uuid": UUID_B, "current_name": "Older", "matched_name": "OldName", "changed_at": "2015-06-08T02:18:20.000Z", "source": "crafty"}]
            return [LookupResult(queried_name=n, provider="crafty", outcome=LookupOutcome.RESOLVED, uuid=UUID_A, current_name="Newer", message="2 accounts have used 'OldName'; took the most recent holder", is_historical=True, candidates=cands) for n in names]

    summary = IdentityService(ctx).lookup_players([pid], service=Fake())
    assert summary.resolved == 1
    with db.read_session() as s:
        p = s.get(Player, pid)
        assert p.canonical_uuid == UUID_A and p.identity_status == "verified" and p.identity_source == "crafty" and p.current_name == "Newer"
        assert {a.username for a in p.aliases} == {"OldName", "Newer"}
        flags = {f.code: f for f in s.execute(select(Flag).where(Flag.entity_type == "player", Flag.entity_id == pid, Flag.is_active.is_(True))).scalars()}
        assert "IDENTITY_NAME_REUSED" in flags and UUID_B in flags["IDENTITY_NAME_REUSED"].details
    db.dispose()
