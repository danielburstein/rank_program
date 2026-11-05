from __future__ import annotations

import json
import ssl
import threading

import httpx
import pytest

from rankrecovery.config.defaults import DEFAULT_SETTINGS
from rankrecovery.core.enums import LookupOutcome
from rankrecovery.core.identity import floodgate_uuid_from_xuid
from rankrecovery.identity import failures as F
from rankrecovery.identity.failures import classify_exception, classify_status
from rankrecovery.identity.lookup import LookupService, build_default_providers, build_lookup_service
from rankrecovery.identity.providers import GeyserBedrockProvider, MojangProvider
from rankrecovery.identity.proxies import ProxyPool
from rankrecovery.identity.ratelimit import RateLimiter

NOTCH = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
NOTCH_HEX = "069a79f444e94726a5befca90e38aaf5"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """Builds a LookupService around an httpx.MockTransport."""

    def __init__(self, handler, settings_overrides: dict | None = None, proxy_text: str | None = None):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.clock = FakeClock()
        self.proxies_used: list[str | None] = []
        settings = dict(DEFAULT_SETTINGS)
        settings.update({"lookup.backoff_base_seconds": 1.0, "lookup.backoff_max_seconds": 8.0, "lookup.max_retries": 2})
        settings.update(settings_overrides or {})
        self.settings = settings

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        def client_factory(proxy_url: str | None) -> httpx.Client:
            self.proxies_used.append(proxy_url)
            return httpx.Client(transport=httpx.MockTransport(wrapped), headers={"User-Agent": "test"})

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.now += seconds

        self.service = build_lookup_service(settings, proxy_text, client_factory=client_factory, clock=self.clock, sleep=sleep)
        # deterministic "jitter"
        self.service._rng = lambda: 1.0


