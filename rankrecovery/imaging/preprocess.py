"""OCR preprocessing variants tuned for Minecraft pixel text.

Minecraft text is bright, saturated and often coloured on a dark translucent
background. The max-channel grayscale keeps coloured glyphs bright; CLAHE and
adaptive thresholding help with photos of monitors.
"""

from __future__ import annotations

from dataclasses import dataclass

try:  # optional, see regions.py
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
import numpy as np
from PIL import Image


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    image: np.ndarray  # grayscale uint8, black text on white (Tesseract-friendly)
    scale: float  # applied scale factor relative to the input


def pil_to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def upscale(arr: np.ndarray, factor: float, *, nearest_first: bool = True, smooth: bool = False) -> np.ndarray:
    """Nearest-neighbour upscale (keeps pixel edges crisp), optional cubic smoothing."""
    if factor <= 1.0:
        return arr
    h, w = arr.shape[:2]
    size = (int(round(w * factor)), int(round(h * factor)))
    out = cv2.resize(arr, size, interpolation=cv2.INTER_NEAREST if nearest_first else cv2.INTER_CUBIC)
    if smooth and nearest_first:
        out = cv2.resize(out, size, interpolation=cv2.INTER_CUBIC)
    return out


def max_channel_gray(bgr: np.ndarray) -> np.ndarray:
    """Brightest channel per pixel: coloured text stays bright, dark overlay stays dark."""
    return bgr.max(axis=2).astype(np.uint8)


def clahe(gray: np.ndarray, clip: float = 3.0, tile: int = 8) -> np.ndarray:
    return cv2.createCLAHE(clipLimit=clip, tileGridSize=(tile, tile)).apply(gray)


def sharpen(gray: np.ndarray, amount: float = 0.6) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
    return cv2.addWeighted(gray, 1 + amount, blur, -amount, 0)


def adaptive_threshold(gray: np.ndarray, block: int = 31, c: int = -10) -> np.ndarray:
    block = max(3, block | 1)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, c)


def otsu_threshold(gray: np.ndarray) -> np.ndarray:
    _, out = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return out


def saturation_mask(bgr: np.ndarray, min_sat: int = 60, min_val: int = 90) -> np.ndarray:
    """Mask of saturated, bright pixels (coloured prefixes/names)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 1] >= min_sat) & (hsv[:, :, 2] >= min_val)).astype(np.uint8) * 255


def text_mask(bgr: np.ndarray, min_val: int = 120) -> np.ndarray:
    """Bright pixels (white or coloured) on a dark background: candidate text."""
    gray = max_channel_gray(bgr)
    return (gray >= min_val).astype(np.uint8) * 255


def channel_isolate(bgr: np.ndarray, channel: str) -> np.ndarray:
    idx = {"b": 0, "g": 1, "r": 2}[channel]
    return bgr[:, :, idx]


def variants(bgr: np.ndarray, *, factor: float = 3.0, include_channels: bool = False) -> list[Variant]:
    """Produce named preprocessing variants, all black-on-white for Tesseract."""
    up = upscale(bgr, factor)
    gray = max_channel_gray(up)
    out: list[Variant] = []

    out.append(text_only(bgr, factor=factor))

    inverted = cv2.bitwise_not(gray)
    out.append(Variant("maxchannel_inverted", inverted, factor))

    enhanced = cv2.bitwise_not(sharpen(clahe(gray)))
    out.append(Variant("clahe_sharpen_inverted", enhanced, factor))

    adaptive = cv2.bitwise_not(adaptive_threshold(gray))
    out.append(Variant("adaptive_threshold", adaptive, factor))

    smooth_up = upscale(bgr, factor, nearest_first=True, smooth=True)
    smooth_gray = cv2.bitwise_not(otsu_threshold(max_channel_gray(smooth_up)))
    out.append(Variant("otsu_smooth", smooth_gray, factor))

    if include_channels:
        for ch in ("r", "g", "b"):
            out.append(Variant(f"channel_{ch}_inverted", cv2.bitwise_not(channel_isolate(up, ch)), factor))
    return out


def text_only(bgr: np.ndarray, *, factor: float = 3.0, min_val: int = 110) -> Variant:
    """Keep only components that belong to a horizontal text line; erase everything else.

    Stars, particles, blocks and player models around a nametag are removed,
    which makes Tesseract far more reliable on Minecraft HUD crops. Small
    marks (colons, dots) are kept when they sit inside a detected line band.
    """
    from rankrecovery.imaging.regions import (  # local import: regions imports this module
        Glyph,
        find_text_rows,
    )

    # Local-contrast mask at the native scale (a window several glyphs wide keeps strokes solid),
    # then nearest-neighbour upscale so pixel edges stay crisp.
    gray0 = max_channel_gray(bgr)
    h0, w0 = gray0.shape[:2]
    block = max(21, (int(min(h0, w0) * 0.25) // 2) * 2 + 1)
    local = cv2.adaptiveThreshold(gray0, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, -25)
    bright0 = ((local > 0) & (gray0 >= min_val)).astype(np.uint8) | (gray0 >= 200).astype(np.uint8)
    bright = upscale(bright0, factor)
    h, w = bright.shape[:2]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    blank = np.full((h, w), 255, np.uint8)
    if n <= 1:
        return Variant("text_only", blank, factor)
    min_h = max(3, int(3 * factor))
    max_h = max(min_h + 2, int(h * 0.6))
    glyphs: list[Glyph] = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if min_h <= ch <= max_h and cw <= 12 * ch and area >= 3 and area / (cw * ch) <= 0.9:
            glyphs.append(Glyph(x, y, cw, ch))
    runs = [r for r in find_text_rows(bright, glyphs=glyphs, min_glyphs=3) if r.density >= 0.3 and r.alignment <= 0.3]
    if not runs:
        return Variant("text_only", blank, factor)
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        x, y, cw, ch, _area = (int(v) for v in stats[i])
        cx, cy = x + cw / 2, y + ch / 2
        for r in runs:
            rh = max(r.height, 1)
            if ch > 1.6 * rh:
                continue
            if r.y0 - 0.35 * rh <= cy <= r.y1 + 0.35 * rh and r.x0 - 1.5 * rh <= cx <= r.x1 + 2.5 * rh:
                keep[i] = True
                break
    out = blank.copy()
    out[keep[labels]] = 0
    # Mild anti-aliasing: Tesseract's models expect smooth strokes, not pixel-font blocks.
    sigma = 0.45 * factor
    if sigma >= 0.6:
        out = cv2.GaussianBlur(out, (0, 0), sigma)
    return Variant("text_only", out, factor)
