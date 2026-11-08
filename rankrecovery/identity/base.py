"""Lookup provider contract and the structured result every provider returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from rankrecovery.core.enums import LookupOutcome


@dataclass(slots=True)
class LookupAttempt:
    """One HTTP attempt, persisted verbatim (minus secrets)."""

    provider: str
    queried_name: str
    attempt_no: int
    started_at: datetime
    elapsed_ms: int
    http_status: int | None
    outcome: LookupOutcome
    failure_code: str | None = None  # e.g. UUID_LOOKUP_READ_TIMEOUT
    error_message: str = ""
    proxy_id: str | None = None  # safe identifier, never credentials
    resolved_uuid: str | None = None
    resolved_name: str | None = None
    response_valid: bool = False
    raw_excerpt: str = ""


@dataclass(slots=True)
class LookupResult:
    queried_name: str
    provider: str
    outcome: LookupOutcome
    uuid: str | None = None  # canonical dashed
    current_name: str | None = None
    failure_code: str | None = None
    message: str = ""
    attempts: list[LookupAttempt] = field(default_factory=list)
    is_historical: bool = False  # True only when a historical-name provider matched the old name
    cached: bool = False
    candidates: list[dict] = field(default_factory=list)  # ambiguous matches: {uuid, current_name, changed_at, source}

    @property
    def resolved(self) -> bool:
        return self.outcome == LookupOutcome.RESOLVED and self.uuid is not None


class LookupProvider(Protocol):
    name: str
    supports_bedrock: bool
    supports_historical: bool

    def lookup(self, username: str) -> LookupResult: ...
