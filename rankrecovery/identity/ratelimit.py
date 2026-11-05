"""Thread-safe token-bucket rate limiter keyed by provider name."""

from __future__ import annotations

import threading
import time


class RateLimitCancelled(RuntimeError):
    """Raised when a wait for a token is cancelled."""


class _Bucket:
    def __init__(self, rate_per_minute: float, clock) -> None:
        self.capacity = max(1.0, float(rate_per_minute))
        self.rate_per_second = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = clock()

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
        self.updated = now


class RateLimiter:
    """``acquire(provider)`` blocks until a token is available (bounded wait)."""

    def __init__(
        self,
        default_per_minute: float = 100.0,
        per_provider: dict[str, float] | None = None,
        *,
        max_wait_seconds: float = 120.0,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.default_per_minute = default_per_minute
        self.per_provider = dict(per_provider or {})
        self.max_wait_seconds = max_wait_seconds
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, provider: str) -> _Bucket:
        bucket = self._buckets.get(provider)
        if bucket is None:
            bucket = _Bucket(self.per_provider.get(provider, self.default_per_minute), self._clock)
            self._buckets[provider] = bucket
        return bucket

    def acquire(self, provider: str, cancel: threading.Event | None = None) -> float:
        """Take one token, waiting if needed. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                bucket = self._bucket(provider)
                bucket.refill(self._clock())
                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return waited
                deficit = 1.0 - bucket.tokens
                wait = min(deficit / bucket.rate_per_second, 1.0)
            if cancel is not None and cancel.is_set():
                raise RateLimitCancelled("cancelled while waiting for rate limit")
            if waited + wait > self.max_wait_seconds:
                raise RateLimitCancelled(f"rate limit wait exceeded {self.max_wait_seconds}s")
            self._sleep(wait)
            waited += wait

    def set_rate(self, provider: str, per_minute: float) -> None:
        with self._lock:
            self.per_provider[provider] = per_minute
            self._buckets.pop(provider, None)
