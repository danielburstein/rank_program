"""Local OCR extractor (Tesseract by default, EasyOCR optional).

The pipeline: detect HUD regions -> crop each region/column -> build
preprocessing variants tuned for Minecraft pixel text -> run OCR on each
variant and keep the best -> group words into lines -> split TAB lines at
ping numbers -> detect decorative icons around each line -> emit RawRow.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image

from rankrecovery.core.enums import ExtractorType, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.parsing import BBox, RawRow
from rankrecovery.extraction.base import (
    DetectedRegion,
    ExtractionOutput,
    ExtractionSettings,
    ExtractorUnavailable,
)
from rankrecovery.imaging.decode import DecodeError, decode_image
from rankrecovery.imaging.preprocess import Variant, pil_to_bgr, variants
from rankrecovery.imaging.regions import CropMapping, crop_and_upscale, detect_regions, fallback_full_image

log = logging.getLogger(__name__)

_SYSTEM_WORDS = {"died", "won", "joined", "left", "quit", "killed", "was", "slain", "fell", "drowned", "burned", "enabled", "disabled"}
_ICON_TOKEN_RE = re.compile(r"^[^A-Za-z0-9_.]{1,2}$|^[oO0°º©®@QgCc(){}<>~¢¤•·]$")
_NUMERIC_RE = re.compile(r"^\d{2,4}$")
_ICONISH_LETTERS = set("oO0cCQq@°º©«»®·•")
_TRAILING_PUNCT = ".,;'\"!?)(]|*`´’‘“”"
DEFAULT_VARIANTS = ["text_only", "otsu_smooth"]


@dataclass(slots=True)
class Word:
    text: str
    x: int  # in variant (upscaled) crop coordinates
    y: int
    w: int
    h: int
    conf: float
    line_key: tuple[int, int, int]


@dataclass(slots=True)
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def bbox(self) -> BBox:
        x0 = min(w.x for w in self.words)
        y0 = min(w.y for w in self.words)
        x1 = max(w.x + w.w for w in self.words)
        y1 = max(w.y + w.h for w in self.words)
        return (x0, y0, x1 - x0, y1 - y0)

    @property
    def height(self) -> float:
        return float(np.median([w.h for w in self.words]))


class TesseractExtractor:
    """TextExtractor implementation using Tesseract through pytesseract."""

    def __init__(self, settings: ExtractionSettings, region_detector: Any | None = None) -> None:
        try:
            import pytesseract  # noqa: F401
        except ImportError as exc:
            raise ExtractorUnavailable("pytesseract is not installed") from exc
        if shutil.which("tesseract") is None:
            raise ExtractorUnavailable("tesseract binary not found on PATH")
        self.settings = settings
        self.region_detector = region_detector or detect_regions
        self._variant_names = list(settings.extra.get("ocr_variants") or DEFAULT_VARIANTS)
        try:
            import pytesseract as _pt

            self._version = str(_pt.get_tesseract_version())
        except Exception:
            self._version = "unknown"

    # ------------------------------------------------------------------ public
    def extract(self, image_bytes: bytes, *, filename: str = "", regions: list[DetectedRegion] | None = None) -> ExtractionOutput:
        started = time.monotonic()
        output = ExtractionOutput(
            rows=[],
            regions=[],
            extractor=ExtractorType.OCR,
            engine="tesseract",
            model=f"tesseract {self._version}",
            version=self._version,
            raw_output="",
            full_text="",
            settings_fingerprint=self.settings.fingerprint(),
            cost={"ocr_passes": 0},
        )
        try:
            img = decode_image(image_bytes)
        except DecodeError as exc:
            output.error = f"decode: {exc}"
            return output
        if regions is None:
            try:
                regions = self.region_detector(img)
            except Exception as exc:  # pragma: no cover - defensive
                output.warnings.append(f"region detection failed: {exc}")
                regions = []
            if not regions:
                regions = [fallback_full_image(img)]
        output.regions = regions
        raw_parts: list[str] = []
        texts: list[str] = []
        for region in regions:
            crops = region.columns or [region.bbox]
            for crop_bbox in crops:
                try:
                    rows, lines_text, words, raw = self._read_crop(img, crop_bbox, region, output)
                except Exception as exc:  # one bad crop must not kill the image
                    log.warning("ocr crop failed: %s", exc)
                    output.warnings.append(f"crop {crop_bbox} failed: {exc}")
                    continue
                output.rows.extend(rows)
                output.words.extend(words)
                texts.extend(lines_text)
                raw_parts.append(raw)
        output.raw_output = "\n".join(raw_parts)
        output.full_text = "\n".join(texts)
        output.cost["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return output

    # ------------------------------------------------------------------- crop
    def _read_crop(self, img: Image.Image, bbox: BBox, region: DetectedRegion, output: ExtractionOutput) -> tuple[list[RawRow], list[str], list[dict[str, Any]], str]:
        import pytesseract

        crop, mapping = crop_and_upscale(img, bbox, 1.0, None)
        bgr = pil_to_bgr(crop)
        factor = float(max(1, self.settings.upscale_factor))
        all_variants = {v.name: v for v in variants(bgr, factor=factor)}
        chosen = [all_variants[n] for n in self._variant_names if n in all_variants] or list(all_variants.values())[:1]
        psm = self._psm_for(region, bbox)
        config = f"--psm {psm} -c preserve_interword_spaces=1"
        best: tuple[float, Variant, dict[str, list]] | None = None
        for variant in chosen:
            try:
                data = pytesseract.image_to_data(variant.image, config=config, output_type=pytesseract.Output.DICT)
            except pytesseract.TesseractError as exc:
                output.warnings.append(f"tesseract error on {variant.name}: {exc}")
                continue
            output.cost["ocr_passes"] = output.cost.get("ocr_passes", 0) + 1
            score = _score(data)
            if best is None or score > best[0]:
                best = (score, variant, data)
        if best is None:
            return [], [], [], ""
        _, variant, data = best
        words = _words_from_data(data)
        lines = _group_lines(words)
        scale = variant.scale  # variant pixels per crop pixel
        text_mask = variant.image < 128  # black text on white -> True where ink
        rows: list[RawRow] = []
        line_texts: list[str] = []
        word_dicts: list[dict[str, Any]] = []
        img_right = img.width
        for line in lines:
            line_texts.append(" ".join(w.text for w in line.words))
            for w in line.words:
                fx, fy, fw, fh = _to_full(mapping, scale, (w.x, w.y, w.w, w.h))
                word_dicts.append({"text": w.text, "x": fx, "y": fy, "w": fw, "h": fh, "conf": round(w.conf / 100, 3), "region": region.kind.value})
            segments = _split_tab_segments(line) if region.kind in (RegionKind.TAB, RegionKind.FULL, RegionKind.UNKNOWN) else [line]
            for seg in segments:
                row = self._row_from_line(seg, text_mask, mapping, scale, region, img_right, variant.name)
                if row is not None:
                    rows.append(row)
        raw = f"# region={region.kind.value} bbox={bbox} variant={variant.name} psm={psm}\n" + "\n".join(line_texts)
        return rows, line_texts, word_dicts, raw

    def _psm_for(self, region: DetectedRegion, bbox: BBox) -> int:
        if region.kind == RegionKind.NAMETAG:
            return 7 if bbox[3] <= 80 else 6
        return 6

    def _row_from_line(self, line: Line, ink: np.ndarray, mapping: CropMapping, scale: float, region: DetectedRegion, img_right: int, variant_name: str) -> RawRow | None:
        words = [_strip_punct(w) for w in line.words]
        words = [w for w in words if w.text]
        leading_tokens: list[Word] = []
        trailing_tokens: list[Word] = []
        while words and (_is_icon_token(words[0].text) or _is_junk_token(words[0].text)):
            leading_tokens.append(words.pop(0))
        while words and _is_icon_token(words[-1].text):
            trailing_tokens.insert(0, words.pop())
        while len(words) > 1 and len(words[-1].text) <= 2 and region.kind != RegionKind.CHAT:
            words.pop()  # ping-number or icon debris after the username
        # inner icon tokens (the second diamond, "co" for the infinity glyph) are dropped from the text
        inner = [w for w in words if not _is_icon_token(w.text)]
        if not inner:
            return None
        text = " ".join(w.text for w in inner)
        if not any(ch.isalnum() for ch in text):
            return None
        line_h = line.height
        lx, ly, lw, lh = line.bbox
        leading = LeadingIcon.NONE
        trailing = TrailingIcon.NONE
        if leading_tokens:
            leading = _classify_leading(ink, leading_tokens[-1], line_h)
        else:
            leading = _probe_icon(ink, inner[0], line_h, side="left")[0]
        if trailing_tokens:
            trailing = _classify_trailing(ink, trailing_tokens[0], line_h)
        else:
            trailing = _probe_icon(ink, inner[-1], line_h, side="right")[1]
        full_bbox = _to_full(mapping, scale, (min(w.x for w in inner), ly, max(w.x + w.w for w in inner) - min(w.x for w in inner), lh))
        cut_off = full_bbox[0] + full_bbox[2] >= img_right - 3
        confs = [w.conf for w in inner if w.conf >= 0]
        confidence = (sum(confs) / len(confs) / 100.0) if confs else 0.0
        if variant_name == "adaptive_threshold":
            confidence *= 0.95
        tokens = [w.text for w in inner]
        has_colon = any(t.endswith(":") and len(t) > 1 for t in tokens)
        kind = region.kind
        if kind == RegionKind.CHAT and region.confidence < 0.8 and not has_colon:
            # Ambiguous large block: let the parser treat it as a TAB-style row unless it is
            # clearly a chat system message.
            kind = RegionKind.CHAT if (tokens[-1].lower() in _SYSTEM_WORDS or tokens[0].startswith("[") or tokens[0].startswith("@")) else RegionKind.UNKNOWN
        return RawRow(
            text=text,
            region=kind,
            bbox=full_bbox,
            leading_icon=leading,
            trailing_icon=trailing,
            confidence=round(min(1.0, confidence), 3),
            username_cut_off=cut_off,
            has_colon_after_name=has_colon if kind != RegionKind.CHAT else None,
            extractor_notes=f"variant={variant_name}",
        )


class EasyOcrExtractor(TesseractExtractor):
    """EasyOCR backend behind the same interface (only if the package is installed)."""

    def __init__(self, settings: ExtractionSettings, region_detector: Any | None = None) -> None:  # noqa: D401
        try:
            import easyocr  # type: ignore
        except ImportError as exc:
            raise ExtractorUnavailable("easyocr is not installed") from exc
        self.settings = settings
        self.region_detector = region_detector or detect_regions
        self._variant_names = list(settings.extra.get("ocr_variants") or ["maxchannel_inverted"])
        self._reader = easyocr.Reader(["en"], gpu=bool(settings.extra.get("gpu", False)))
        self._version = "easyocr"

    def _read_crop(self, img: Image.Image, bbox: BBox, region: DetectedRegion, output: ExtractionOutput):  # type: ignore[override]
        crop, mapping = crop_and_upscale(img, bbox, 1.0, None)
        bgr = pil_to_bgr(crop)
        factor = float(max(1, self.settings.upscale_factor))
        variant = variants(bgr, factor=factor)[0]
        results = self._reader.readtext(cv2.cvtColor(variant.image, cv2.COLOR_GRAY2BGR), detail=1, paragraph=False)
        output.cost["ocr_passes"] = output.cost.get("ocr_passes", 0) + 1
        words: list[Word] = []
        for i, (box, text, conf) in enumerate(results):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x, y, w, h = int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))
            for tok in str(text).split():
                words.append(Word(tok, x, y, w, h, float(conf) * 100, (0, 0, i)))
        lines = _group_lines(words)
        ink = variant.image < 128
        rows: list[RawRow] = []
        texts: list[str] = []
        word_dicts: list[dict[str, Any]] = []
        for line in lines:
            texts.append(" ".join(w.text for w in line.words))
            segments = _split_tab_segments(line) if region.kind in (RegionKind.TAB, RegionKind.FULL, RegionKind.UNKNOWN) else [line]
            for seg in segments:
                row = self._row_from_line(seg, ink, mapping, variant.scale, region, img.width, variant.name)
                if row is not None:
                    rows.append(row)
        return rows, texts, word_dicts, "\n".join(texts)


# ------------------------------------------------------------------- helpers
def _score(data: dict[str, list]) -> float:
    texts = data.get("text", [])
    confs: list[float] = []
    for i, c in enumerate(data.get("conf", [])):
        try:
            value = float(c)
        except (TypeError, ValueError):
            continue
        if value < 0 or not str(texts[i]).strip():
            continue
        confs.append(value)
    if not confs:
        return 0.0
    good = [c for c in confs if c >= 50]
    return len(good) * 1.0 + (sum(confs) / len(confs)) / 100.0


def _words_from_data(data: dict[str, list]) -> list[Word]:
    words: list[Word] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = str(data["text"][i]).strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        words.append(
            Word(
                text=text,
                x=int(data["left"][i]),
                y=int(data["top"][i]),
                w=int(data["width"][i]),
                h=int(data["height"][i]),
                conf=conf,
                line_key=(int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i])),
            )
        )
    return words


def _group_lines(words: list[Word]) -> list[Line]:
    """Group by Tesseract's own line ids, then re-split lines whose words are vertically apart."""
    by_key: dict[tuple[int, int, int], list[Word]] = {}
    for w in words:
        by_key.setdefault(w.line_key, []).append(w)
    lines: list[Line] = []
    for key in sorted(by_key):
        ws = sorted(by_key[key], key=lambda w: w.x)
        current: list[Word] = []
        for w in ws:
            if current:
                ref_h = max(float(np.median([q.h for q in current])), 1.0)
                prev_cy = float(np.median([q.y + q.h / 2 for q in current]))
                if abs((w.y + w.h / 2) - prev_cy) > 0.6 * ref_h:
                    lines.append(Line(current))
                    current = []
            current.append(w)
        if current:
            lines.append(Line(current))
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    return lines


