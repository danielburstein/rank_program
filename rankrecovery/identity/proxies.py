"""Proxy list parsing and a thread-safe rotating pool.

Credentials are never exposed through ``safe_id``, ``repr`` or logs.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
_HOSTPORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-\[\]:]+?):(?P<port>\d{1,5})$")
_CRED_RE = re.compile(r"//[^/@\s]+:[^/@\s]*@")


def scrub_credentials(text: str) -> str:
    """Remove ``user:pass@`` fragments from any string destined for logs or the DB."""
    if not text:
        return text
    text = _CRED_RE.sub("//***:***@", text)
    # bare "user:pass@host" without scheme
    return re.sub(r"(?<![\w/])[^\s:@/]+:[^\s@/]*@(?=[\w.\-\[])", "***:***@", text)


@dataclass(frozen=True, slots=True)
class ProxyEntry:
    url: str  # full URL including credentials; used only to configure httpx
    scheme: str
    host: str
    port: int
    has_auth: bool
    safe_id: str  # "http://host:port#abcd" - never contains credentials

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ProxyEntry({self.safe_id})"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.safe_id


@dataclass(slots=True)
class ProxyParseError:
    line_no: int
    line: str
    reason: str


@dataclass(slots=True)
class ProxyParseResult:
    entries: list[ProxyEntry] = field(default_factory=list)
    errors: list[ProxyParseError] = field(default_factory=list)


def _make_entry(scheme: str, host: str, port: int, user: str | None, password: str | None) -> ProxyEntry:
    if not host:
        raise ValueError("empty host")
    if not (1 <= port <= 65535):
        raise ValueError(f"port {port} out of range")
    auth = ""
    has_auth = bool(user)
    if has_auth:
        auth = quote(user or "", safe="") + ":" + quote(password or "", safe="") + "@"
    url = f"{scheme}://{auth}{host}:{port}"
    digest = hashlib.sha256(url.encode()).hexdigest()[:6]
    safe_id = f"{scheme}://{host}:{port}#{digest}"
    return ProxyEntry(url=url, scheme=scheme, host=host, port=port, has_auth=has_auth, safe_id=safe_id)


def parse_proxy_line(line: str) -> ProxyEntry:
    """Parse one proxy line in any supported format.

    Supported: ``scheme://host:port``, ``scheme://user:pass@host:port``,
    ``host:port``, ``host:port:user:pass``, ``user:pass@host:port``.
    """
    text = line.strip()
    if not text:
        raise ValueError("empty line")
    if "://" in text:
        parts = urlsplit(text)
        scheme = (parts.scheme or "").lower()
        if scheme not in _SCHEMES:
            raise ValueError(f"unsupported scheme {scheme!r}")
        if parts.hostname is None or parts.port is None:
            raise ValueError("missing host or port")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("unexpected path or query in proxy URL")
        user = unquote(parts.username) if parts.username is not None else None
        password = unquote(parts.password) if parts.password is not None else None
        return _make_entry(scheme, parts.hostname, parts.port, user, password)
    if "@" in text:
        creds, _, hostport = text.rpartition("@")
        user, _, password = creds.partition(":")
        m = _HOSTPORT_RE.match(hostport)
        if not m:
            raise ValueError("expected user:pass@host:port")
        return _make_entry("http", m.group("host"), int(m.group("port")), user, password)
    pieces = text.split(":")
    if len(pieces) == 2:
        m = _HOSTPORT_RE.match(text)
        if not m:
            raise ValueError("expected host:port")
        return _make_entry("http", m.group("host"), int(m.group("port")), None, None)
    if len(pieces) == 4:
        host, port, user, password = pieces
        if not port.isdigit():
            raise ValueError("expected host:port:user:pass")
        return _make_entry("http", host, int(port), user, password)
    raise ValueError("unrecognised proxy format")


def parse_proxy_list(text: str | None) -> ProxyParseResult:
    """Parse a whole proxy list; one bad line never aborts the parse."""
    result = ProxyParseResult()
    if not text:
        return result
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        try:
            entry = parse_proxy_line(line)
        except ValueError as exc:
            result.errors.append(ProxyParseError(line_no, scrub_credentials(raw.strip()), str(exc)))
            continue
        if entry.url in seen:
            result.errors.append(ProxyParseError(line_no, entry.safe_id, "duplicate proxy"))
            continue
        seen.add(entry.url)
        result.entries.append(entry)
    return result


@dataclass(slots=True)
class _ProxyState:
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    disabled_until: float | None = None


class ProxyPool:
    """Thread-safe proxy selection with failure tracking.

    Modes: ``direct`` (never use proxies), ``rotate`` (round-robin per
    request), ``sticky`` (keep using one proxy until it fails).
    """

    MODES = ("direct", "rotate", "sticky")

    def __init__(
        self,
        entries: list[ProxyEntry] | None = None,
        *,
        mode: str = "direct",
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        clock=time.monotonic,
    ) -> None:
        if mode not in self.MODES:
            raise ValueError(f"unknown proxy mode {mode!r}")
        self.mode = mode
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._entries: list[ProxyEntry] = list(entries or [])
        self._state: dict[str, _ProxyState] = {e.safe_id: _ProxyState() for e in self._entries}
        self._cursor = 0
        self._sticky: ProxyEntry | None = None
        self._lock = threading.Lock()

    # -------------------------------------------------------------- queries
    @property
    def enabled(self) -> bool:
        return self.mode != "direct" and bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[ProxyEntry]:
        return list(self._entries)

    def _available(self, entry: ProxyEntry) -> bool:
        st = self._state[entry.safe_id]
        if st.disabled_until is None:
            return True
        if self._clock() >= st.disabled_until:
            st.disabled_until = None
            st.consecutive_failures = 0
            return True
        return False

    def next_proxy(self) -> ProxyEntry | None:
        """Pick the proxy for the next request, or None for a direct connection."""
        if not self.enabled:
            return None
        with self._lock:
            if self.mode == "sticky" and self._sticky is not None and self._available(self._sticky):
                return self._sticky
            n = len(self._entries)
            for _ in range(n):
                entry = self._entries[self._cursor % n]
                self._cursor = (self._cursor + 1) % n
                if self._available(entry):
                    if self.mode == "sticky":
                        self._sticky = entry
                    return entry
            return None  # every proxy is cooling down

    def rotate(self) -> ProxyEntry | None:
        """Force moving to another proxy (used after a proxy failure)."""
        with self._lock:
            self._sticky = None
        return self.next_proxy()

    # ------------------------------------------------------------- feedback
    def mark_failure(self, entry: ProxyEntry | None) -> None:
        if entry is None:
            return
        with self._lock:
            st = self._state.setdefault(entry.safe_id, _ProxyState())
            st.consecutive_failures += 1
            st.total_failures += 1
            if st.consecutive_failures >= self.failure_threshold:
                st.disabled_until = self._clock() + self.cooldown_seconds
                if self._sticky is not None and self._sticky.safe_id == entry.safe_id:
                    self._sticky = None

    def mark_success(self, entry: ProxyEntry | None) -> None:
        if entry is None:
            return
        with self._lock:
            st = self._state.setdefault(entry.safe_id, _ProxyState())
            st.consecutive_failures = 0
            st.total_successes += 1

    def is_disabled(self, entry: ProxyEntry) -> bool:
        with self._lock:
            return not self._available(entry)

    def status(self) -> list[dict]:
        """Safe status report for the GUI (no credentials)."""
        with self._lock:
            now = self._clock()
            out = []
            for e in self._entries:
                st = self._state[e.safe_id]
                out.append(
                    {
                        "proxy": e.safe_id,
                        "has_auth": e.has_auth,
                        "consecutive_failures": st.consecutive_failures,
                        "total_failures": st.total_failures,
                        "total_successes": st.total_successes,
                        "disabled": st.disabled_until is not None and now < st.disabled_until,
                    }
                )
            return out

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ProxyPool(mode={self.mode}, proxies={[e.safe_id for e in self._entries]})"
