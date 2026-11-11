"""Lookup service: provider selection, bounded retries, proxy rotation, caching.

Retries apply only to technical failures (timeouts, connection errors,
rate limiting, 5xx, proxy connection failures). Documented not-found
responses, other 4xx responses and invalid payloads are never retried.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, MutableMapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from rankrecovery.core.enums import LookupOutcome
from rankrecovery.identity import failures as F
from rankrecovery.identity.base import LookupAttempt, LookupResult
from rankrecovery.identity.failures import is_proxy_failure, is_retryable
from rankrecovery.identity.providers import (
    ClientFactory,
    ConfigurableJsonProvider,
    CraftyHistoricalProvider,
    GeyserBedrockProvider,
    LabyNetHistoricalProvider,
    ManualProvider,
    MinecraftServicesProvider,
    MojangProvider,
    NoHistoricalProvider,
)
from rankrecovery.identity.proxies import ProxyEntry, ProxyPool, parse_proxy_list
from rankrecovery.identity.ratelimit import RateLimitCancelled, RateLimiter

CacheKey = tuple[str, str]
log = logging.getLogger(__name__)
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
OUTAGE_CODES = frozenset({F.API_UNAVAILABLE, F.HTTP_SERVER_ERROR, F.CONNECTION_ERROR, F.CONNECTION_TIMEOUT, F.READ_TIMEOUT, F.DNS_ERROR})


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
        self.rate_limiter = rate_limiter or RateLimiter(float(self.settings.get("lookup.requests_per_minute", 60)), per_provider=_per_provider_rates(self.settings), burst=_burst(self.settings), max_wait_seconds=_max_wait(self.settings), clock=clock, sleep=sleep)
        self.cache = cache if cache is not None else {}
        self._cache_lock = threading.Lock()
        self._prefetched: dict[CacheKey, LookupResult] = {}  # answers from a bulk request, handed to the next single lookup of that name
        self._outages: dict[str, dict[str, float]] = {}  # provider -> consecutive outage failures and when its breaker closes again
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

    @property
    def bulk_lookups(self) -> bool:
        return bool(self.settings.get("lookup.bulk_lookups", True))

    @property
    def rate_limit_cooldown(self) -> float:
        return max(0.0, float(self.settings.get("lookup.rate_limit_cooldown_seconds", 10)))

    @property
    def outage_threshold(self) -> int:
        return max(1, int(self.settings.get("lookup.outage_threshold", 5)))

    @property
    def outage_cooldown(self) -> float:
        return max(1.0, float(self.settings.get("lookup.outage_cooldown_seconds", 120)))

    # --------------------------------------------------------------- breaker
    def _outage_open(self, provider: str) -> dict[str, float] | None:
        """The provider's breaker state when it is open (recent consecutive outage failures), else None."""
        with self._cache_lock:
            state = self._outages.get(provider)
            if state and state.get("open_until", 0.0) > self._clock():
                return dict(state)
        return None

    def _note_outcome(self, provider: str, code: str | None, *, ok: bool) -> None:
        with self._cache_lock:
            if ok or code not in OUTAGE_CODES:
                if ok:
                    self._outages.pop(provider, None)
                return
            state = self._outages.setdefault(provider, {"failures": 0.0, "open_until": 0.0})
            state["failures"] += 1
            if state["failures"] >= self.outage_threshold:
                state["open_until"] = self._clock() + self.outage_cooldown
                if state["failures"] == self.outage_threshold:
                    log.warning("%s answered %d outage errors in a row (%s); skipping it for %.0fs so the run does not stall", provider, int(state["failures"]), code, self.outage_cooldown)

    def _outage_result(self, username: str, provider: str, state: dict[str, float]) -> LookupResult:
        return LookupResult(queried_name=username, provider=provider, outcome=LookupOutcome.FAILED, failure_code=F.API_UNAVAILABLE, message=f"{provider} is unavailable ({int(state['failures'])} consecutive failures); skipped without a request, retry later with lookup --retry-failed")

    @property
    def rate_limit_max_cooldown(self) -> float:
        return max(self.rate_limit_cooldown, float(self.settings.get("lookup.rate_limit_max_cooldown_seconds", 300)))

    @property
    def rate_limit_max_retries(self) -> int:
        return max(0, int(self.settings.get("lookup.rate_limit_max_retries", 10)))

    def _looks_bedrock(self, username: str) -> bool:
        return bool(self.bedrock_prefix) and username.startswith(self.bedrock_prefix)

    @property
    def history_mode(self) -> str:
        """``fallback``: ask the historical provider only when no account holds the name now. ``always``: cross-check every name."""
        mode = str(self.settings.get("lookup.history_mode", "fallback"))
        return mode if mode in ("fallback", "always") else "fallback"

    @property
    def history_pick(self) -> str:
        """``most_recent``: when several accounts used the name, take the latest holder. ``ambiguous``: leave it to a person."""
        pick = str(self.settings.get("lookup.history_pick", "most_recent"))
        return pick if pick in ("most_recent", "ambiguous") else "most_recent"

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
            return  # failures are never cached; a several-holders answer is re-fetched so its candidate list stays complete
        key = (provider_name, username.lower())
        with self._cache_lock:
            self.cache[key] = (self._clock() + self.cache_ttl_seconds, replace(result, cached=False))

    # --------------------------------------------------------------- resolve
    def resolve(self, username: str, *, is_bedrock: bool | None = None, force: bool = False, cancel: threading.Event | None = None) -> LookupResult:
        username = username.strip()
        if is_bedrock is None:
            is_bedrock = self._looks_bedrock(username)
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

        if not JAVA_NAME.fullmatch(username):
            return LookupResult(queried_name=username, provider="validation", outcome=LookupOutcome.NOT_FOUND, failure_code=F.HISTORICAL_NAME_UNRESOLVED, message=f"{username!r} cannot be a Java username (letters, digits and _ only, 1-16 characters); probably a chat line or a bot, not looked up")
        provider = self.java_provider()
        current = self._resolve_with(provider, username, force=force, cancel=cancel)
        historical = self.historical_provider()
        if historical is None:
            if current.outcome == LookupOutcome.NOT_FOUND:
                return replace(
                    current,
                    failure_code=F.HISTORICAL_NAME_UNRESOLVED,
                    message=(
                        f"no current account named {username!r} ({current.provider} reported not-found); "
                        "historical username resolution is unsupported, manual identity matching required"
                    ),
                )
            return current
        if current.outcome in (LookupOutcome.FAILED, LookupOutcome.SKIPPED):
            return current  # technical failure: retry later rather than guess from history alone
        if current.outcome == LookupOutcome.RESOLVED and self.history_mode == "fallback":
            return current  # somebody holds the name today: that account is taken as the player, done
        hist = self._resolve_with(historical, username, force=force, cancel=cancel)
        return self._combine(username, current, hist, pick=self.history_pick)

    @staticmethod
    def _combine(username: str, current: LookupResult, hist: LookupResult, *, pick: str = "ambiguous") -> LookupResult:
        """Merge the current-name owner with the accounts that previously held the name.

        Names can be reused, so a current owner and a previous holder may be
        different people. Agreement resolves. Several holders are settled by
        ``pick``: ``most_recent`` takes the account that held the name last
        (the current holder if there is one, else the latest dated record),
        ``ambiguous`` leaves the choice to a person.
        """
        attempts = list(current.attempts) + list(hist.attempts)
        current_uuid = current.uuid if current.outcome == LookupOutcome.RESOLVED else None
        hist_candidates = list(hist.candidates) if hist.candidates else ([{"uuid": hist.uuid, "current_name": hist.current_name, "matched_name": username, "changed_at": None, "source": hist.provider}] if hist.outcome == LookupOutcome.RESOLVED and hist.uuid else [])
        candidates: dict[str, dict] = {c["uuid"]: dict(c) for c in hist_candidates if c.get("uuid")}
        if current_uuid:
            candidates.setdefault(current_uuid, {"uuid": current_uuid, "current_name": current.current_name, "matched_name": username, "changed_at": None, "source": current.provider})
            candidates[current_uuid]["current_holder"] = True

        if hist.outcome == LookupOutcome.FAILED and hist.failure_code != F.AMBIGUOUS_IDENTITY:
            # history check failed technically: keep the current-name answer, say the check did not happen
            if current_uuid:
                return replace(current, attempts=attempts, message=f"{current.message}; historical check via {hist.provider} failed ({hist.failure_code})")
            return replace(hist, attempts=attempts)

        if len(candidates) == 1:
            cand = next(iter(candidates.values()))
            historical_match = hist.outcome == LookupOutcome.RESOLVED
            if historical_match and current_uuid:
                message = f"current owner and {hist.provider} history agree: {cand['current_name']!r} held {username!r}"
            elif historical_match:
                message = hist.message
            else:
                message = f"{current.message}; {hist.provider} has no record of a previous holder"
            return LookupResult(queried_name=username, provider=hist.provider if historical_match else current.provider, outcome=LookupOutcome.RESOLVED, uuid=cand["uuid"], current_name=cand.get("current_name") or current.current_name, message=message, attempts=attempts, is_historical=historical_match, candidates=[cand])

        if len(candidates) > 1:
            ordered = sorted(candidates.values(), key=_recency_key, reverse=True)
            summary = "; ".join(f"{c['uuid']} (now {c.get('current_name') or '?'}{', current holder' if c.get('current_holder') else ''}{', took the name ' + str(c['changed_at'])[:10] if c.get('changed_at') else ''})" for c in ordered)
            chosen = _most_recent(ordered) if pick == "most_recent" else None
            if chosen is not None:
                others = "; ".join(f"{c['uuid']} (now {c.get('current_name') or '?'})" for c in ordered if c is not chosen)
                message = f"{len(candidates)} accounts have used {username!r}; took the most recent holder {chosen.get('current_name') or '?'!r} ({chosen['uuid']}). Others: {others}"
                is_current = bool(chosen.get("current_holder"))
                return LookupResult(queried_name=username, provider=current.provider if is_current else hist.provider, outcome=LookupOutcome.RESOLVED, uuid=chosen["uuid"], current_name=chosen.get("current_name") or (current.current_name if is_current else None), message=message, attempts=attempts, is_historical=not is_current, candidates=ordered)
            return LookupResult(queried_name=username, provider=hist.provider, outcome=LookupOutcome.FAILED, failure_code=F.AMBIGUOUS_IDENTITY, message=f"{len(candidates)} accounts have used {username!r}; choose one manually: {summary}", attempts=attempts, candidates=ordered)

        return LookupResult(queried_name=username, provider=current.provider, outcome=LookupOutcome.NOT_FOUND, failure_code=F.HISTORICAL_NAME_UNRESOLVED, message=f"no current account named {username!r} and {hist.provider} has no record of a previous holder", attempts=attempts)

    def _resolve_with(self, provider: Any, username: str, *, force: bool, cancel: threading.Event | None) -> LookupResult:
        name = getattr(provider, "name", "provider")
        with self._cache_lock:
            prefetched = self._prefetched.pop((name, username.lower()), None)
        if prefetched is not None:
            return prefetched  # answered by a bulk request moments ago
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
        attempt_no = 0
        failures = 0  # technical failures, bounded by lookup.max_retries
        rate_hits = 0  # 429s have their own budget: pause, retry, pause longer each time
        last_pause = 0.0

        while True:
            attempt_no += 1
            if cancel is not None and cancel.is_set():
                return self._cancelled(username, name, attempts)
            outage = self._outage_open(name)
            if outage is not None:
                return self._outage_result(username, name, outage) if last is None else self._finalise_outage(username, name, attempts, outage)
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
            self._note_outcome(name, code, ok=result.outcome in (LookupOutcome.RESOLVED, LookupOutcome.NOT_FOUND))

            if result.outcome in (LookupOutcome.RESOLVED, LookupOutcome.NOT_FOUND):
                self.proxy_pool.mark_success(proxy)
                result.attempts = attempts
                self._cache_set(name, username, result)
                return result
            if result.outcome == LookupOutcome.SKIPPED:
                result.attempts = attempts
                return result

            if code == F.RATE_LIMITED:
                rate_hits += 1
                last_pause = self._rate_limit_pause(rate_hits, retry_after)
                self.rate_limiter.penalise(name, last_pause)  # every worker waits, not just this one
                if rate_hits > self.rate_limit_max_retries:
                    break
                self._sleep(last_pause)
                continue

            if is_proxy_failure(code):
                self.proxy_pool.mark_failure(proxy)
                if code == F.PROXY_AUTH_FAILED and not rotate:
                    break
                if rotate:
                    proxy = self.proxy_pool.rotate()
            if not is_retryable(code):
                break
            failures += 1
            if failures >= total_attempts:
                break
            self._sleep(self._backoff(failures))

        if last is None:
            return LookupResult(
                queried_name=username,
                provider=name,
                outcome=LookupOutcome.FAILED,
                failure_code=F.PROXY_CONNECTION_FAILED,
                message="no proxy available: every configured proxy is disabled after repeated failures",
                attempts=attempts,
            )
        rate_budget_spent = rate_hits > self.rate_limit_max_retries
        exhausted = is_retryable(last.failure_code) and (rate_budget_spent or (self.max_retries > 0 and (failures >= total_attempts or proxies_exhausted)))
        if last.outcome == LookupOutcome.FAILED and exhausted:
            if rate_budget_spent:
                message = f"{name} answered 429 to {rate_hits} tries for this name (pauses grew to {last_pause:.0f}s); run the lookup again later and it resumes where it stopped"
            else:
                suffix = "; every configured proxy is now disabled" if proxies_exhausted else ""
                message = f"{len(attempts)} attempt(s) failed; last failure {last.failure_code}: {last.message}{suffix}"
            return LookupResult(
                queried_name=username,
                provider=name,
                outcome=LookupOutcome.FAILED,
                failure_code=F.RETRIES_EXHAUSTED,
                message=message,
                attempts=attempts,
            )
        last.attempts = attempts
        return last

    def _finalise_outage(self, username: str, provider: str, attempts: list[LookupAttempt], state: dict[str, float]) -> LookupResult:
        result = self._outage_result(username, provider, state)
        result.attempts = attempts
        return result

    def _rate_limit_pause(self, hit_no: int, retry_after: float | None) -> float:
        """How long to pause after the ``hit_no``-th 429 for one request: the server's Retry-After the first time, then doubling."""
        escalated = self.rate_limit_cooldown * (2 ** (hit_no - 1))
        if retry_after is not None and hit_no == 1:
            pause = retry_after
        elif retry_after is not None:
            pause = max(retry_after, escalated)
        else:
            pause = escalated
        return min(max(pause, 0.0), self.rate_limit_max_cooldown)

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
        if self.bulk_lookups and not (cancel is not None and cancel.is_set()):
            java_names = [n for n, b in items if not (b if b is not None else self._looks_bedrock(n.strip()))]
            self._prefetch_bulk(java_names, force=force, cancel=cancel)
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

    # ------------------------------------------------------------------ bulk
    def _prefetch_bulk(self, names: list[str], *, force: bool, cancel: threading.Event | None) -> int:
        """Ask the Java provider for ten names per request; the answers feed the single lookups that follow.

        Mojang's single-name endpoint rate-limits a list of eighty names within
        seconds, its bulk endpoint answers the same list in eight requests.
        """
        provider = self.java_provider()
        if not getattr(provider, "supports_bulk", False):
            return 0
        pname = getattr(provider, "name", "provider")
        todo: list[str] = []
        seen: set[str] = set()
        for raw in names:
            n = raw.strip()
            key = n.lower()
            if not n or key in seen or not JAVA_NAME.fullmatch(n):
                continue  # one impossible name would fail the whole batch; resolve() answers it without a request
            seen.add(key)
            if not force and self._cache_get(pname, n) is not None:
                continue
            todo.append(n)
        size = max(1, int(getattr(provider, "bulk_size", 10)))
        fetched = 0
        for i in range(0, len(todo), size):
            if cancel is not None and cancel.is_set():
                break
            chunk = todo[i : i + size]
            results = self._bulk_with_retries(provider, chunk, cancel)
            if results is None:
                continue  # these names take the single-lookup path
            for n in chunk:
                res = results.get(n.lower())
                if res is None:
                    continue
                with self._cache_lock:
                    self._prefetched[(pname, n.lower())] = res
                self._cache_set(pname, n, res)
                fetched += 1
        return fetched

    def _bulk_with_retries(self, provider: Any, chunk: list[str], cancel: threading.Event | None) -> dict[str, LookupResult] | None:
        name = getattr(provider, "name", "provider")
        rotate = self.proxy_pool.mode == "rotate"
        proxy: ProxyEntry | None = None
        attempt_no = 0
        failures = 0
        rate_hits = 0
        while True:
            attempt_no += 1
            if cancel is not None and cancel.is_set():
                return None
            if self._outage_open(name) is not None:
                return None
            try:
                self.rate_limiter.acquire(name, cancel)
            except RateLimitCancelled:
                return None
            if self.proxy_pool.enabled:
                proxy = self.proxy_pool.next_proxy() if (proxy is None or rotate) else proxy
                if proxy is None:
                    return None
            results, attempt, retry_after = provider.attempt_bulk(chunk, attempt_no=attempt_no, proxy=proxy)
            if results is not None:
                self.proxy_pool.mark_success(proxy)
                self._note_outcome(name, None, ok=True)
                return results
            code = attempt.failure_code
            self._note_outcome(name, code, ok=False)
            if code == F.RATE_LIMITED:
                rate_hits += 1
                pause = self._rate_limit_pause(rate_hits, retry_after)
                self.rate_limiter.penalise(name, pause)
                if rate_hits > self.rate_limit_max_retries:
                    log.warning("bulk lookup via %s rate limited %d times in a row; the names fall back to single lookups", name, rate_hits)
                    return None
                self._sleep(pause)
                continue
            if is_proxy_failure(code):
                self.proxy_pool.mark_failure(proxy)
                if rotate:
                    proxy = self.proxy_pool.rotate()
            failures += 1
            if not is_retryable(code) or failures > self.max_retries:
                log.info("bulk lookup of %d names via %s gave up (%s: %s); those names fall back to single lookups", len(chunk), name, code, attempt.error_message[:120])
                return None
            self._sleep(self._backoff(failures))

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                close()


