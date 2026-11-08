from __future__ import annotations

import json

import httpx

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.core.enums import LookupOutcome
from rankrecovery.identity.lookup import build_lookup_service
from rankrecovery.identity.providers import LabyNetHistoricalProvider

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "853c80ef-3c37-49fd-aa49-938b674adae6"


def laby_user(uuid: str, current: str, history: list[tuple[str, str | None]]) -> dict:
    return {"userName": current, "uuid": uuid.replace("-", ""), "history": [{"userName": n, "updatedAt": t} for n, t in history]}


def make_service(mojang_handler, laby_handler, *, historical="labynet"):
    settings = dict(DEFAULT_SETTINGS)
    settings["lookup.historical_provider"] = historical
    settings["lookup.max_retries"] = 0

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "laby.net":
            return laby_handler(request)
        return mojang_handler(request)

    return build_lookup_service(settings, client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(route), headers={"User-Agent": "test"}), sleep=lambda s: None)


def mojang_ok(name: str, uuid: str):
    return lambda r: httpx.Response(200, json={"id": uuid.replace("-", ""), "name": name})


def mojang_404(r):
    return httpx.Response(404, json={"errorMessage": "Couldn't find any profile"})


def laby_with(*users):
    return lambda r: httpx.Response(200, json={"users": list(users)})


def test_provider_parses_candidates():
    calls: list[str] = []

    def handler(r):
        calls.append(str(r.url))
        return httpx.Response(200, json={"users": [laby_user(UUID_A, "NewName", [("OldName", "2023-01-01T00:00:00+00:00"), ("NewName", "2024-01-01T00:00:00+00:00")]), laby_user(UUID_B, "Other", [("Other", None)])]})

    p = LabyNetHistoricalProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(handler)))
    res = p.lookup("oldname")
    assert calls[0] == "https://laby.net/api/search/get-previous-accounts/oldname"
    assert res.outcome == LookupOutcome.RESOLVED and res.uuid == UUID_A and res.current_name == "NewName" and res.is_historical
    assert res.candidates[0]["changed_at"] == "2023-01-01T00:00:00+00:00"


def test_provider_not_found_and_invalid():
    p = LabyNetHistoricalProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"users": []}))))
    res = p.lookup("Nobody")
    assert res.outcome == LookupOutcome.NOT_FOUND and res.failure_code == "UUID_LOOKUP_NOT_FOUND"
    p = LabyNetHistoricalProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"weird": 1}))))
    assert p.lookup("Nobody").failure_code == "UUID_LOOKUP_INVALID_RESPONSE"


def test_agreement_between_current_owner_and_history():
    svc = make_service(mojang_ok("OldName", UUID_A), laby_with(laby_user(UUID_A, "OldName", [("OldName", None)])))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_A and res.is_historical and "agree" in res.message
    assert len(res.attempts) == 2


def test_name_reuse_becomes_ambiguous():
    svc = make_service(mojang_ok("OldName", UUID_B), laby_with(laby_user(UUID_A, "Renamed", [("OldName", "2022-05-05T00:00:00+00:00"), ("Renamed", "2023-05-05T00:00:00+00:00")])))
    res = svc.resolve("OldName")
    assert res.outcome == LookupOutcome.FAILED and res.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY"
    uuids = {c["uuid"] for c in res.candidates}
    assert uuids == {UUID_A, UUID_B}
    assert any(c.get("current_holder") for c in res.candidates)


def test_history_only_resolves_when_current_name_is_gone():
    svc = make_service(mojang_404, laby_with(laby_user(UUID_A, "Renamed", [("OldName", "2022-05-05T00:00:00+00:00"), ("Renamed", None)])))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_A and res.is_historical and res.current_name == "Renamed"


def test_two_previous_holders_are_ambiguous():
    svc = make_service(mojang_404, laby_with(laby_user(UUID_A, "A", [("OldName", "2021-01-01T00:00:00+00:00")]), laby_user(UUID_B, "B", [("OldName", "2022-01-01T00:00:00+00:00")])))
    res = svc.resolve("OldName")
    assert res.failure_code == "UUID_LOOKUP_AMBIGUOUS_IDENTITY" and len(res.candidates) == 2


def test_nothing_anywhere_is_unresolved():
    svc = make_service(mojang_404, laby_with())
    res = svc.resolve("OldName")
    assert res.outcome == LookupOutcome.NOT_FOUND and res.failure_code == "UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED"


def test_current_name_without_history_record_stays_current_match():
    svc = make_service(mojang_ok("OldName", UUID_A), laby_with())
    res = svc.resolve("OldName")
    assert res.resolved and not res.is_historical and "no record" in res.message


def test_history_outage_keeps_current_answer():
    svc = make_service(mojang_ok("OldName", UUID_A), lambda r: httpx.Response(503))
    res = svc.resolve("OldName")
    assert res.resolved and res.uuid == UUID_A and not res.is_historical and "failed" in res.message


def test_without_historical_provider_behaviour_unchanged():
    svc = make_service(mojang_404, laby_with(), historical="none")
    res = svc.resolve("OldName")
    assert res.outcome == LookupOutcome.NOT_FOUND and res.failure_code == "UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED"
    assert len(res.attempts) == 1


def test_ambiguous_result_is_persisted_for_review(tmp_path):
    from rankrecovery.db.engine import Database, sqlite_url
    from rankrecovery.services.context import AppContext
    from rankrecovery.services.identity import IdentityService
    from rankrecovery.services.players import get_or_create_player
    from rankrecovery.services.playerview import PlayerViewService

    db = Database(sqlite_url(tmp_path / "amb.db"))
    db.run_migrations()
    ctx = AppContext(db)
    with db.write_session() as s:
        player, _ = get_or_create_player(s, "OldName")
        pid = player.id
    svc = make_service(mojang_ok("OldName", UUID_B), laby_with(laby_user(UUID_A, "Renamed", [("OldName", "2022-05-05T00:00:00+00:00")])))
    IdentityService(ctx).lookup_players([pid], service=svc)
    detail = PlayerViewService(ctx).detail(pid)
    assert detail["player"]["identity_status"] == "ambiguous" and detail["player"]["uuid"] is None
    cands = [r for r in detail["identity_resolutions"] if r["status"] == "ambiguous"]
    assert {r["uuid"] for r in cands} == {UUID_A, UUID_B}
    assert "UUID_LOOKUP_AMBIGUOUS_IDENTITY" in {f["code"] for f in detail["flags"] if f["is_active"]}
    # the reviewer settles it with a manual UUID
    IdentityService(ctx).set_manual_uuid(pid, UUID_A, actor="daniel", reason="screenshots are from 2022, before the rename")
    assert PlayerViewService(ctx).detail(pid)["player"]["identity_status"] == "manual"
    db.dispose()


_ = json
