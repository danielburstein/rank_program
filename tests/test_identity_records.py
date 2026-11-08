from __future__ import annotations

import json

import httpx

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.core.enums import LookupOutcome
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.identity.providers import LabyNetHistoricalProvider, MojangProfileProvider
from rankrecovery.services.context import AppContext
from rankrecovery.services.identity import IdentityService
from rankrecovery.services.players import get_or_create_player
from rankrecovery.services.playerview import PlayerViewService

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "853c80ef-3c37-49fd-aa49-938b674adae6"


def ctx_with_players(tmp_path, names):
    db = Database(sqlite_url(tmp_path / "rec.db"))
    db.run_migrations()
    ctx = AppContext(db)
    ids = {}
    with db.write_session() as s:
        for n in names:
            p, _ = get_or_create_player(s, n)
            ids[n] = p.id
    return ctx, ids


def test_import_usercache_json_links_uuids_and_flags_conflicts(tmp_path):
    ctx, ids = ctx_with_players(tmp_path, ["OldName", "Someone", "Clashy"])
    svc = IdentityService(ctx)
    svc.set_manual_uuid(ids["Clashy"], UUID_B, actor="t", reason="known")
    usercache = json.dumps([
        {"name": "OldName", "uuid": UUID_A, "expiresOn": "2024-03-14 18:22:01 +0000"},
        {"name": "Clashy", "uuid": UUID_A, "expiresOn": "2024-03-14 18:22:01 +0000"},
        {"name": "NeverSeen", "uuid": "11111111-2222-3333-4444-555555555555"},
        {"name": "bad name!", "uuid": UUID_B},
    ]).encode()
    summary = svc.import_identity_records(usercache, filename="usercache.json", actor="daniel")
    assert summary["matched"] == 1 and summary["conflicts"] == 1 and summary["unmatched"] == 1 and summary["invalid"] == 1
    view = PlayerViewService(ctx)
    old = view.detail(ids["OldName"])
    assert old["player"]["uuid"] == UUID_A and old["player"]["identity_status"] == "verified" and old["player"]["identity_source"] == "server_records"
    clashy = view.detail(ids["Clashy"])
    assert clashy["player"]["uuid"] == UUID_B  # not overwritten
    assert "IDENTITY_CONFLICT" in {f["code"] for f in clashy["flags"] if f["is_active"]}
    assert any(r["status"] == "ambiguous" and r["uuid"] == UUID_A for r in clashy["identity_resolutions"])
    # CSV form, and --create for names not yet seen
    csv = b"username,uuid\nNeverSeen,11111111-2222-3333-4444-555555555555\n"
    summary = svc.import_identity_records(csv, filename="players.csv", actor="daniel", create_missing=True)
    assert summary["created"] == 1 and summary["matched"] == 1
    assert view.lookup_by_name("NeverSeen")["player"]["uuid"] == "11111111-2222-3333-4444-555555555555"
    ctx.db.dispose()


def test_refresh_current_names_from_mojang(tmp_path):
    ctx, ids = ctx_with_players(tmp_path, ["OldName", "Bedrocky"])
    svc = IdentityService(ctx)
    svc.set_manual_uuid(ids["OldName"], UUID_A, actor="t", reason="known")
    svc.set_manual_uuid(ids["Bedrocky"], "00000000-0000-0000-0009-01f64f65c7c3", actor="t", reason="floodgate")
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(str(r.url))
        return httpx.Response(200, json={"id": UUID_A.replace("-", ""), "name": "ShinyNewName"})

    provider = MojangProfileProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(handler)))
    counts = svc.refresh_current_names(provider=provider)
    assert counts == {"checked": 1, "updated": 1, "unchanged": 0, "not_found": 0, "failed": 0}
    assert calls == [f"https://sessionserver.mojang.com/session/minecraft/profile/{UUID_A.replace('-', '')}"]
    d = PlayerViewService(ctx).detail(ids["OldName"])
    assert d["player"]["current_name"] == "ShinyNewName" and "ShinyNewName" in d["player"]["aliases"]
    assert d["lookups"][0]["provider"] == "mojang_profile"
    # second run without force skips players that already have a current name
    assert svc.refresh_current_names(provider=provider)["checked"] == 0
    ctx.db.dispose()


def test_profile_provider_rejects_mismatched_id():
    p = MojangProfileProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": UUID_B.replace("-", ""), "name": "X"}))))
    res = p.lookup(UUID_A)
    assert res.outcome == LookupOutcome.FAILED and res.failure_code == "UUID_LOOKUP_INVALID_RESPONSE"
    p = MojangProfileProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204))))
    assert p.lookup(UUID_A).outcome == LookupOutcome.NOT_FOUND


def test_labynet_extra_headers_are_sent():
    seen: dict[str, str] = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in r.headers.items()})
        return httpx.Response(200, json={"users": []})

    settings = dict(DEFAULT_SETTINGS)
    settings["lookup.labynet_extra_headers"] = {"X-Api-Token": "granted-by-labynet"}
    p = LabyNetHistoricalProvider(settings, client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(handler)))
    p.lookup("Someone")
    assert seen["x-api-token"] == "granted-by-labynet"