def _recency_key(cand: dict) -> tuple[int, float]:
    """Current holder first, then the latest 'took the name' date; undated records last."""
    if cand.get("current_holder"):
        return (2, 0.0)
    raw = cand.get("changed_at")
    if not raw:
        return (0, 0.0)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (1, parsed.timestamp())
    except ValueError:
        return (0, 0.0)


def _most_recent(ordered: list[dict]) -> dict | None:
    """The account that held the name last, or None when no record is dated."""
    if not ordered:
        return None
    best = ordered[0]
    return best if _recency_key(best)[0] > 0 else None


# -------------------------------------------------------------------- builders
def _per_provider_rates(settings: dict[str, Any]) -> dict[str, float]:
    return {
        "mojang": float(settings.get("lookup.mojang_requests_per_minute", 60)),
        "minecraftservices": float(settings.get("lookup.minecraftservices_requests_per_minute", 60)),
        "labynet": float(settings.get("lookup.labynet_requests_per_minute", 30)),
        "crafty": float(settings.get("lookup.crafty_requests_per_minute", 60)),
    }


def _max_wait(settings: dict[str, Any]) -> float:
    """A worker may wait out the longest rate-limit pause plus a margin before its lookup is skipped."""
    return max(120.0, float(settings.get("lookup.rate_limit_max_cooldown_seconds", 300)) + 60.0)


def _burst(settings: dict[str, Any]) -> float | None:
    burst = float(settings.get("lookup.burst", 10) or 0)
    return burst if burst > 0 else None


def build_default_providers(settings: dict[str, Any], client_factory: ClientFactory | None = None) -> dict[str, Any]:
    providers: dict[str, Any] = {
        "mojang": MojangProvider(settings, client_factory=client_factory),
        "minecraftservices": MinecraftServicesProvider(settings, client_factory=client_factory),
        "geyser": GeyserBedrockProvider(settings, client_factory=client_factory),
        "manual": ManualProvider(),
        "none": NoHistoricalProvider(),
        "labynet": LabyNetHistoricalProvider(settings, client_factory=client_factory),
        "crafty": CraftyHistoricalProvider(settings, client_factory=client_factory),
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
    limiter = RateLimiter(float(settings.get("lookup.requests_per_minute", 60)), per_provider=_per_provider_rates(settings), burst=_burst(settings), max_wait_seconds=_max_wait(settings), clock=clock, sleep=sleep)
    providers = build_default_providers(settings, client_factory=client_factory)
    return LookupService(settings, providers, pool, limiter, cache, clock=clock, sleep=sleep)
