"""Metadata-based AI-generation indicators.

This is *suspicion*, not proof. Absence of indicators never proves authenticity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rankrecovery.core.enums import AiStatus


@dataclass(frozen=True, slots=True)
class AiIndicatorHit:
    field: str  # metadata key or "filename"
    matched: str  # indicator string or regex that matched
    value_excerpt: str
    weak: bool = False  # filename-only evidence


@dataclass(frozen=True, slots=True)
class AiScanResult:
    status: AiStatus
    hits: tuple[AiIndicatorHit, ...]

    @property
    def flagged(self) -> bool:
        return self.status in (AiStatus.AI_INDICATOR_FOUND, AiStatus.MANUALLY_FLAGGED)

    @property
    def weak_only(self) -> bool:
        return self.status == AiStatus.AI_FILENAME_INDICATOR


def scan_metadata(
    metadata: dict[str, str],
    *,
    indicators: list[str],
    filename: str | None = None,
    filename_patterns: list[str] | None = None,
    inspect_filename: bool = True,
) -> AiScanResult:
    hits: list[AiIndicatorHit] = []
    lowered_indicators = [i.lower() for i in indicators if i]
    for key, value in metadata.items():
        haystack = f"{key} {value}".lower()
        for ind in lowered_indicators:
            if ind in haystack:
                excerpt = str(value)[:120]
                hits.append(AiIndicatorHit(field=key, matched=ind, value_excerpt=excerpt))
                break
    weak_hits: list[AiIndicatorHit] = []
    if inspect_filename and filename:
        for pattern in filename_patterns or []:
            try:
                if re.search(pattern, filename, flags=re.IGNORECASE):
                    weak_hits.append(AiIndicatorHit(field="filename", matched=pattern, value_excerpt=filename[:120], weak=True))
            except re.error:
                continue
    if hits:
        return AiScanResult(AiStatus.AI_INDICATOR_FOUND, tuple(hits + weak_hits))
    if weak_hits:
        return AiScanResult(AiStatus.AI_FILENAME_INDICATOR, tuple(weak_hits))
    return AiScanResult(AiStatus.NO_AI_INDICATOR_FOUND, ())