def _split_tab_segments(line: Line) -> list[Line]:
    """A TAB line read across several columns is split after each standalone ping number."""
    segments: list[Line] = []
    current: list[Word] = []
    words = line.words
    for i, w in enumerate(words):
        current.append(w)
        is_ping = _NUMERIC_RE.match(w.text) is not None and i > 0 and i < len(words) - 1
        if is_ping:
            # the word after a ping number starts a new column row
            segments.append(Line(current))
            current = []
    if current:
        segments.append(Line(current))
    out: list[Line] = []
    for seg in segments:
        ws = [w for w in seg.words]
        # drop a trailing ping number from the segment text (the parser also tolerates it)
        if len(ws) > 1 and _NUMERIC_RE.match(ws[-1].text):
            ws = ws[:-1]
        if ws:
            out.append(Line(ws))
    return out or [line]


def _is_icon_token(text: str) -> bool:
    """Tokens Tesseract produces for the diamond/infinity/yin-yang glyphs."""
    if _ICON_TOKEN_RE.match(text) is not None:
        return True
    if len(text) == 1:
        return True
    if len(text) <= 2 and any(not ch.isalnum() for ch in text):
        return True
    return len(text) <= 2 and all(ch in _ICONISH_LETTERS for ch in text)


def _is_junk_token(text: str) -> bool:
    """Head-icon debris at the start of a TAB row ("=o", "B°.0", "MZ,")."""
    if len(text) <= 2:
        return True
    if len(text) <= 4 and any((not ch.isalnum()) and ch not in "_." for ch in text):
        return True
    return False


