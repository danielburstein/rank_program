"""Mojang bulk lookups, the shared 429 cool-down and the burst cap."""

from __future__ import annotations

import json
import threading

import httpx

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.core.enums import LookupOutcome
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.identity import failures as F
from rankrecovery.identity.lookup import build_lookup_service
from rankrecovery.identity.ratelimit import RateLimiter

HEX = {"Alpha": "069a79f444e94726a5befca90e38aaf5", "Beta": "853c80ef3c3749fdaa49938b674adae6", "Gamma": "289a7ee76ae446388f58a7f8ffaeea24"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, handler, overrides: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.clock = FakeClock()
        settings = dict(DEFAULT_SETTINGS)
        settings.update({"lookup.max_retries": 2, "lookup.historical_provider": "none", "lookup.backoff_base_seconds": 1.0})
        settings.update(overrides or {})

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.now += seconds

        self.service = build_lookup_service(settings, client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(wrapped), headers={"User-Agent": "test"}), clock=self.clock, sleep=sleep)
        self.service._rng = lambda: 1.0

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


def bulk_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        names = json.loads(request.content)
        assert len(names) <= 10
        return httpx.Response(200, json=[{"id": HEX[n], "name": n} for n in names if n in HEX])
    name = str(request.url).rsplit("/", 1)[-1]
    return httpx.Response(200, json={"id": HEX[name], "name": name}) if name in HEX else httpx.Response(404)


def test_resolve_many_asks_mojang_ten_names_at_a_time():
    h = Harness(bulk_handler)
    names = ["Alpha", "Beta", "Gamma"] + [f"Ghost{i}" for i in range(20)]
    results = h.service.resolve_many(names, workers=3)
    assert [r.queried_name for r in results] == names
    assert len(h.posts) == 3 and not h.gets  # 23 names -> 3 bulk requests, no single lookups
    assert str(h.posts[0].url) == "https://api.mojang.com/profiles/minecraft" and json.loads(h.posts[0].content) == names[:10]
    alpha = results[0]
    assert alpha.resolved and alpha.uuid == "069a79f4-44e9-4726-a5be-fca90e38aaf5" and alpha.current_name == "Alpha" and alpha.provider == "mojang"
    assert len(alpha.attempts) == 1 and alpha.attempts[0].http_status == 200 and "bulk lookup" in alpha.attempts[0].error_message and alpha.attempts[0].queried_name == "Alpha"
    ghost = results[3]
    assert ghost.outcome == LookupOutcome.NOT_FOUND and ghost.failure_code == F.HISTORICAL_NAME_UNRESOLVED and ghost.attempts[0].failure_code == F.NOT_FOUND


def test_bulk_answers_feed_the_crafty_fallback_only_for_missing_names():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crafty.gg":
            return httpx.Response(200, json={"success": True, "data": {"username": "Ghost", "known": True, "current_player": None, "historical_players": [{"uuid": HEX["Gamma"], "username": "Renamed", "changed_at": "2019-01-01T00:00:00.000Z"}]}})
        return bulk_handler(request)

    h = Harness(handler, {"lookup.historical_provider": "crafty"})
    results = h.service.resolve_many(["Alpha", "Ghost"], workers=2)
    assert results[0].resolved and not results[0].is_historical
    assert results[1].resolved and results[1].is_historical and results[1].current_name == "Renamed"
    assert [r.url.host for r in h.requests] == ["api.mojang.com", "api.crafty.gg"]


def test_bulk_429_pauses_everyone_then_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
        return bulk_handler(request)

    h = Harness(handler)
    results = h.service.resolve_many(["Alpha", "Beta"], workers=2)
    assert all(r.resolved for r in results) and len(h.posts) == 2 and not h.gets
    assert h.sleeps and sum(h.sleeps) >= 7.0  # Retry-After honoured before the second bulk call


def test_bulk_client_error_falls_back_to_single_lookups():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={"error": "Invalid profile name"})
        return bulk_handler(request)

    h = Harness(handler)
    results = h.service.resolve_many(["Alpha", "Beta", "Nobody"], workers=2)
    assert len(h.posts) == 1 and len(h.gets) == 3 and not h.sleeps  # 400 is not retried
    assert results[0].resolved and results[1].resolved and results[2].outcome == LookupOutcome.NOT_FOUND