def ok_json(body: dict, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def test_happy_path_mojang_normalises_uuid():
    h = Harness(lambda r: ok_json({"id": NOTCH_HEX.upper(), "name": "Notch"}))
    r = h.service.resolve("notch")
    assert r.resolved and r.uuid == NOTCH and r.current_name == "Notch" and r.provider == "mojang"
    assert not r.is_historical and "historical ownership not verified" in r.message
    assert len(r.attempts) == 1 and r.attempts[0].http_status == 200 and r.attempts[0].response_valid
    assert r.attempts[0].resolved_uuid == NOTCH and r.attempts[0].proxy_id is None
    assert h.requests[0].url.host == "api.mojang.com" and "notch" in str(h.requests[0].url)


def test_404_is_not_found_and_not_retried():
    h = Harness(lambda r: httpx.Response(404, json={"errorMessage": "Couldn't find any profile with name xyz"}))
    r = h.service.resolve("OldNameGone")
    assert r.outcome == LookupOutcome.NOT_FOUND and not r.resolved
    assert len(h.requests) == 1 and len(r.attempts) == 1 and not h.sleeps
    assert r.attempts[0].failure_code == F.NOT_FOUND and r.attempts[0].http_status == 404
    # no historical provider configured -> the identity outcome is historical-unresolved
    assert r.failure_code == F.HISTORICAL_NAME_UNRESOLVED and "manual identity matching" in r.message


def test_204_is_not_found():
    h = Harness(lambda r: httpx.Response(204))
    r = h.service.resolve("Someone")
    assert r.outcome == LookupOutcome.NOT_FOUND and r.attempts[0].failure_code == F.NOT_FOUND and len(h.requests) == 1


def test_429_honours_retry_after_then_succeeds():
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return ok_json({"id": NOTCH_HEX, "name": "Notch"})

    h = Harness(handler)
    r = h.service.resolve("Notch")
    assert r.resolved and len(r.attempts) == 2
    assert r.attempts[0].failure_code == F.RATE_LIMITED and r.attempts[0].http_status == 429
    assert h.sleeps == [7.0]


def test_500_then_200():
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        return httpx.Response(500, text="boom") if calls["n"] == 1 else ok_json({"id": NOTCH_HEX, "name": "Notch"})

    h = Harness(handler)
    r = h.service.resolve("Notch")
    assert r.resolved and r.attempts[0].failure_code == F.HTTP_SERVER_ERROR and len(h.sleeps) == 1


def test_503_is_api_unavailable_and_retryable():
    h = Harness(lambda r: httpx.Response(503))
    r = h.service.resolve("Notch")
    assert r.failure_code == F.RETRIES_EXHAUSTED and F.API_UNAVAILABLE in r.message
    assert all(a.failure_code == F.API_UNAVAILABLE for a in r.attempts)


def test_other_4xx_not_retried():
    h = Harness(lambda r: httpx.Response(403))
    r = h.service.resolve("Notch")
    assert r.outcome == LookupOutcome.FAILED and r.failure_code == F.HTTP_CLIENT_ERROR and len(h.requests) == 1


def test_connect_timeout_retried_then_exhausted():
    def handler(r):
        raise httpx.ConnectTimeout("timed out", request=r)

    h = Harness(handler)
    r = h.service.resolve("Notch")
    assert r.outcome == LookupOutcome.FAILED and r.failure_code == F.RETRIES_EXHAUSTED
    assert len(r.attempts) == 3 and all(a.failure_code == F.CONNECTION_TIMEOUT for a in r.attempts)
    assert F.CONNECTION_TIMEOUT in r.message
    assert h.sleeps == [1.0, 2.0]  # base * 2**(n-1) with jitter factor 1.0
    assert all(a.attempt_no == i for i, a in enumerate(r.attempts, start=1))


def test_read_timeout_classified():
    def handler(r):
        raise httpx.ReadTimeout("read timed out", request=r)

    h = Harness(handler)
    r = h.service.resolve("Notch")
    assert r.attempts[0].failure_code == F.READ_TIMEOUT and r.outcome != LookupOutcome.NOT_FOUND


def test_proxy_error_is_proxy_failure_and_rotates():
    seen_proxies: list[str | None] = []

    def handler(r):
        raise httpx.ProxyError("503 Service Unavailable from proxy", request=r)

    h = Harness(handler, {"lookup.proxy_mode": "rotate", "lookup.proxy_failure_threshold": 1}, proxy_text="http://u:p@1.1.1.1:80\n2.2.2.2:80")
    seen_proxies = h.proxies_used
    r = h.service.resolve("Notch")
    assert r.outcome == LookupOutcome.FAILED and r.failure_code == F.RETRIES_EXHAUSTED
    assert all(a.failure_code == F.PROXY_CONNECTION_FAILED for a in r.attempts)
    assert all(a.failure_code != F.NOT_FOUND for a in r.attempts)
    assert all(a.proxy_id and "@" not in a.proxy_id and ":p@" not in a.error_message for a in r.attempts)
    # both proxies were tried (rotation happened) and the pool marked failures
    assert {p for p in seen_proxies if p} == {"http://u:p@1.1.1.1:80", "http://2.2.2.2:80"}
    assert all(s["total_failures"] >= 1 for s in h.service.proxy_pool.status())


def test_proxy_auth_failure():
    def handler(r):
        raise httpx.ProxyError("407 Proxy Authentication Required", request=r)

    h = Harness(handler, {"lookup.proxy_mode": "sticky"}, proxy_text="http://u:p@1.1.1.1:80")
    r = h.service.resolve("Notch")
    assert r.failure_code == F.PROXY_AUTH_FAILED and len(r.attempts) == 1


def test_proxy_tunnel_failure():
    def handler(r):
        raise httpx.ProxyError("CONNECT tunnel failed, response 403", request=r)

    h = Harness(handler, {"lookup.proxy_mode": "rotate", "lookup.max_retries": 0}, proxy_text="1.1.1.1:80")
    r = h.service.resolve("Notch")
    assert r.attempts[0].failure_code == F.PROXY_TUNNEL_FAILED


def test_dns_error():
    def handler(r):
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=r)

    h = Harness(handler, {"lookup.max_retries": 0})
    r = h.service.resolve("Notch")
    assert r.failure_code == F.DNS_ERROR


def test_tls_error():
    def handler(r):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=r)

    h = Harness(handler, {"lookup.max_retries": 0})
    r = h.service.resolve("Notch")
    assert r.failure_code == F.TLS_ERROR
    assert classify_exception(ssl.SSLError("handshake"), via_proxy=False) == F.TLS_ERROR


