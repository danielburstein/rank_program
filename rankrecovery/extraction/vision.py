"""Vision-model extractor backed by the Anthropic API.

Large screenshots are never sent whole: the image is split into HUD regions
(TAB list columns, chat, nametags, scoreboard), each crop is upscaled with
nearest-neighbour sampling and sent separately with a strict JSON schema.
Every raw response is kept verbatim for auditing.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from rankrecovery.core.enums import ExtractorType, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.parsing import BBox, RawRow
from rankrecovery.extraction.base import (
    DetectedRegion,
    ExtractionOutput,
    ExtractionSettings,
    ExtractorUnavailable,
)
from rankrecovery.imaging.decode import DecodeError, decode_image, to_png_bytes
from rankrecovery.imaging.regions import (
    CropMapping,
    crop_and_upscale,
    detect_regions,
    downscale_to,
    fallback_full_image,
)

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"
LAYOUT_MAX_EDGE = 1568

FORMAT_DESCRIPTION = """You are reading screenshots (sometimes phone photos of a monitor) from a Minecraft Java server.
Player rows have the shape: [optional cosmetic swirl] [icon] RANK [icon] USERNAME [optional ping number].
Rules of this server's format:
- Ordinary ranks are written between two small diamond icons, e.g. "◇ Titan ◇ AndrewIsFire". Rank words you may see include Member, Default, Nova, Hero, Legend, Titan, God, Solar, Immortal, Immortal+, Cosmic, Galactic, Infinite, Scuffed.
- INFINITE rows use the infinity symbol ∞ instead of the diamonds: "∞ Infinite ∞ Name".
- SCUFFED rows and custom prefixes use a round yin-yang icon ☯ instead of the diamonds: "☯ Scuffed ☯ Name", "☯ ms kitten ☯ Name", "☯ Ouroboros ☯ Name". Report the custom prefix text exactly as written.
- Staff rows show a staff prefix (Trainee, Helper, Mod, Sr Mod, Head Mod, Admin, Admin+, Co Owner, Owner, Dev) before the name. A staff row may show an icon AFTER the username: a yin-yang ☯ after the name means the staff member also has SCUFFED, an infinity ∞ after the name means INFINITE. This is the only place an icon follows the username. Report it in trailing_icon; report "none" when nothing follows the name.
- A small white swirl/comet icon at the far left of some rows is cosmetic. It is NOT the yin-yang icon. Ignore it.
- Player head icons at the left of TAB rows and the coloured ping numbers at the right are not text. Ignore them.
- Usernames beginning with a dot (e.g. ".Axeliuz10") are real Bedrock usernames; keep the dot.
- In chat, only a line of the form "<prefix> <Name>: message" carries a rank. Join messages "[+] Name", mentions "@Name", holograms and system messages such as "Name died" or "Name won ..." carry NO rank: report them with has_colon_after_name=false and an empty prefix so they are treated as context only.
- The scoreboard on the right may repeat the viewing player's prefix and name: report it with region "scoreboard".
- Never guess. If a prefix or name cannot be read, set the *_unreadable flag and leave the text empty. If a username is cut off at the image edge, report the visible part and set username_cut_off=true; never complete it.
- One entry per player row. Do not merge a prefix from one row with a name from another row."""

ROWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_prefix_text": {"type": "string"},
                    "prefix_unreadable": {"type": "boolean"},
                    "username": {"type": "string"},
                    "username_unreadable": {"type": "boolean"},
                    "username_cut_off": {"type": "boolean"},
                    "prefix_colour": {"type": "string"},
                    "leading_icon": {"type": "string", "enum": ["diamond", "infinity", "yinyang", "none", "other"]},
                    "trailing_icon": {"type": "string", "enum": ["none", "yinyang", "infinity", "unknown"]},
                    "region": {"type": "string", "enum": ["tab", "chat", "nametag", "scoreboard", "unknown"]},
                    "bbox": {"type": "array", "items": {"type": "number"}},
                    "has_colon_after_name": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "raw_prefix_text",
                    "prefix_unreadable",
                    "username",
                    "username_unreadable",
                    "username_cut_off",
                    "prefix_colour",
                    "leading_icon",
                    "trailing_icon",
                    "region",
                    "bbox",
                    "has_colon_after_name",
                    "confidence",
                    "notes",
                ],
                "additionalProperties": False,
            },
        },
        "all_text": {"type": "string"},
    },
    "required": ["rows", "all_text"],
    "additionalProperties": False,
}

LAYOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["tab", "chat", "nametag", "scoreboard"]},
                    "bbox": {"type": "array", "items": {"type": "number"}},
                    "columns": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "number"}},
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "bbox", "columns", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["regions"],
    "additionalProperties": False,
}

LAYOUT_PROMPT = """Locate the Minecraft HUD elements in this screenshot. Return bounding boxes as [x0, y0, x1, y1] on a 0-1000 scale of the image width and height.
Report: the TAB player list (the translucent box with rows of player names; include one "columns" box per column of names, tight around the text of that column, in reading order), chat lines at the bottom-left (one box around the whole chat block), each floating nametag above a player (one box per nametag), and the scoreboard sidebar at the right if present. Omit elements that are not present. Do not include debug text, compass bars, item bars or music players."""

ROWS_PROMPT = """Read every player row in this crop of the {region} area of a Minecraft screenshot and return strict JSON.
Return bbox as [x0, y0, x1, y1] on a 0-1000 scale of this crop's width and height, tight around the prefix+username text of that row.
In all_text, transcribe every piece of readable text in the crop in reading order (used for search indexing)."""


@dataclass(slots=True)
class _Call:
    kind: str
    region: RegionKind
    mapping: CropMapping | None
    crop_size: tuple[int, int]


class AnthropicVisionExtractor:
    """TextExtractor implementation that calls the Anthropic Messages API."""

    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        settings: ExtractionSettings,
        api_key: str | None,
        client: Any | None = None,
        region_detector: Any | None = None,
    ) -> None:
        self.settings = settings
        self.region_detector = region_detector or detect_regions
        if client is None:
            if not api_key:
                raise ExtractorUnavailable("no Anthropic API key configured")
            import anthropic

            client = anthropic.Anthropic(
                api_key=api_key,
                timeout=float(settings.extra.get("timeout", 120)),
                max_retries=int(settings.extra.get("max_retries", 3)),
            )
        self.client = client
        self._model = settings.model or "claude-opus-5-5"
        self._effort = settings.effort or "medium"
        self._max_tokens = int(settings.max_tokens or 16000)
        self._use_model_layout = bool(settings.extra.get("model_layout", True))
        # Server-side refusal fallback (beta): if the safety classifier declines a
        # request, the API re-runs it on a fallback model inside the same call.
        self._server_fallback = bool(settings.extra.get("server_fallback", True))

    # ------------------------------------------------------------------ public
    def extract(self, image_bytes: bytes, *, filename: str = "", regions: list[DetectedRegion] | None = None) -> ExtractionOutput:
        started = time.monotonic()
        output = ExtractionOutput(
            rows=[],
            regions=[],
            extractor=ExtractorType.VISION,
            engine="anthropic",
            model=self._model,
            version=f"prompt-v{PROMPT_VERSION}",
            raw_output="",
            full_text="",
            settings_fingerprint=self.settings.fingerprint(),
            cost={"api_calls": 0, "input_tokens": 0, "output_tokens": 0},
        )
        try:
            img = decode_image(image_bytes)
        except DecodeError as exc:
            output.error = f"decode: {exc}"
            return output
        raw_parts: list[str] = []
        texts: list[str] = []
        try:
            if regions is None:
                regions = self._regions(img, output, raw_parts)
            output.regions = regions
            for region in regions:
                crops = region.columns or [region.bbox]
                for crop_bbox in crops:
                    for tile in self._tiles(crop_bbox):
                        rows, text = self._read_crop(img, tile, region.kind, output, raw_parts)
                        output.rows.extend(rows)
                        if text:
                            texts.append(text)
        except _ApiFailure as exc:
            output.error = str(exc)
        except ExtractorUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("vision extraction failed")
            output.error = f"{type(exc).__name__}: {exc}"
        output.rows = _dedupe_rows(output.rows)
        output.raw_output = "\n".join(raw_parts)
        output.full_text = "\n".join(texts)
        output.words = [
            {"text": f"{r.prefix_text or ''} {r.username_text or ''}".strip(), "x": r.bbox[0], "y": r.bbox[1], "w": r.bbox[2], "h": r.bbox[3], "conf": r.confidence, "region": r.region.value}
            for r in output.rows
            if r.bbox
        ]
        output.cost["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return output

    # ---------------------------------------------------------------- regions
    def _regions(self, img: Image.Image, output: ExtractionOutput, raw_parts: list[str]) -> list[DetectedRegion]:
        heuristic: list[DetectedRegion] = []
        try:
            heuristic = self.region_detector(img)
        except Exception as exc:  # pragma: no cover - defensive
            output.warnings.append(f"heuristic region detection failed: {exc}")
        model_regions: list[DetectedRegion] = []
        if self._use_model_layout:
            model_regions = self._layout_call(img, output, raw_parts)
        if model_regions:
            regions = model_regions
            # Borrow heuristic TAB columns when the model gave a TAB without columns.
            for region in regions:
                if region.kind == RegionKind.TAB and not region.columns:
                    for h in heuristic:
                        if h.kind == RegionKind.TAB and h.columns and _iou(h.bbox, region.bbox) > 0.3:
                            region.columns = [c for c in h.columns if _iou(c, region.bbox) > 0]
            return regions
        if heuristic:
            return heuristic
        output.warnings.append("no regions detected; reading the full image")
        return [fallback_full_image(img)]

    def _layout_call(self, img: Image.Image, output: ExtractionOutput, raw_parts: list[str]) -> list[DetectedRegion]:
        small, scale = downscale_to(img, LAYOUT_MAX_EDGE)
        data = self._call(small, LAYOUT_PROMPT, LAYOUT_SCHEMA, output, raw_parts, kind="layout")
        if not data:
            return []
        regions: list[DetectedRegion] = []
        for item in data.get("regions", []):
            try:
                kind = RegionKind(item["kind"])
                bbox = _norm_to_px(item["bbox"], small.width, small.height)
                bbox = _scale_bbox(bbox, 1 / scale)
                columns = [_scale_bbox(_norm_to_px(c, small.width, small.height), 1 / scale) for c in item.get("columns", [])]
                columns = [c for c in columns if c[2] > 4 and c[3] > 4]
                regions.append(DetectedRegion(kind=kind, bbox=bbox, columns=columns if len(columns) > 1 else [], confidence=float(item.get("confidence", 0.8)), notes="model layout"))
            except (KeyError, ValueError, TypeError) as exc:
                output.warnings.append(f"layout item skipped: {exc}")
        return regions

    # ------------------------------------------------------------------- rows
    def _tiles(self, bbox: BBox) -> list[BBox]:
        """Split a very wide crop into overlapping tiles so text stays legible after upscaling."""
        x, y, w, h = bbox
        factor = max(1, self.settings.upscale_factor)
        max_w = max(200, int(self.settings.max_crop_edge / factor))
        if w <= max_w:
            return [bbox]
        tiles: list[BBox] = []
        overlap = int(max_w * 0.15)
        cursor = x
        while cursor < x + w:
            tw = min(max_w, x + w - cursor)
            tiles.append((cursor, y, tw, h))
            if cursor + tw >= x + w:
                break
            cursor += tw - overlap
        return tiles

    def _read_crop(self, img: Image.Image, bbox: BBox, kind: RegionKind, output: ExtractionOutput, raw_parts: list[str]) -> tuple[list[RawRow], str]:
        try:
            crop, mapping = crop_and_upscale(img, bbox, self.settings.upscale_factor, self.settings.max_crop_edge)
        except ValueError as exc:
            output.warnings.append(f"crop skipped: {exc}")
            return [], ""
        prompt = ROWS_PROMPT.format(region=kind.value)
        data = self._call(crop, prompt, ROWS_SCHEMA, output, raw_parts, kind=f"rows:{kind.value}")
        if not data:
            return [], ""
        rows: list[RawRow] = []
        for item in data.get("rows", []):
            try:
                rows.append(self._row_from_item(item, kind, crop.size, mapping))
            except (KeyError, ValueError, TypeError) as exc:
                output.warnings.append(f"row skipped: {exc}")
        return rows, str(data.get("all_text", ""))

    def _row_from_item(self, item: dict[str, Any], kind: RegionKind, crop_size: tuple[int, int], mapping: CropMapping) -> RawRow:
        cw, ch = crop_size
        bbox = mapping.to_full(_norm_to_px(item["bbox"], cw, ch))
        region = kind
        try:
            reported = RegionKind(item.get("region", kind.value))
            if kind in (RegionKind.FULL, RegionKind.UNKNOWN):
                region = reported
        except ValueError:
            pass
        prefix = str(item.get("raw_prefix_text", "") or "")
        username = str(item.get("username", "") or "")
        text = " ".join(p for p in (prefix, username) if p)
        return RawRow(
            text=text,
            region=region,
            bbox=bbox,
            prefix_text=prefix,
            username_text=username,
            leading_icon=_enum_or(LeadingIcon, item.get("leading_icon"), LeadingIcon.NONE),
            trailing_icon=_enum_or(TrailingIcon, item.get("trailing_icon"), TrailingIcon.NONE),
            prefix_colour=str(item.get("prefix_colour", "") or "") or None,
            confidence=max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
            prefix_unreadable=bool(item.get("prefix_unreadable", False)),
            username_unreadable=bool(item.get("username_unreadable", False)),
            username_cut_off=bool(item.get("username_cut_off", False)),
            has_colon_after_name=bool(item.get("has_colon_after_name", False)),
            extractor_notes=str(item.get("notes", "") or ""),
        )

    # ------------------------------------------------------------------- api
    def _call(self, image: Image.Image, prompt: str, schema: dict[str, Any], output: ExtractionOutput, raw_parts: list[str], *, kind: str) -> dict[str, Any] | None:
        png = to_png_bytes(image)
        payload = base64.standard_b64encode(png).decode("ascii")
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": FORMAT_DESCRIPTION,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "output_config": {"format": {"type": "json_schema", "schema": schema}, "effort": self._effort},
        }
        try:
            import anthropic
        except ImportError:  # pragma: no cover
            anthropic = None  # type: ignore[assignment]
        try:
            beta_api = getattr(self.client, "beta", None) if self._server_fallback else None
            if beta_api is not None and hasattr(beta_api, "messages"):
                response = beta_api.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
            else:
                response = self.client.messages.create(**request)
        except Exception as exc:
            if anthropic is not None:
                if isinstance(exc, anthropic.AuthenticationError):
                    raise ExtractorUnavailable("Anthropic API key was rejected") from exc
                if isinstance(exc, anthropic.RateLimitError):
                    raise _ApiFailure("rate limited by the Anthropic API after retries") from exc
                if isinstance(exc, anthropic.APIStatusError):
                    raise _ApiFailure(f"Anthropic API error {exc.status_code}: {getattr(exc, 'message', exc)}") from exc
                if isinstance(exc, anthropic.APIConnectionError):
                    raise _ApiFailure(f"Anthropic API connection error: {exc}") from exc
            raise _ApiFailure(f"{type(exc).__name__}: {exc}") from exc
        output.cost["api_calls"] = output.cost.get("api_calls", 0) + 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            output.cost["input_tokens"] = output.cost.get("input_tokens", 0) + int(getattr(usage, "input_tokens", 0) or 0)
            output.cost["output_tokens"] = output.cost.get("output_tokens", 0) + int(getattr(usage, "output_tokens", 0) or 0)
        stop = getattr(response, "stop_reason", None)
        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", "") == "text":
                text += getattr(block, "text", "")
        raw_parts.append(json.dumps({"call": kind, "model": self._model, "stop_reason": stop, "response": text}))
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            output.warnings.append(f"{kind}: model refused ({getattr(details, 'category', None)})")
            return None
        if stop == "max_tokens":
            output.warnings.append(f"{kind}: response truncated at max_tokens")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            output.warnings.append(f"{kind}: invalid JSON from model: {exc}")
            return None
        if not isinstance(data, dict):
            output.warnings.append(f"{kind}: unexpected JSON shape")
            return None
        return data


class _ApiFailure(RuntimeError):
    pass


def _enum_or(enum_cls, value, default):
    try:
        return enum_cls(str(value))
    except (ValueError, TypeError):
        return default


def _norm_to_px(box: list[float], width: int, height: int) -> BBox:
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, x1 = sorted((max(0.0, min(1000.0, x0)), max(0.0, min(1000.0, x1))))
    y0, y1 = sorted((max(0.0, min(1000.0, y0)), max(0.0, min(1000.0, y1))))
    px0, py0 = int(round(x0 / 1000 * width)), int(round(y0 / 1000 * height))
    px1, py1 = int(round(x1 / 1000 * width)), int(round(y1 / 1000 * height))
    return (px0, py0, max(1, px1 - px0), max(1, py1 - py0))


def _scale_bbox(bbox: BBox, factor: float) -> BBox:
    x, y, w, h = bbox
    return (int(round(x * factor)), int(round(y * factor)), max(1, int(round(w * factor))), max(1, int(round(h * factor))))


def _iou(a: BBox, b: BBox) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _dedupe_rows(rows: list[RawRow]) -> list[RawRow]:
    """Overlapping tiles can report the same row twice; keep the more confident copy."""
    out: list[RawRow] = []
    for row in rows:
        key_name = (row.username_text or "").lower()
        duplicate = None
        for existing in out:
            if key_name and (existing.username_text or "").lower() == key_name and existing.region == row.region:
                if row.bbox and existing.bbox and _iou(row.bbox, existing.bbox) > 0.2:
                    duplicate = existing
                    break
        if duplicate is None:
            out.append(row)
        elif row.confidence > duplicate.confidence:
            out[out.index(duplicate)] = row
    return out
