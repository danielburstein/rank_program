"""Lookup providers.

Every provider performs exactly one HTTP attempt per ``attempt()`` call and
records a :class:`LookupAttempt`; retries, backoff, proxy rotation and caching
live in :mod:`rankrecovery.identity.lookup`.

Endpoint assumptions (verify against current provider documentation):

* Mojang: ``GET https://api.mojang.com/users/profiles/minecraft/{name}`` returns
  ``{"id": "<32 hex>", "name": "<CurrentName>"}``; a documented not-found is
  HTTP 404 (older API versions returned 204 No Content).
* Minecraft Services: ``GET https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}``
  with the same body shape; not-found is 404.
* Geyser Global API: ``GET https://api.geysermc.org/v2/utils/uuid/bedrock_or_java/{username}?prefix={prefix}``
  returns ``{"id": "<undashed Floodgate UUID>", "name": "..."}``; 400 for an
  invalid gamertag, 404 when unknown, 503 when the Xbox API is unavailable.
* Mojang removed the name-history endpoint in September 2022, so historical
  usernames cannot be resolved through Mojang. A third-party service may be
  configured explicitly via :class:`ConfigurableJsonProvider`.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from rankrecovery.core.enums import LookupOutcome
from rankrecovery.core.identity import floodgate_uuid_from_xuid, is_floodgate_uuid, normalise_uuid
from rankrecovery.identity import failures as F
from rankrecovery.identity.base import LookupAttempt, LookupResult
from rankrecovery.identity.failures import classify_exception, classify_status, retry_after_seconds
from rankrecovery.identity.proxies import ProxyEntry, scrub_credentials

USER_AGENT = "rankrecovery/0.1 (+https://github.com/danielburstein/rank_program)"
ClientFactory = Callable[[str | None], httpx.Client]


class ProviderPayloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_client_factory(settings: dict[str, Any]) -> ClientFactory:
    connect = float(settings.get("lookup.connect_timeout_seconds", 10))
    read = float(settings.get("lookup.read_timeout_seconds", 15))

    def factory(proxy_url: str | None) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(connect=connect, read=read, write=read, pool=connect),
            proxy=proxy_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
            trust_env=False,
        )

    return factory


class _ClientCache:
    """One httpx.Client per proxy URL (httpx.Client is thread-safe)."""

    def __init__(self, factory: ClientFactory) -> None:
        self._factory = factory
        self._clients: dict[str | None, httpx.Client] = {}
        self._lock = threading.Lock()

    def get(self, proxy_url: str | None) -> httpx.Client:
        with self._lock:
            client = self._clients.get(proxy_url)
            if client is None:
                client = self._factory(proxy_url)
                self._clients[proxy_url] = client
            return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:  # pragma: no cover - best effort
                    pass
            self._clients.clear()


class HttpJsonProvider:
    """Base class for providers that GET a URL and read a JSON object."""

    name: str = "http"
    supports_bedrock: bool = False
    supports_historical: bool = False
    not_found_statuses: frozenset[int] = frozenset({404})

    def __init__(
        self,
        url_template: str,
        *,
        settings: dict[str, Any] | None = None,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url_template = url_template
        self.settings = dict(settings or {})
        self._clients = _ClientCache(client_factory or default_client_factory(self.settings))
        self._clock = clock

    # ------------------------------------------------------------ overrides
    def build_url(self, username: str) -> str:
        return self.url_template.format(name=quote(username, safe=""))

    def parse_payload(self, data: Any, username: str) -> tuple[str, str]:
        """Return (canonical uuid, name) or raise ProviderPayloadError."""
        if not isinstance(data, dict):
            raise ProviderPayloadError(F.INVALID_RESPONSE, "response is not a JSON object")
        raw_id = data.get("id")
        name = data.get("name")
        if not isinstance(raw_id, str) or not raw_id:
            raise ProviderPayloadError(F.INVALID_RESPONSE, "response has no 'id' string")
        if not isinstance(name, str) or not name:
            raise ProviderPayloadError(F.INVALID_RESPONSE, "response has no 'name' string")
        uuid = normalise_uuid(raw_id)
        if uuid is None:
            raise ProviderPayloadError(F.INVALID_UUID, f"malformed uuid {raw_id[:40]!r}")
        return uuid, name

    def validate_match(self, username: str, uuid: str, name: str) -> tuple[LookupOutcome, str | None, str]:
        """Decide whether the returned profile actually answers the query."""
        if name.lower() != username.lower():
            return (
                LookupOutcome.NOT_FOUND,
                F.HISTORICAL_NAME_UNRESOLVED,
                f"provider returned a different name ({name!r}) for query {username!r}; not treated as a match",
            )
        return LookupOutcome.RESOLVED, None, "current-name match; historical ownership not verified"

    # --------------------------------------------------------------- lookup
    def attempt(self, username: str, *, attempt_no: int = 1, proxy: ProxyEntry | None = None) -> tuple[LookupResult, float | None]:
        """One HTTP attempt. Returns (result, retry_after_seconds)."""
        started_wall = datetime.now(UTC)
        started = self._clock()
        url = self.build_url(username)
        proxy_id = proxy.safe_id if proxy else None
        attempt = LookupAttempt(
            provider=self.name,
            queried_name=username,
            attempt_no=attempt_no,
            started_at=started_wall,
            elapsed_ms=0,
            http_status=None,
            outcome=LookupOutcome.FAILED,
            proxy_id=proxy_id,
        )
        retry_after: float | None = None
        try:
            client = self._clients.get(proxy.url if proxy else None)
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001 - every transport failure must be classified
            attempt.elapsed_ms = int((self._clock() - started) * 1000)
            attempt.failure_code = classify_exception(exc, via_proxy=proxy is not None)
            attempt.error_message = scrub_credentials(f"{type(exc).__name__}: {exc}")[:500]
            return self._result(username, attempt, LookupOutcome.FAILED, None, None, attempt.failure_code, attempt.error_message), None

        attempt.elapsed_ms = int((self._clock() - started) * 1000)
        attempt.http_status = response.status_code
        attempt.raw_excerpt = scrub_credentials(response.text[:200]) if response.content else ""
        outcome, code = classify_status(response.status_code, self.not_found_statuses)
        if outcome == LookupOutcome.NOT_FOUND:
            attempt.outcome = LookupOutcome.NOT_FOUND
            attempt.failure_code = code
            attempt.response_valid = True
            attempt.error_message = f"documented not-found response (HTTP {response.status_code})"
            return self._result(username, attempt, outcome, None, None, code, attempt.error_message), None
        if outcome == LookupOutcome.FAILED:
            attempt.failure_code = code
            attempt.error_message = f"HTTP {response.status_code}"
            if code == F.RATE_LIMITED:
                retry_after = retry_after_seconds(response)
            return self._result(username, attempt, outcome, None, None, code, attempt.error_message), retry_after

        try:
            data = json.loads(response.text) if response.content else None
        except ValueError:
            attempt.failure_code = F.INVALID_RESPONSE
            attempt.error_message = "response body is not valid JSON"
            return self._result(username, attempt, LookupOutcome.FAILED, None, None, F.INVALID_RESPONSE, attempt.error_message), None
        try:
            uuid, name = self.parse_payload(data, username)
        except ProviderPayloadError as exc:
            attempt.failure_code = exc.code
            attempt.error_message = exc.message
            return self._result(username, attempt, LookupOutcome.FAILED, None, None, exc.code, exc.message), None

        attempt.response_valid = True
        match_outcome, match_code, message = self.validate_match(username, uuid, name)
        if match_outcome == LookupOutcome.RESOLVED:
            attempt.outcome = LookupOutcome.RESOLVED
            attempt.resolved_uuid = uuid
            attempt.resolved_name = name
            attempt.error_message = ""
            return self._result(username, attempt, LookupOutcome.RESOLVED, uuid, name, None, message), None
        attempt.outcome = match_outcome
        attempt.failure_code = match_code
        attempt.error_message = message
        attempt.resolved_uuid = uuid
        attempt.resolved_name = name
        return self._result(username, attempt, match_outcome, None, name, match_code, message), None

    def lookup(self, username: str) -> LookupResult:
        result, _ = self.attempt(username)
        return result

    def _result(
        self,
        username: str,
        attempt: LookupAttempt,
        outcome: LookupOutcome,
        uuid: str | None,
        name: str | None,
        code: str | None,
        message: str,
    ) -> LookupResult:
        return LookupResult(
            queried_name=username,
            provider=self.name,
            outcome=outcome,
            uuid=uuid,
            current_name=name,
            failure_code=code,
            message=message,
            attempts=[attempt],
            is_historical=self.supports_historical and outcome == LookupOutcome.RESOLVED,
        )

    def close(self) -> None:
        self._clients.close()


class MojangProvider(HttpJsonProvider):
    name = "mojang"
    not_found_statuses = frozenset({404, 204})

    def __init__(self, settings: dict[str, Any] | None = None, *, client_factory: ClientFactory | None = None, clock=time.monotonic) -> None:
        settings = dict(settings or {})
        super().__init__(
            str(settings.get("lookup.mojang_name_url", "https://api.mojang.com/users/profiles/minecraft/{name}")),
            settings=settings,
            client_factory=client_factory,
            clock=clock,
        )


class MinecraftServicesProvider(HttpJsonProvider):
    name = "minecraftservices"
    not_found_statuses = frozenset({404})

    def __init__(self, settings: dict[str, Any] | None = None, *, client_factory: ClientFactory | None = None, clock=time.monotonic) -> None:
        settings = dict(settings or {})
        super().__init__(
            str(settings.get("lookup.minecraftservices_name_url", "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}")),
            settings=settings,
            client_factory=client_factory,
            clock=clock,
        )


class GeyserBedrockProvider(HttpJsonProvider):
    """Resolve Floodgate (Bedrock) players through the Geyser Global API."""

    name = "geyser"
    supports_bedrock = True
    # 400 = gamertag invalid / no Xbox account for it -> documented not-found.
    not_found_statuses = frozenset({404, 400})

    def __init__(self, settings: dict[str, Any] | None = None, *, client_factory: ClientFactory | None = None, clock=time.monotonic) -> None:
        settings = dict(settings or {})
        self.prefix = str(settings.get("lookup.bedrock_prefix", "."))
        super().__init__(
            str(settings.get("lookup.geyser_url", "https://api.geysermc.org/v2/utils/uuid/bedrock_or_java/{name}?prefix={prefix}")),
            settings=settings,
            client_factory=client_factory,
            clock=clock,
        )

    def build_url(self, username: str) -> str:
        return self.url_template.format(name=quote(username, safe=""), prefix=quote(self.prefix, safe=""))

    def parse_payload(self, data: Any, username: str) -> tuple[str, str]:
        if not isinstance(data, dict):
            raise ProviderPayloadError(F.INVALID_RESPONSE, "response is not a JSON object")
        name = data.get("name")
        raw_id = data.get("id")
        xuid = data.get("xuid")
        uuid: str | None = None
        if isinstance(raw_id, str) and raw_id:
            uuid = normalise_uuid(raw_id)
            if uuid is None:
                raise ProviderPayloadError(F.INVALID_UUID, f"malformed uuid {raw_id[:40]!r}")
        elif xuid is not None:
            try:
                uuid = floodgate_uuid_from_xuid(int(xuid))
            except (TypeError, ValueError) as exc:
                raise ProviderPayloadError(F.INVALID_UUID, f"malformed xuid {str(xuid)[:40]!r}") from exc
        else:
            raise ProviderPayloadError(F.INVALID_RESPONSE, "response has neither 'id' nor 'xuid'")
        if not is_floodgate_uuid(uuid):
            raise ProviderPayloadError(F.INVALID_UUID, f"{uuid} is not a Floodgate UUID")
        if not isinstance(name, str) or not name:
            name = username
        return uuid, name

    def validate_match(self, username: str, uuid: str, name: str) -> tuple[LookupOutcome, str | None, str]:
        q = username.lower()
        n = name.lower()
        if n == q or n == q.removeprefix(self.prefix.lower()) or (self.prefix.lower() + n) == q:
            return LookupOutcome.RESOLVED, None, "Floodgate gamertag match via Geyser Global API (cached players only)"
        return (
            LookupOutcome.NOT_FOUND,
            F.HISTORICAL_NAME_UNRESOLVED,
            f"Geyser returned gamertag {name!r} for query {username!r}; not treated as a match",
        )


class ManualProvider:
    """Placeholder provider that never touches the network."""

    name = "manual"
    supports_bedrock = True
    supports_historical = False

    def attempt(self, username: str, *, attempt_no: int = 1, proxy: ProxyEntry | None = None) -> tuple[LookupResult, float | None]:
        return self.lookup(username), None

    def lookup(self, username: str) -> LookupResult:
        return LookupResult(
            queried_name=username,
            provider=self.name,
            outcome=LookupOutcome.SKIPPED,
            failure_code=F.PROVIDER_UNSUPPORTED,
            message="manual resolution required; no automatic provider configured",
        )

    def close(self) -> None:  # pragma: no cover - nothing to close
        return None


class NoHistoricalProvider(ManualProvider):
    """Historical-name resolution is unsupported unless an admin configures a service.

    Mojang removed the public name-history endpoint in September 2022, so
    there is no first-party way to map an old username to a UUID.
    """

    name = "none"
    supports_historical = True

    def lookup(self, username: str) -> LookupResult:
        return LookupResult(
            queried_name=username,
            provider=self.name,
            outcome=LookupOutcome.SKIPPED,
            failure_code=F.PROVIDER_UNSUPPORTED,
            message="historical username resolution is not supported by the configured providers",
        )


def _json_path(data: Any, path: str) -> Any:
    """Resolve a dotted path like ``profile.uuid`` or ``results.0.id``."""
    current = data
    for part in (p for p in path.split(".") if p):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class ConfigurableJsonProvider(HttpJsonProvider):
    """Generic GET+JSON provider an admin can point at a documented service.

    The admin is responsible for being entitled to use the service and for
    respecting its terms and rate limits.
    """

    supports_historical = True

    def __init__(
        self,
        name: str,
        url_template: str,
        *,
        json_path_uuid: str = "id",
        json_path_name: str = "name",
        not_found_statuses: set[int] | frozenset[int] = frozenset({404}),
        historical: bool = True,
        bedrock: bool = False,
        settings: dict[str, Any] | None = None,
        client_factory: ClientFactory | None = None,
        clock=time.monotonic,
    ) -> None:
        super().__init__(url_template, settings=settings, client_factory=client_factory, clock=clock)
        self.name = name
        self.json_path_uuid = json_path_uuid
        self.json_path_name = json_path_name
        self.not_found_statuses = frozenset(not_found_statuses)
        self.supports_historical = historical
        self.supports_bedrock = bedrock

    def parse_payload(self, data: Any, username: str) -> tuple[str, str]:
        raw_id = _json_path(data, self.json_path_uuid)
        name = _json_path(data, self.json_path_name)
        if not isinstance(raw_id, str) or not raw_id:
            raise ProviderPayloadError(F.INVALID_RESPONSE, f"no uuid at {self.json_path_uuid!r}")
        uuid = normalise_uuid(raw_id)
        if uuid is None:
            raise ProviderPayloadError(F.INVALID_UUID, f"malformed uuid {raw_id[:40]!r}")
        if not isinstance(name, str) or not name:
            name = username
        return uuid, name

    def validate_match(self, username: str, uuid: str, name: str) -> tuple[LookupOutcome, str | None, str]:
        if self.supports_historical:
            return LookupOutcome.RESOLVED, None, f"historical-name match reported by {self.name}; verify before trusting"
        return super().validate_match(username, uuid, name)