def test_plain_connection_error():
    def handler(r):
        raise httpx.ConnectError("Connection refused", request=r)

    h = Harness(handler, {"lookup.max_retries": 0})
    assert h.service.resolve("Notch").failure_code == F.CONNECTION_ERROR


def test_invalid_json_and_invalid_uuid():
    h = Harness(lambda r: httpx.Response(200, text="<html>not json</html>"))
    r = h.service.resolve("Notch")
    assert r.failure_code == F.INVALID_RESPONSE and len(h.requests) == 1 and not r.attempts[0].response_valid
    h2 = Harness(lambda r: ok_json({"id": "zzzz", "name": "Notch"}))
    r2 = h2.service.resolve("Notch")
    assert r2.failure_code == F.INVALID_UUID and len(h2.requests) == 1
    h3 = Harness(lambda r: ok_json({"name": "Notch"}))
    assert h3.service.resolve("Notch").failure_code == F.INVALID_RESPONSE


def test_different_name_returned_is_not_a_match():
    h = Harness(lambda r: ok_json({"id": NOTCH_HEX, "name": "SomebodyElse"}))
    r = h.service.resolve("Notch")
    assert r.outcome == LookupOutcome.NOT_FOUND and r.uuid is None
    assert r.attempts[0].failure_code == F.HISTORICAL_NAME_UNRESOLVED


def test_bedrock_routes_to_geyser_never_mojang():
    xuid = 2535459493735633

    def handler(r):
        assert r.url.host == "api.geysermc.org"
        return ok_json({"id": floodgate_uuid_from_xuid(xuid).replace("-", ""), "name": ".Axeliuz10"})

    h = Harness(handler)
    r = h.service.resolve(".Axeliuz10")
    assert r.resolved and r.provider == "geyser" and r.uuid == floodgate_uuid_from_xuid(xuid)
    assert all(req.url.host != "api.mojang.com" for req in h.requests)
    assert ".Axeliuz10" in str(h.requests[0].url) and "prefix=." in str(h.requests[0].url)


def test_bedrock_explicit_flag_and_xuid_payload():
    h = Harness(lambda r: ok_json({"xuid": 2535459493735633, "name": "Axeliuz10"}))
    r = h.service.resolve(".Axeliuz10", is_bedrock=True)
    assert r.resolved and r.uuid == floodgate_uuid_from_xuid(2535459493735633)
    h2 = Harness(lambda r: ok_json({"id": NOTCH_HEX, "name": ".Axeliuz10"}))
    assert h2.service.resolve(".Axeliuz10").failure_code == F.INVALID_UUID  # not a floodgate uuid
    h3 = Harness(lambda r: httpx.Response(400, json={"message": "gamertag invalid"}))
    assert h3.service.resolve(".bad").outcome == LookupOutcome.NOT_FOUND


def test_floodgate_uuid_derivation():
    assert floodgate_uuid_from_xuid(0x0901F64F65C7C3) == "00000000-0000-0000-0009-01f64f65c7c3"


def test_cache_hit_makes_no_http_call():
    h = Harness(lambda r: ok_json({"id": NOTCH_HEX, "name": "Notch"}))
    first = h.service.resolve("Notch")
    second = h.service.resolve("NOTCH")
    assert not first.cached and second.cached and second.uuid == NOTCH and len(h.requests) == 1
    forced = h.service.resolve("Notch", force=True)
    assert not forced.cached and len(h.requests) == 2
    h.clock.now += h.service.cache_ttl_seconds + 1
    expired = h.service.resolve("Notch")
    assert not expired.cached and len(h.requests) == 3


def test_not_found_is_cached_too():
    h = Harness(lambda r: httpx.Response(404))
    h.service.resolve("Gone")
    r = h.service.resolve("Gone")
    assert r.cached and r.outcome == LookupOutcome.NOT_FOUND and len(h.requests) == 1


def test_resolve_many_returns_one_result_per_name_in_order():
    def handler(r):
        name = str(r.url).rsplit("/", 1)[-1]
        if name == "missing":
            return httpx.Response(404)
        return ok_json({"id": NOTCH_HEX, "name": name})

    h = Harness(handler)
    seen: list[str] = []
    names = ["Alpha", "missing", "Gamma", "Delta", "Epsilon"]
    results = h.service.resolve_many(names, workers=3, on_result=lambda res: seen.append(res.queried_name))
    assert [r.queried_name for r in results] == names
    assert results[1].outcome == LookupOutcome.NOT_FOUND and all(r.resolved for i, r in enumerate(results) if i != 1)
    assert sorted(seen) == sorted(names)


