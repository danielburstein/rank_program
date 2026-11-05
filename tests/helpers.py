"""Shared helpers for service-level tests."""

from __future__ import annotations

import io

from PIL import Image, PngImagePlugin

from rankrecovery.core.enums import ExtractorType, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.parsing import RawRow
from rankrecovery.extraction.base import DetectedRegion, ExtractionOutput, ExtractionSettings, TextExtractor


def png_bytes(seed: int = 0, *, text_chunks: dict[str, str] | None = None, size: tuple[int, int] = (64, 48)) -> bytes:
    img = Image.new("RGB", size, (seed % 255, (seed * 7) % 255, (seed * 13) % 255))
    for x in range(size[0]):
        img.putpixel((x, (x * seed) % size[1]), (255, 255, 255))
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    for k, v in (text_chunks or {}).items():
        info.add_text(k, v)
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


class StubExtractor:
    """Returns canned rows per filename; implements the TextExtractor contract."""

    def __init__(self, rows_by_filename: dict[str, list[RawRow]], *, engine: str = "stub"):
        self.rows_by_filename = rows_by_filename
        self.settings = ExtractionSettings(extractor=ExtractorType.OCR, engine=engine, model="stub-1")
        self.calls = 0

    def extract(self, image_bytes: bytes, *, filename: str = "", regions: list[DetectedRegion] | None = None) -> ExtractionOutput:
        self.calls += 1
        rows = self.rows_by_filename.get(filename, [])
        return ExtractionOutput(rows=list(rows), regions=[], extractor=ExtractorType.OCR, engine=self.settings.engine, model="stub-1", version="1", raw_output="stub", full_text=" ".join(r.text for r in rows), settings_fingerprint=self.settings.fingerprint())


def tab(text: str, *, trailing: TrailingIcon = TrailingIcon.NONE, leading: LeadingIcon = LeadingIcon.DIAMOND, confidence: float = 0.95, cut_off: bool = False) -> RawRow:
    return RawRow(text=text, region=RegionKind.TAB, leading_icon=leading, trailing_icon=trailing, confidence=confidence, username_cut_off=cut_off, bbox=(0, 0, 100, 10))


_ = TextExtractor