def test_bulk_can_be_switched_off_and_skips_bedrock_and_cached_names():
    h = Harness(bulk_handler, {"lookup.bulk_lookups": False})
    h.service.resolve_many(["Alpha", "Beta"], workers=2)
    assert not h.posts and len(h.gets) == 2

    h = Harness(bulk_handler, {"lookup.bedrock_provider": "manual"})
    h.service.resolve_many(["Alpha", ".gamertag", "alpha"], workers=2)
    assert len(h.posts) == 1 and json.loads(h.posts[0].content) == ["Alpha"]  # Bedrock names and duplicates never enter a bulk request
    h.service.resolve_many(["Alpha"], workers=1)
    assert len(h.posts) == 1  # already cached
    again = h.service.resolve("Alpha")
    assert again.cached and again.resolved and len(h.requests) == 1


def test_single_lookup_429_penalises_the_provider_for_every_worker():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else bulk_handler(request)

    h = Harness(handler, {"lookup.bulk_lookups": False, "lookup.rate_limit_cooldown_seconds": 10})
    r = h.service.resolve("Alpha")
    assert r.resolved and len(h.gets) == 2
    assert sum(h.sleeps) >= 10.0  # the cool-down, not just one worker's back-off
    bucket = h.service.rate_limiter._bucket("mojang")
    assert bucket.blocked_until <= h.clock.now


def test_repeated_429_pauses_longer_each_time_and_keeps_trying():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] <= 4 else bulk_handler(request)

    h = Harness(handler, {"lookup.bulk_lookups": False, "lookup.max_retries": 1, "lookup.rate_limit_cooldown_seconds": 10})
    r = h.service.resolve("Alpha")
    assert r.resolved and len(h.gets) == 5  # four 429s did not exhaust it: max_retries only bounds technical failures
    assert [s for s in h.sleeps if s >= 10] == [10.0, 20.0, 40.0, 80.0]


def test_rate_limit_budget_eventually_gives_up_with_a_clear_message():
    h = Harness(lambda r: httpx.Response(429), {"lookup.bulk_lookups": False, "lookup.rate_limit_max_retries": 2, "lookup.rate_limit_cooldown_seconds": 1, "lookup.rate_limit_max_cooldown_seconds": 2})
    r = h.service.resolve("Alpha")
    assert r.outcome == LookupOutcome.FAILED and r.failure_code == F.RETRIES_EXHAUSTED and "429" in r.message and "resumes" in r.message
    assert len(h.gets) == 3 and [s for s in h.sleeps if s >= 1] == [1.0, 2.0]  # capped at the max cool-down


def test_bulk_repeated_429_escalates_then_falls_back_to_single_lookups():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429) if request.method == "POST" else bulk_handler(request)

    h = Harness(handler, {"lookup.rate_limit_max_retries": 1, "lookup.rate_limit_cooldown_seconds": 5})
    results = h.service.resolve_many(["Alpha"], workers=1)
    assert results[0].resolved and len(h.posts) == 2 and len(h.gets) == 1
    assert [s for s in h.sleeps if s >= 5][:1] == [5.0]


def test_provider_outage_trips_a_breaker_instead_of_retrying_every_name():
    h = Harness(lambda r: httpx.Response(503), {"lookup.bulk_lookups": False, "lookup.max_retries": 0, "lookup.outage_threshold": 3, "lookup.outage_cooldown_seconds": 60})
    results = h.service.resolve_many([f"Name{i}" for i in range(8)], workers=1)
    assert len(h.gets) == 3  # the fourth name onwards never reached the network
    assert all(r.outcome == LookupOutcome.FAILED and r.failure_code == F.API_UNAVAILABLE for r in results)
    assert "skipped without a request" in results[-1].message and "retry-failed" in results[-1].message
    h.clock.now += 61  # cool-down over: the breaker lets a request through again
    h.service.resolve("Another")
    assert len(h.gets) == 4


def test_breaker_resets_on_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] <= 2 else bulk_handler(request)

    h = Harness(handler, {"lookup.bulk_lookups": False, "lookup.max_retries": 0, "lookup.outage_threshold": 3})
    assert h.service.resolve("Alpha").outcome == LookupOutcome.FAILED
    assert h.service.resolve("Beta").outcome == LookupOutcome.FAILED
    assert h.service.resolve("Alpha").resolved  # third call succeeds and clears the count
    assert h.service._outages == {}


def test_impossible_java_names_are_answered_without_a_request():
    h = Harness(bulk_handler)
    r = h.service.resolve("FredBoat♪♪")
    assert r.outcome == LookupOutcome.NOT_FOUND and r.failure_code == F.HISTORICAL_NAME_UNRESOLVED and "cannot be a Java username" in r.message and not h.requests
    results = h.service.resolve_many(["Alpha", "FredBoat♪♪", "way_too_long_for_minecraft"], workers=2)
    assert results[0].resolved and results[1].outcome == LookupOutcome.NOT_FOUND and results[2].outcome == LookupOutcome.NOT_FOUND
    assert len(h.posts) == 1 and json.loads(h.posts[0].content) == ["Alpha"]  # junk never enters a bulk request