def _strip_punct(word: Word) -> Word:
    text = word.text
    keep_colon = text.endswith(":") and len(text) > 1
    stripped = text.strip(_TRAILING_PUNCT)
    if keep_colon and not stripped.endswith(":"):
        stripped = stripped.rstrip(":") + ":"
    if stripped == text:
        return word
    return Word(stripped, word.x, word.y, word.w, word.h, word.conf, word.line_key)


def _to_full(mapping: CropMapping, scale: float, bbox: BBox) -> BBox:
    x, y, w, h = bbox
    crop_bbox = (int(x / scale), int(y / scale), max(1, int(w / scale)), max(1, int(h / scale)))
    return mapping.to_full(crop_bbox)


@dataclass(slots=True)
class BlobShape:
    aspect: float
    holes: int
    extent: float
    solidity: float
    rel_height: float


def _blob_shape(ink: np.ndarray, x: int, y: int, w: int, h: int, line_h: float) -> BlobShape | None:
    H, W = ink.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    patch = ink[y0:y1, x0:x1].astype(np.uint8) * 255
    contours, hierarchy = cv2.findContours(patch, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or hierarchy is None:
        return None
    # largest outer contour
    outer = [(i, c) for i, c in enumerate(contours) if hierarchy[0][i][3] == -1]
    if not outer:
        return None
    idx, contour = max(outer, key=lambda ic: cv2.contourArea(ic[1]))
    bx, by, bw, bh = cv2.boundingRect(contour)
    area = cv2.contourArea(contour)
    if bw < 2 or bh < 2 or area <= 0:
        return None
    hull = cv2.convexHull(contour)
    hull_area = max(cv2.contourArea(hull), 1.0)
    holes = sum(1 for i in range(len(contours)) if hierarchy[0][i][3] == idx and cv2.contourArea(contours[i]) >= area * 0.03)
    return BlobShape(aspect=bw / bh, holes=holes, extent=area / (bw * bh), solidity=area / hull_area, rel_height=bh / max(line_h, 1.0))


def _shape_to_icons(shape: BlobShape | None) -> tuple[LeadingIcon, TrailingIcon]:
    if shape is None or shape.rel_height < 0.3 or shape.rel_height > 1.6:
        return LeadingIcon.NONE, TrailingIcon.NONE
    if shape.aspect >= 1.35 and (shape.holes >= 1 or shape.extent < 0.6):
        return LeadingIcon.INFINITY, TrailingIcon.INFINITY
    if 0.7 <= shape.aspect <= 1.35:
        if shape.holes >= 1 or (shape.solidity >= 0.85 and shape.extent >= 0.62):
            return LeadingIcon.YINYANG, TrailingIcon.YINYANG
        if 0.3 <= shape.extent <= 0.66 and shape.rel_height <= 0.9:
            return LeadingIcon.DIAMOND, TrailingIcon.UNKNOWN
    return LeadingIcon.OTHER, TrailingIcon.UNKNOWN


def _classify_leading(ink: np.ndarray, token: Word, line_h: float) -> LeadingIcon:
    leading, _ = _shape_to_icons(_blob_shape(ink, token.x, token.y, token.w, token.h, line_h))
    if leading == LeadingIcon.NONE:
        return LeadingIcon.OTHER
    return leading


def _classify_trailing(ink: np.ndarray, token: Word, line_h: float) -> TrailingIcon:
    _, trailing = _shape_to_icons(_blob_shape(ink, token.x, token.y, token.w, token.h, line_h))
    return trailing


def _probe_icon(ink: np.ndarray, word: Word, line_h: float, *, side: str) -> tuple[LeadingIcon, TrailingIcon]:
    """Look for an icon blob Tesseract did not report, left or right of ``word``."""
    H, W = ink.shape[:2]
    span = int(2.2 * line_h)
    pad = int(0.25 * line_h)
    if side == "right":
        x0, x1 = word.x + word.w + 2, min(W, word.x + word.w + span)
    else:
        x0, x1 = max(0, word.x - span), max(0, word.x - 2)
    y0, y1 = max(0, int(word.y - pad)), min(H, int(word.y + word.h + pad))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return LeadingIcon.NONE, TrailingIcon.NONE
    strip = ink[y0:y1, x0:x1].astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(strip, connectivity=8)
    candidates = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if 0.35 * line_h <= h <= 1.4 * line_h and area >= 4 and w <= 2.2 * line_h:
            candidates.append((x, y, w, h))
    if not candidates:
        return LeadingIcon.NONE, TrailingIcon.NONE
    # nearest blob to the word
    cx, cy, cw, ch = min(candidates, key=lambda c: c[0]) if side == "right" else max(candidates, key=lambda c: c[0] + c[2])
    shape = _blob_shape(ink, x0 + cx, y0 + cy, cw, ch, line_h)
    return _shape_to_icons(shape)
