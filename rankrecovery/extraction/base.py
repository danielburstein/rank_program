"""Pluggable text-extraction interface.

Both the Anthropic vision extractor and the local OCR extractor implement
:class:`TextExtractor` and return the same :class:`ExtractionOutput`, so the
parser, database and GUI never care which engine produced the rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from rankrecovery.core.enums import ExtractorType, RegionKind
from rankrecovery.core.parsing import BBox, RawRow


@dataclass(slots=True)
class DetectedRegion:
    kind: RegionKind
    bbox: BBox  # full-image pixels
    columns: list[BBox] = field(default_factory=list)  # TAB list column crops
    confidence: float = 1.0
    notes: str = ""


@dataclass(slots=True)
class ExtractionSettings:
    """Settings that influence the output and therefore the cache key."""

    extractor: ExtractorType
    engine: str  # "anthropic" | "tesseract" | "easyocr"
    model: str = ""  # vision model id or OCR engine version
    upscale_factor: int = 2
    max_crop_edge: int = 1568
    effort: str = "medium"
    max_tokens: int = 16000
    preprocessing: str = "default"
    prompt_version: str = "1"
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "extractor": self.extractor.value,
                "engine": self.engine,
                "model": self.model,
                "upscale": self.upscale_factor,
                "max_edge": self.max_crop_edge,
                "effort": self.effort,
                "preprocessing": self.preprocessing,
                "prompt_version": self.prompt_version,
                "extra": self.extra,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:20]


@dataclass(slots=True)
class ExtractionOutput:
    rows: list[RawRow]
    regions: list[DetectedRegion]
    extractor: ExtractorType
    engine: str
    model: str
    version: str
    raw_output: str  # verbatim engine output (JSON responses, TSV from tesseract...)
    full_text: str  # all text read, for indexing and Mode 6 search
    words: list[dict[str, Any]] = field(default_factory=list)  # word/line boxes: {text,x,y,w,h,conf,region}
    settings_fingerprint: str = ""
    cost: dict[str, Any] = field(default_factory=dict)  # tokens, api calls, elapsed
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class TextExtractor(Protocol):
    """Extractor contract. Implementations must be thread-safe."""

    settings: ExtractionSettings

    def extract(self, image_bytes: bytes, *, filename: str = "", regions: list[DetectedRegion] | None = None) -> ExtractionOutput:
        """Extract player rows from one image.

        ``regions`` lets a caller (e.g. Mode 6 rescan with a user-drawn region)
        restrict or override region detection.
        """
        ...


class ExtractorUnavailable(RuntimeError):
    """Raised when an extractor cannot run (no API key, engine missing)."""