def test_rate_limiter_penalise_blocks_until_the_cooldown_passes():
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock.now += s

    limiter = RateLimiter(600, clock=clock, sleep=sleep)
    limiter.acquire("mojang")
    limiter.penalise("mojang", 5)
    limiter.acquire("mojang")
    assert 5.0 <= sum(sleeps) <= 6.5
    assert limiter.acquire("mojang") == 0.0  # refilled after the hold


def test_rate_limiter_burst_caps_the_initial_run():
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock.now += s

    limiter = RateLimiter(120, burst=10, clock=clock, sleep=sleep)
    for _ in range(10):
        limiter.acquire("mojang")
    assert not sleeps
    limiter.acquire("mojang")
    assert sleeps and abs(sum(sleeps) - 0.5) < 0.01  # 120/min = one token every half second

    unlimited = RateLimiter(120, clock=clock, sleep=sleep)
    for _ in range(100):
        unlimited.acquire("x")
    assert len(sleeps) == 1  # no burst cap: the whole minute's budget is available at once


def test_cancelled_bulk_prefetch_makes_no_requests():
    cancel = threading.Event()
    cancel.set()
    h = Harness(bulk_handler)
    results = h.service.resolve_many(["Alpha", "Beta"], workers=2, cancel=cancel)
    assert not h.requests and all(r.outcome == LookupOutcome.SKIPPED for r in results)


def test_database_cache_round_trips_on_sqlite_and_serves_repeat_lookups(tmp_path):
    """SQLite returns naive datetimes; comparing them with an aware now() used to raise inside the cache (seen live)."""
    from rankrecovery.services.context import AppContext
    from rankrecovery.services.identity import DbLookupCache, IdentityService
    from rankrecovery.services.players import get_or_create_player

    db = Database(sqlite_url(tmp_path / "cache.db"))
    db.run_migrations()
    ctx = AppContext(db)
    cache = DbLookupCache(ctx)
    from rankrecovery.identity.base import LookupResult

    cache[("mojang", "alpha")] = (10_000_000.0, LookupResult(queried_name="Alpha", provider="mojang", outcome=LookupOutcome.RESOLVED, uuid="069a79f4-44e9-4726-a5be-fca90e38aaf5", current_name="Alpha"))
    hit = cache.get(("mojang", "alpha"))
    assert hit is not None and hit[1].uuid == "069a79f4-44e9-4726-a5be-fca90e38aaf5" and hit[1].cached
    assert cache.get(("mojang", "nobody")) is None

    with db.write_session() as s:
        pid = get_or_create_player(s, "Alpha")[0].id
        pid2 = get_or_create_player(s, "Ghost")[0].id
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.host}")
        if request.url.host == "api.crafty.gg":
            return httpx.Response(404, json={"success": False, "message": "Username not found"})
        return bulk_handler(request)

    factory = lambda proxy: httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})  # noqa: E731
    svc = IdentityService(ctx)
    first = svc.lookup_players([pid, pid2], service=svc.build_lookup_service(client_factory=factory))
    assert first.resolved == 1 and first.not_found == 1 and first.failed == 0
    assert requests == ["POST api.mojang.com", "GET api.crafty.gg"]
    second = svc.lookup_players([pid2], service=svc.build_lookup_service(client_factory=factory))
    assert second.not_found == 1 and second.failed == 0 and len(requests) == 2  # both answers came from the database cache
    db.dispose()


def test_startup_retires_labynet_without_a_token(tmp_path):
    from rankrecovery.db.settings_store import SettingsStore
    from rankrecovery.services.context import AppContext

    db = Database(sqlite_url(tmp_path / "settings.db"))
    db.run_migrations()
    store = SettingsStore(db)
    store.set("lookup.historical_provider", "labynet", changed_by="console")
    AppContext(db, store)
    assert store.get("lookup.historical_provider") == "crafty"
    assert any(h["new"] == "crafty" and "428" in (h.get("reason") or "") for h in store.history("lookup.historical_provider"))

    store.set("lookup.labynet_extra_headers", {"X-Api-Token": "granted"}, changed_by="console")
    store.set("lookup.historical_provider", "labynet", changed_by="console")
    AppContext(db, store)
    assert store.get("lookup.historical_provider") == "labynet"  # a token makes laby.net usable, so it is left alone
    db.dispose()
