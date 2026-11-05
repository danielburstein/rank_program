"""Classify lookup failures into the exact flag codes required by the specification.

A documented not-found response is never confused with a technical failure.
"""

from __future__ import annotations

import ssl

import httpx

from rankrecovery.core.enums import LookupOutcome

PROXY_CONNECTION_FAILED = "UUID_LOOKUP_PROXY_CONNECTION_FAILED"
PROXY_AUTH_FAILED = "UUID_LOOKUP_PROXY_AUTH_FAILED"
PROXY_TUNNEL_FAILED = "UUID_LOOKUP_PROXY_TUNNEL_FAILED"
CONNECTION_TIMEOUT = "UUID_LOOKUP_CONNECTION_TIMEOUT"
READ_TIMEOUT = "UUID_LOOKUP_READ_TIMEOUT"
CONNECTION_ERROR = "UUID_LOOKUP_CONNECTION_ERROR"
DNS_ERROR = "UUID_LOOKUP_DNS_ERROR"
TLS_ERROR = "UUID_LOOKUP_TLS_ERROR"
RATE_LIMITED = "UUID_LOOKUP_RATE_LIMITED"
HTTP_CLIENT_ERROR = "UUID_LOOKUP_HTTP_CLIENT_ERROR"
HTTP_SERVER_ERROR = "UUID_LOOKUP_HTTP_SERVER_ERROR"
API_UNAVAILABLE = "UUID_LOOKUP_API_UNAVAILABLE"
INVALID_RESPONSE = "UUID_LOOKUP_INVALID_RESPONSE"
INVALID_UUID = "UUID_LOOKUP_INVALID_UUID"
PROVIDER_UNSUPPORTED = "UUID_LOOKUP_PROVIDER_UNSUPPORTED"
HISTORICAL_NAME_UNRESOLVED = "UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED"
AMBIGUOUS_IDENTITY = "UUID_LOOKUP_AMBIGUOUS_IDENTITY"
RETRIES_EXHAUSTED = "UUID_LOOKUP_RETRIES_EXHAUSTED"
NOT_FOUND = "UUID_LOOKUP_NOT_FOUND"

RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        CONNECTION_TIMEOUT,
        READ_TIMEOUT,
        CONNECTION_ERROR,
        DNS_ERROR,
        RATE_LIMITED,
        HTTP_SERVER_ERROR,
        API_UNAVAILABLE,
        PROXY_CONNECTION_FAILED,
        PROXY_TUNNEL_FAILED,
    }
)

PROXY_CODES: frozenset[str] = frozenset({PROXY_CONNECTION_FAILED, PROXY_AUTH_FAILED, PROXY_TUNNEL_FAILED})

_DNS_MARKERS = (
    "getaddrinfo",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "name resolution",
    "no address associated",
    "could not resolve",
)
_TLS_MARKERS = ("ssl", "tls", "certificate", "handshake")
_PROXY_AUTH_MARKERS = ("407", "authentication required", "proxy authentication", "unauthorized", "auth")
_PROXY_TUNNEL_MARKERS = ("tunnel", "connect tunnel", "connect method", "connect failed")


def _message(exc: BaseException) -> str:
    parts = [str(exc)]
    cause = exc.__cause__ or exc.__context__
    depth = 0
    while cause is not None and depth < 4:
        parts.append(str(cause))
        cause = cause.__cause__ or cause.__context__
        depth += 1
    return " | ".join(p for p in parts if p).lower()


def classify_exception(exc: BaseException, *, via_proxy: bool) -> str:
    """Map an exception raised during an HTTP lookup to a flag code."""
    msg = _message(exc)
    if isinstance(exc, ssl.SSLError):
        return TLS_ERROR
    if isinstance(exc, httpx.ProxyError):
        if any(m in msg for m in _PROXY_AUTH_MARKERS):
            return PROXY_AUTH_FAILED
        if any(m in msg for m in _PROXY_TUNNEL_MARKERS):
            return PROXY_TUNNEL_FAILED
        return PROXY_CONNECTION_FAILED
    if isinstance(exc, httpx.ConnectTimeout):
        return CONNECTION_TIMEOUT
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return READ_TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        if any(m in msg for m in _DNS_MARKERS):
            return DNS_ERROR
        if any(m in msg for m in _TLS_MARKERS):
            return TLS_ERROR
        if via_proxy and any(m in msg for m in _PROXY_TUNNEL_MARKERS):
            return PROXY_TUNNEL_FAILED
        return CONNECTION_ERROR
    if isinstance(exc, httpx.TransportError):
        if any(m in msg for m in _TLS_MARKERS):
            return TLS_ERROR
        return CONNECTION_ERROR
    if any(m in msg for m in _TLS_MARKERS):
        return TLS_ERROR
    return CONNECTION_ERROR


def classify_status(status: int, not_found_statuses: frozenset[int] | set[int]) -> tuple[LookupOutcome, str | None]:
    """Map an HTTP status to (outcome, failure_code).

    ``LookupOutcome.RESOLVED`` here only means "a candidate body arrived";
    the provider still validates the payload.
    """
    if status in not_found_statuses:
        return LookupOutcome.NOT_FOUND, NOT_FOUND
    if status == 200:
        return LookupOutcome.RESOLVED, None
    if status == 429:
        return LookupOutcome.FAILED, RATE_LIMITED
    if status in (502, 503, 504):
        return LookupOutcome.FAILED, API_UNAVAILABLE
    if status >= 500:
        return LookupOutcome.FAILED, HTTP_SERVER_ERROR
    if 400 <= status < 500:
        return LookupOutcome.FAILED, HTTP_CLIENT_ERROR
    if 200 <= status < 300:
        # 201/202/206... are not documented responses for these providers.
        return LookupOutcome.FAILED, INVALID_RESPONSE
    return LookupOutcome.FAILED, INVALID_RESPONSE


def is_retryable(code: str | None) -> bool:
    return code is not None and code in RETRYABLE_CODES


def is_proxy_failure(code: str | None) -> bool:
    return code is not None and code in PROXY_CODES


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    """Parse a numeric Retry-After header (seconds). HTTP-date forms are ignored."""
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return max(0.0, value)
