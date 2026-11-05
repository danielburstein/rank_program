"""Lookup service: provider selection, bounded retries, proxy rotation, caching.

Retries apply only to technical failures (timeouts, connection errors,
rate limiting, 5xx, proxy connection failures). Documented not-found
responses, other 4xx responses and invalid payloads are never retried.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, MutableMapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any

from rankrecovery.core.enums import LookupOutcome
from rankrecovery.identity import failures as F
from rankrecovery.identity.base import LookupAttempt, LookupResult
from rankrecovery.identity.failures import is_proxy_failure, is_retryable
from rankrecovery.identity.providers import (
    ClientFactory,
    ConfigurableJsonProvider,
    GeyserBedrockProvider,
    ManualProvider,
    MinecraftServicesProvider,
    MojangProvider,
    NoHistoricalProvider,
)
from rankrecovery.identity.proxies import ProxyEntry, ProxyPool, parse_proxy_list
from rankrecovery.identity.ratelimit import RateLimitCancelled, RateLimiter

CacheKey = tuple[str, str]


class LookupService:
    def __init__(
        self,
        settings: dict[str, Any],
        providers: dict[str, Any],
        proxy_pool: ProxyPool | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: MutableMapping[CacheKey, tuple[float, LookupResult]] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.settings = dict(settings)
        self.providers = providers
        self.proxy_pool = proxy_pool or ProxyPool(mode="direct")
        self.rate_limiter = rate_limiter or RateLimiter(float(self.settings.get("lookup.requests_per_minute", 100)), clock=clock, sleep=sleep)
        self.cache = cache if cache is not None else {}
        self._cache_lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng

    # ------------------------------------------------------------- settings
    @property
    def max_retries(self) -> int:
        return max(0, int(self.settings.get("lookup.max_retries", 3)))

    @property
    def backoff_base(self) -> float:
        return float(self.settings.get("lookup.backoff_base_seconds", 1.0))

    @property
    def backoff_max(self) -> float:
        return float(self.settings.get("lookup.backoff_max_seconds", 30.0))

    @property
    def cache_ttl_seconds(self) -> float:
        return float(self.settings.get("lookup.cache_ttl_hours", 24 * 7)) * 3600.0

    @property
    def bedrock_prefix(self) -> str:
        return str(self.settings.get("lookup.bedrock_prefix", "."))

    def _provider(self, key: str) -> Any | None:
        return self.providers.get(key)

    def java_provider(self) -> Any:
        key = str(self.settings.get("lookup.java_provider", "mojang"))
        provider = self._provider(key)
        if provider is None:
            raise KeyError(f"java provider {key!r} is not configured")
        return provider

    def bedrock_provider(self) -> Any:
        key = str(self.settings.get("lookup.bedrock_provider", "geyser"))
        provider = self._provider(key)
        if provider is None:
            raise KeyError(f"bedrock provider {key!r} is not configured")
        return provider

    def historical_provider(self) -> Any | None:
        key = str(self.settings.get("lookup.historical_provider", "none"))
        if key in ("", "none"):
            return None
        return self._provider(key)

    # ----------------------------------------------------------------- cache
    def _cache_get(self, provider_name: str, username: str) -> LookupResult | None:
        key = (provider_name, username.lower())
        with self._cache_lock:
            entry = self.cache.get(key)
            if entry is None:
                return None
            expires_at, result = entry
            if expires_at < self._clock():
                try:
                    del self.cache[key]
                except KeyError:  # pragma: no cover - race with another thread
                    pass
                return None
        return replace(result, attempts=list(result.attempts), cached=True)

    def _cache_set(self, provider_name: str, username: str, result: LookupResult) -> None:
        if result.outcome not in (LookupOutcome.RESOLVED, LookupOutcome.NOT_FOUND):
            return
        key = (provider_name, username.lower())
        with self._cache_lock:
            self.cache[key] = (self._clock() + self.cache_ttl_seconds, replace(result, cached=False))

    # --------------------------------------------------------------- resolve
    def resolve(self, username: str, *, is_bedrock: bool | None = None, force: bool = False, cancel: threading.Event | None = None) -> LookupResult:
        username = username.strip()
        if is_bedrock is None:
            is_bedrock = bool(self.bedrock_prefix) and username.startswith(self.bedrock_prefix)
        if is_bedrock:
            provider = self.bedrock_provider()
            if not getattr(provider, "supports_bedrock", False):
                return LookupResult(
                    queried_name=username,
                    provider=getattr(provider, "name", "?"),
                    outcome=LookupOutcome.SKIPPED,
                    failure_code=F.PROVIDER_UNSUPPORTED,
                    message="Bedrock player: the configured provider does not support Floodgate lookups; a manual UUID is required",
                )
            return self._resolve_with(provider, username, force=force, cancel=cancel)

        provider = self.java_provider()
        result = self._resolve_with(provider, username, force=force, cancel=cancel)
        if result.outcome != LookupOutcome.NOT_FOUND:
            return result

        historical = self.historical_provider()
        if historical is None:
            return replace(
                result,
                failure_code=F.HISTORICAL_NAME_UNRESOLVED,
                message=(
                    f"no current account named {username!r} ({result.provider} reported not-found); "
                    "historical username resolution is unsupported, manual identity matching required"
                ),
            )
        hist = self._resolve_with(historical, username, force=force, cancel=cancel)
        hist.attempts = list(result.attempts) + list(hist.attempts)
        if hist.outcome == LookupOutcome.RESOLVED:
            hist.is_historical = True
            return hist
        if hist.outcome == LookupOutcome.NOT_FOUND:
            return replace(hist, failure_code=F.HISTORICAL_NAME_UNRESOLVED, message=f"neither current-name nor historical provider resolved {username!r}")
        return hist

    def _resolve_with(self, provider: Any, username: str, *, force: bool, cancel: threading.Event | None) -> LookupResult:
        name = getattr(provider, "name", "provider")
        if not force:
            cached = self._cache_get(name, username)
            if cached is not None:
                return cached

        attempts: list[LookupAttempt] = []
        last: LookupResult | None = None
        rotate = self.proxy_pool.mode == "rotate"
        proxy: ProxyEntry | None = None
        total_attempts = self.max_retries + 1

        proxies_exhausted = False
        for attempt_no in range(1, total_attempts + 1):
            if cancel is not None and cancel.is_set():
                return self._cancelled(username, name, attempts)
            try:
                self.rate_limiter.acquire(name, cancel)
            except RateLimitCancelled as exc:
                return self._cancelled(username, name, attempts, str(exc))
            if self.proxy_pool.enabled:
                proxy = self.proxy_pool.next_proxy() if (proxy is None or rotate) else proxy
                if proxy is None:
                    # Every configured proxy is cooling down. Never fall back to a
                    # direct connection the operator did not ask for.
                    proxies_exhausted = True
                    break
            result, retry_after = provider.attempt(username, attempt_no=attempt_no, proxy=proxy)
            attempts.extend(result.attempts)
            last = result
            code = result.failure_code

            if result.outcome in (LookupOutcome.RESOLVED, LookupOutcome.NOT_FOUND):
                self.proxy_pool.mark_success(proxy)
                result.attempts = attempts
                self._cache_set(name, username, result)
                return result
            if result.outcome == LookupOutcome.SKIPPED:
                result.attempts = attempts
                return result

            if is_proxy_failure(code):
                self.proxy_pool.mark_failure(proxy)
                if code == F.PROXY_AUTH_FAILED and not rotate:
                    break
                if rotate:
                    proxy = self.proxy_pool.rotate()
            if not is_retryable(code):
                break
            if attempt_no >= total_attempts:
                break
            delay = retry_after if (code == F.RATE_LIMITED and retry_after is not None) else self._backoff(attempt_no)
            self._sleep(delay)

        if last is None:
            return LookupResult(
                queried_name=username,
                provider=name,
                outcome=LookupOutcome.FAILED,
                failure_code=F.PROXY_CONNECTION_FAILED,
                message="no proxy available: every configured proxy is disabled after repeated failures",
                attempts=attempts,
            )
        exhausted = self.max_retries > 0 and is_retryable(last.failure_code) and (len(attempts) >= total_attempts or proxies_exhausted)
        if last.outcome == LookupOutcome.FAILED and exhausted:
            suffix = "; every configured proxy is now disabled" if proxies_exhausted else ""
            return LookupResult(
                queried_name=username,
                provider=name,
                outcome=LookupOutcome.FAILED,
                failure_code=F.RETRIES_EXHAUSTED,
                message=f"{len(attempts)} attempt(s) failed; last failure {last.failure_code}: {last.message}{suffix}",
                attempts=attempts,
            )
        last.attempts = attempts
        return last

    def _backoff(self, attempt_no: int) -> float:
        cap = min(self.backoff_max, self.backoff_base * (2 ** (attempt_no - 1)))
        return cap * self._rng()  # full jitter

    @staticmethod
    def _cancelled(username: str, provider: str, attempts: list[LookupAttempt], message: str = "cancelled") -> LookupResult:
        return LookupResult(queried_name=username, provider=provider, outcome=LookupOutcome.SKIPPED, failure_code=None, message=message, attempts=attempts)

    # ---------------------------------------------------------- resolve_many
    def resolve_many(
        self,
        names: Iterable[str | tuple[str, bool]],
        *,
        workers: int | None = None,
        on_result: Callable[[LookupResult], None] | None = None,
        cancel: threading.Event | None = None,
        force: bool = False,
    ) -> list[LookupResult]:
        """Resolve many names concurrently with a bounded in-flight window.

        Returns results in input order. ``on_result`` is called from worker
        completion in the calling thread as each result arrives.
        """
        items: list[tuple[str, bool | None]] = []
        for item in names:
            if isinstance(item, tuple):
                items.append((item[0], item[1]))
            else:
                items.append((item, None))
        if not items:
            return []
        workers = max(1, int(workers or self.settings.get("lookup.workers", 4)))
        window = 2 * workers
        results: list[LookupResult | None] = [None] * len(items)
        pending: dict[Future[LookupResult], int] = {}
        next_index = 0

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uuid-lookup") as pool:
            while next_index < len(items) or pending:
                while next_index < len(items) and len(pending) < window and not (cancel is not None and cancel.is_set()):
                    name, is_bedrock = items[next_index]
                    fut = pool.submit(self.resolve, name, is_bedrock=is_bedrock, force=force, cancel=cancel)
                    pending[fut] = next_index
                    next_index += 1
                if not pending:
                    break
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    idx = pending.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001 - never lose a slot
                        name, _ = items[idx]
                        res = LookupResult(queried_name=name, provider="?", outcome=LookupOutcome.FAILED, failure_code=F.CONNECTION_ERROR, message=f"unexpected error: {exc}")
                    results[idx] = res
                    if on_result is not None:
                        on_result(res)
            if cancel is not None and cancel.is_set():
                for idx in range(next_index, len(items)):
                    name, _ = items[idx]
                    results[idx] = self._cancelled(name, "?", [])
        return [r for r in results if r is not None]

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                close()


# -------------------------------------------------------------------- builders
def build_default_providers(settings: dict[str, Any], client_factory: ClientFactory | None = None) -> dict[str, Any]:
    providers: dict[str, Any] = {
        "mojang": MojangProvider(settings, client_factory=client_factory),
        "minecraftservices": MinecraftServicesProvider(settings, client_factory=client_factory),
        "geyser": GeyserBedrockProvider(settings, client_factory=client_factory),
        "manual": ManualProvider(),
        "none": NoHistoricalProvider(),
    }
    historical = str(settings.get("lookup.historical_provider", "none"))
    if historical == "custom":
        url = str(settings.get("lookup.historical_url", "") or "")
        if url:
            providers["custom"] = ConfigurableJsonProvider(
                "custom",
                url,
                json_path_uuid=str(settings.get("lookup.historical_uuid_path", "id")),
                json_path_name=str(settings.get("lookup.historical_name_path", "name")),
                not_found_statuses={int(s) for s in settings.get("lookup.historical_not_found_statuses", [404])},
                settings=settings,
                client_factory=client_factory,
            )
    return providers


def build_lookup_service(
    settings: dict[str, Any],
    proxy_text: str | None = None,
    cache: MutableMapping[CacheKey, tuple[float, LookupResult]] | None = None,
    client_factory: ClientFactory | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LookupService:
    parsed = parse_proxy_list(proxy_text)
    mode = str(settings.get("lookup.proxy_mode", "direct"))
    if mode != "direct" and not parsed.entries:
        mode = "direct"
    pool = ProxyPool(parsed.entries, mode=mode, failure_threshold=int(settings.get("lookup.proxy_failure_threshold", 3)), clock=clock)
    limiter = RateLimiter(float(settings.get("lookup.requests_per_minute", 100)), clock=clock, sleep=sleep)
    providers = build_default_providers(settings, client_factory=client_factory)
    return LookupService(settings, providers, pool, limiter, cache, clock=clock, sleep=sleep)