def test_resolve_many_cancel():
    cancel = threading.Event()
    cancel.set()
    h = Harness(lambda r: ok_json({"id": NOTCH_HEX, "name": "x"}))
    results = h.service.resolve_many(["A", "B"], workers=2, cancel=cancel)
    assert len(results) == 2 and all(r.outcome == LookupOutcome.SKIPPED for r in results) and not h.requests


def test_historical_provider_tried_after_not_found():
    def handler(r):
        if r.url.host == "api.mojang.com":
            return httpx.Response(404)
        return ok_json({"data": {"uuid": NOTCH_HEX, "username": "OldName"}})

    h = Harness(
        handler,
        {
            "lookup.historical_provider": "custom",
            "lookup.historical_url": "https://history.example/{name}",
            "lookup.historical_uuid_path": "data.uuid",
            "lookup.historical_name_path": "data.username",
        },
    )
    r = h.service.resolve("OldName")
    assert r.resolved and r.is_historical and r.provider == "custom" and len(r.attempts) == 2
    assert r.attempts[0].provider == "mojang" and r.attempts[1].provider == "custom"


def test_manual_and_none_providers_skip():
    settings = dict(DEFAULT_SETTINGS)
    providers = build_default_providers(settings, client_factory=lambda p: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert providers["manual"].lookup("x").failure_code == F.PROVIDER_UNSUPPORTED
    assert providers["none"].lookup("x").outcome == LookupOutcome.SKIPPED
    service = LookupService({**settings, "lookup.bedrock_provider": "mojang"}, providers, ProxyPool(), RateLimiter(1000))
    r = service.resolve(".bedrockguy")
    assert r.outcome == LookupOutcome.SKIPPED and r.failure_code == F.PROVIDER_UNSUPPORTED


def test_classify_status_table():
    assert classify_status(200, {404}) == (LookupOutcome.RESOLVED, None)
    assert classify_status(404, {404}) == (LookupOutcome.NOT_FOUND, F.NOT_FOUND)
    assert classify_status(429, {404})[1] == F.RATE_LIMITED
    assert classify_status(500, {404})[1] == F.HTTP_SERVER_ERROR
    assert classify_status(502, {404})[1] == F.API_UNAVAILABLE
    assert classify_status(418, {404})[1] == F.HTTP_CLIENT_ERROR
    assert classify_status(201, {404})[1] == F.INVALID_RESPONSE


def test_provider_direct_lookup_and_user_agent():
    seen = {}

    def handler(r):
        seen["ua"] = r.headers.get("user-agent")
        return ok_json({"id": NOTCH_HEX, "name": "Notch"})

    p = MojangProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "rankrecovery/0.1 (+https://github.com/danielburstein/rank_program)"}))
    assert p.lookup("Notch").resolved and seen["ua"].startswith("rankrecovery/0.1")
    g = GeyserBedrockProvider(dict(DEFAULT_SETTINGS), client_factory=lambda proxy: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    res, _ = g.attempt(".x")
    assert res.failure_code == F.API_UNAVAILABLE


def test_attempt_records_raw_excerpt_without_secrets():
    body = json.dumps({"id": NOTCH_HEX, "name": "Notch", "note": "http://user:secret@host:1"})
    h = Harness(lambda r: httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"}))
    r = h.service.resolve("Notch")
    assert "secret" not in r.attempts[0].raw_excerpt


@pytest.mark.parametrize("exc,code", [
    (httpx.WriteTimeout("w"), F.READ_TIMEOUT),
    (httpx.PoolTimeout("p"), F.READ_TIMEOUT),
    (httpx.RemoteProtocolError("peer closed"), F.CONNECTION_ERROR),
    (httpx.ConnectError("getaddrinfo failed"), F.DNS_ERROR),
])
def test_classify_exception_table(exc, code):
    assert classify_exception(exc, via_proxy=False) == code
