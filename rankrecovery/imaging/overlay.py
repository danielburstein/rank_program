"""Draw extraction overlays (boxes, labels) for the GUI and API previews."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from rankrecovery.core.parsing import BBox

COLOURS = {
    "row": (90, 160, 255),
    "prefix": (255, 190, 40),
    "username": (80, 230, 120),
    "region": (200, 90, 255),
    "highlight": (255, 70, 70),
    "staff": (255, 120, 200),
}


@dataclass(slots=True)
class OverlayBox:
    bbox: BBox
    kind: str = "row"  # key of COLOURS
    label: str = ""


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def draw_overlay(img: Image.Image, boxes: list[OverlayBox], *, line_width: int | None = None) -> Image.Image:
    """Return a copy of ``img`` with boxes and labels drawn."""
    out = img.convert("RGBA")
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    width = line_width or max(2, out.width // 600)
    font = _font(max(12, out.width // 90))
    for box in boxes:
        x, y, w, h = box.bbox
        colour = COLOURS.get(box.kind, COLOURS["row"])
        draw.rectangle([x, y, x + w, y + h], outline=colour + (255,), width=width)
        if box.label:
            text = box.label[:60]
            tw = draw.textlength(text, font=font)
            th = font.size if hasattr(font, "size") else 12
            ty = y - th - 4 if y - th - 4 > 0 else y + h + 2
            draw.rectangle([x, ty, x + tw + 6, ty + th + 4], fill=colour + (200,))
            draw.text((x + 3, ty + 2), text, fill=(0, 0, 0, 255), font=font)
    return Image.alpha_composite(out, layer).convert("RGB")


def overlay_png(img: Image.Image, boxes: list[OverlayBox]) -> bytes:
    buf = io.BytesIO()
    draw_overlay(img, boxes).save(buf, format="PNG")
    return buf.getvalue()


def _field(d, *names, default=None):
    """Read a field from a ParsedDetection object or an API detection dict."""
    if isinstance(d, dict):
        for name in names:
            if name in d and d[name] is not None:
                return d[name]
        return default
    for name in names:
        if hasattr(d, name) and getattr(d, name) is not None:
            return getattr(d, name)
    return default


def boxes_for_detections(detections, *, highlight_username: str | None = None) -> list[OverlayBox]:
    """Build overlay boxes from ParsedDetection objects or detection dicts (API shape)."""
    boxes: list[OverlayBox] = []
    target = (highlight_username or "").lower()
    for d in detections:
        username = _field(d, "normalised_username", "username", default="") or ""
        rank = _field(d, "rank_observation", "rank")
        staff = _field(d, "staff_observation", "staff")
        trailing_rank = _field(d, "trailing_icon_rank")
        bbox = _field(d, "bbox")
        prefix_bbox = _field(d, "prefix_bbox")
        username_bbox = _field(d, "username_bbox")
        is_target = bool(target) and username.lower() == target
        label_parts = []
        if rank:
            label_parts.append(str(rank))
        if staff:
            label_parts.append(f"staff:{staff}")
        if trailing_rank:
            label_parts.append(f"+{trailing_rank}")
        label = f"{username or '?'} {' '.join(label_parts)}".strip()
        if bbox:
            boxes.append(OverlayBox(tuple(bbox), "highlight" if is_target else ("staff" if staff else "row"), label))
        if prefix_bbox:
            boxes.append(OverlayBox(tuple(prefix_bbox), "prefix", ""))
        if username_bbox:
            boxes.append(OverlayBox(tuple(username_bbox), "highlight" if is_target else "username", ""))
    return boxes
