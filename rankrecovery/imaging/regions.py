"""Heuristic detection of Minecraft HUD regions: TAB list, chat, nametags, scoreboard.

The detector works on bright "glyph" components: Minecraft text is bright on
a darker world or on the translucent TAB overlay. Rows are found with a
horizontal projection profile, grouped into blocks, and blocks are
classified by position and shape. TAB blocks are split into columns using a
vertical projection profile.

This is deliberately conservative: when nothing is confident the caller
falls back to the full image, and the vision extractor can additionally ask
the model for a layout.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from rankrecovery.core.enums import RegionKind
from rankrecovery.core.parsing import BBox
from rankrecovery.extraction.base import DetectedRegion
from rankrecovery.imaging.preprocess import max_channel_gray, pil_to_bgr


@dataclass(slots=True)
class TextRow:
    y0: int
    y1: int
    x0: int
    x1: int
    density: float
    glyphs: int = 0
    alignment: float = 0.0  # std of glyph centre-y / glyph height; small for real text

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def width(self) -> int:
        return self.x1 - self.x0


@dataclass(slots=True)
class TextBlock:
    rows: list[TextRow]

    @property
    def bbox(self) -> BBox:
        x0 = min(r.x0 for r in self.rows)
        y0 = min(r.y0 for r in self.rows)
        x1 = max(r.x1 for r in self.rows)
        y1 = max(r.y1 for r in self.rows)
        return (x0, y0, x1 - x0, y1 - y0)

    @property
    def row_height(self) -> float:
        return float(np.median([r.height for r in self.rows]))


@dataclass(slots=True)
class CropMapping:
    """Maps crop-space coordinates back to full-image pixels."""

    offset_x: int
    offset_y: int
    scale: float  # crop pixels per full-image pixel

    def to_full(self, bbox: BBox) -> BBox:
        x, y, w, h = bbox
        return (
            int(round(self.offset_x + x / self.scale)),
            int(round(self.offset_y + y / self.scale)),
            max(1, int(round(w / self.scale))),
            max(1, int(round(h / self.scale))),
        )


# --------------------------------------------------------------------------- glyphs
@dataclass(slots=True)
class Glyph:
    x: int
    y: int
    w: int
    h: int

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def glyph_components(bgr: np.ndarray, *, min_val: int = 150) -> tuple[np.ndarray, list[Glyph]]:
    """Return (mask, glyphs): small bright components that look like text glyphs."""
    h, w = bgr.shape[:2]
    gray = max_channel_gray(bgr)
    # Local contrast: text is brighter than its surroundings (works on photos of monitors
    # where absolute brightness is low), combined with a floor on absolute brightness.
    block = max(15, (int(min(h, w) * 0.04) // 2) * 2 + 1)
    local = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, -28)
    bright = ((local > 0) & (gray >= min(min_val, 110))).astype(np.uint8)
    bright |= (gray >= max(min_val, 200)).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    min_h = max(4, int(h * 0.004))
    max_h = max(min_h + 4, int(h * 0.06))
    keep = np.zeros(n, dtype=bool)
    glyphs: list[Glyph] = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if not (min_h <= ch <= max_h) or cw > 12 * ch or area < 3:
            continue
        fill = area / float(cw * ch)
        if fill > 0.85 and ch > min_h + 2:  # solid bright block, not a glyph
            continue
        keep[i] = True
        glyphs.append(Glyph(x, y, cw, ch))
    return keep[labels].astype(np.uint8) * 255, glyphs


def glyph_mask(bgr: np.ndarray, *, min_val: int = 150) -> np.ndarray:
    return glyph_components(bgr, min_val=min_val)[0]


# ----------------------------------------------------------------------------- rows
def find_text_rows(mask: np.ndarray, *, glyphs: list[Glyph] | None = None, min_glyphs: int = 4) -> list[TextRow]:
    """Cluster glyphs into horizontal text runs.

    A run is a set of glyphs that overlap vertically, have similar heights and
    are separated by gaps no larger than a few glyph heights. Random bright
    specks (stars, particles) rarely satisfy all three conditions.
    """
    if glyphs is None:
        _, glyphs = _glyphs_from_mask(mask)
    if not glyphs:
        return []
    glyphs = sorted(glyphs, key=lambda g: (g.cy, g.x))
    lines: list[list[Glyph]] = []
    for g in glyphs:
        placed = False
        for line in lines[-25:]:  # only recent lines can still match (sorted by y)
            ref_h = float(np.median([q.h for q in line]))
            y0 = float(np.median([q.y for q in line]))
            y1 = y0 + ref_h
            overlap = min(y1, g.y + g.h) - max(y0, g.y)
            if overlap >= 0.5 * min(ref_h, g.h) and 0.45 <= g.h / max(ref_h, 1) <= 2.2:
                line.append(g)
                placed = True
                break
        if not placed:
            lines.append([g])
    rows: list[TextRow] = []
    for line in lines:
        line.sort(key=lambda g: g.x)
        ref_h = float(np.median([q.h for q in line]))
        max_gap = 3.0 * max(ref_h, 4)
        run: list[Glyph] = []
        for g in line:
            if run and g.x - (run[-1].x + run[-1].w) > max_gap:
                rows.extend(_run_to_row(run, min_glyphs))
                run = []
            run.append(g)
        rows.extend(_run_to_row(run, min_glyphs))
    return sorted(rows, key=lambda r: (r.y0, r.x0))


def _run_to_row(run: list[Glyph], min_glyphs: int) -> list[TextRow]:
    if len(run) < min_glyphs:
        return []
    heights = np.array([g.h for g in run], dtype=float)
    if heights.std() / max(heights.mean(), 1) > 0.6:
        return []
    centres = np.array([g.cy for g in run], dtype=float)
    alignment = float(centres.std() / max(np.median(heights), 1.0))
    if alignment > 0.4:  # glyphs scattered vertically: particles, not a text line
        return []
    x0 = min(g.x for g in run)
    x1 = max(g.x + g.w for g in run)
    y0 = min(g.y for g in run)
    y1 = max(g.y + g.h for g in run)
    coverage = sum(g.w for g in run) / max(x1 - x0, 1)
    return [TextRow(y0, y1, x0, x1, float(coverage), len(run), alignment)]


def _glyphs_from_mask(mask: np.ndarray) -> tuple[np.ndarray, list[Glyph]]:
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    glyphs = [Glyph(int(stats[i][0]), int(stats[i][1]), int(stats[i][2]), int(stats[i][3])) for i in range(1, n)]
    return mask, glyphs


def group_rows(rows: list[TextRow], *, gap_factor: float = 1.8, horizontal_factor: float = 4.0) -> list[TextBlock]:
    """Group runs into blocks by vertical adjacency and horizontal proximity."""
    blocks: list[TextBlock] = []
    for r in sorted(rows, key=lambda r: (r.y0, r.x0)):
        best: TextBlock | None = None
        for block in blocks:
            bx, by, bw, bh = block.bbox
            ref = max(min(block.row_height, r.height), 4)
            vertical_gap = r.y0 - (by + bh)
            if vertical_gap > gap_factor * ref:
                continue
            if r.y1 < by - gap_factor * ref:
                continue
            size_ok = 0.4 <= r.height / max(block.row_height, 1) <= 2.5
            horizontal_gap = max(bx - r.x1, r.x0 - (bx + bw))
            if size_ok and horizontal_gap <= horizontal_factor * ref:
                best = block
                break
        if best is None:
            blocks.append(TextBlock([r]))
        else:
            best.rows.append(r)
    return blocks


# ------------------------------------------------------------------------- columns
def split_columns(mask: np.ndarray, bbox: BBox, *, min_gap_ratio: float = 0.012) -> list[BBox]:
    x, y, w, h = bbox
    sub = mask[y : y + h, x : x + w]
    if sub.size == 0:
        return [bbox]
    profile = (sub > 0).sum(axis=0)
    gap_min = max(6, int(w * min_gap_ratio))
    # treat columns with near-zero density as gaps
    is_gap = profile <= max(1, int(h * 0.01))
    gaps: list[tuple[int, int]] = []
    start = None
    for i, g in enumerate(list(is_gap) + [False]):
        if g and start is None:
            start = i
        elif not g and start is not None:
            if i - start >= gap_min:
                gaps.append((start, i))
            start = None
    # ignore leading/trailing gaps
    gaps = [(a, b) for a, b in gaps if a > 0 and b < w]
    if not gaps:
        return [bbox]
    cols: list[BBox] = []
    cursor = 0
    for a, b in gaps:
        if a - cursor >= max(20, int(w * 0.08)):
            cols.append((x + cursor, y, a - cursor, h))
        cursor = b
    if w - cursor >= max(20, int(w * 0.08)):
        cols.append((x + cursor, y, w - cursor, h))
    # A TAB list column must contain text rows on its own; keep at most 6 columns.
    return cols[:6] if cols else [bbox]


def split_columns_from_rows(block: TextBlock, *, min_gap_ratio: float = 0.012, min_rows_per_column: int = 3) -> list[BBox]:
    """Split a block into columns using the x-intervals of its text runs.

    An x-range covered by almost no runs is a column gap. Headers that span
    the gap (e.g. the server name above a TAB list) are tolerated because the
    coverage threshold scales with the number of distinct rows.
    """
    x, y, w, h = block.bbox
    if w <= 0:
        return [block.bbox]
    coverage = np.zeros(w + 1, dtype=np.int32)
    for r in block.rows:
        coverage[max(0, r.x0 - x) : max(0, min(w, r.x1 - x))] += 1
    distinct = _distinct_rows(block)
    tolerance = max(1, int(distinct * 0.15))
    min_gap = max(6, int(w * min_gap_ratio))
    is_gap = coverage <= tolerance
    gaps: list[tuple[int, int]] = []
    start: int | None = None
    for i, g in enumerate(list(is_gap) + [False]):
        if g and start is None:
            start = i
        elif not g and start is not None:
            if i - start >= min_gap and start > 0 and i < w:
                gaps.append((start, i))
            start = None
    if not gaps:
        return [block.bbox]
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for a, b in gaps:
        bounds.append((cursor, a))
        cursor = b
    bounds.append((cursor, w))
    cols: list[BBox] = []
    for a, b in bounds:
        runs = [r for r in block.rows if r.x0 - x < b and r.x1 - x > a]
        if len(runs) >= min_rows_per_column and b - a >= max(20, int(w * 0.05)):
            cols.append((x + a, y, b - a, h))
    return cols if len(cols) >= 2 else [block.bbox]


# -------------------------------------------------------------------------- classify
def _distinct_rows(block: TextBlock) -> int:
    """Number of distinct text lines (runs on the same baseline count once)."""
    ys = sorted(r.y0 for r in block.rows)
    ref = max(block.row_height, 4)
    count, last = 0, -10_000
    for y in ys:
        if y - last > ref * 0.5:
            count += 1
            last = y
    return count


def _pitch_regularity(block: TextBlock) -> float:
    """Coefficient of variation of the spacing between distinct rows (0 = perfectly even)."""
    ys = sorted(r.y0 for r in block.rows)
    ref = max(block.row_height, 4)
    distinct: list[int] = []
    for y in ys:
        if not distinct or y - distinct[-1] > ref * 0.5:
            distinct.append(y)
    if len(distinct) < 3:
        return 0.0
    pitch = np.diff(np.array(distinct, dtype=float))
    return float(pitch.std() / max(pitch.mean(), 1.0))


def _classify(block: TextBlock, img_w: int, img_h: int) -> tuple[RegionKind, float]:
    x, y, w, h = block.bbox
    n = _distinct_rows(block)
    cx = (x + w / 2) / img_w
    cy = (y + h / 2) / img_h
    left_margin = x / img_w
    right_margin = (img_w - (x + w)) / img_w
    widths = np.array([r.width for r in block.rows], dtype=float)
    width_cv = float(widths.std() / widths.mean()) if widths.mean() > 0 else 0.0

    # Text quality: real text lines are well aligned and much wider than tall.
    alignment = float(np.median([r.alignment for r in block.rows]))
    elongation = float(np.median([r.width / max(r.height, 1) for r in block.rows]))
    if alignment > 0.24 or elongation < 5.0:
        return RegionKind.UNKNOWN, 0.1

    tab_like = n >= 3 and w >= img_w * 0.22 and 0.25 <= cx <= 0.75 and 0.12 <= cy <= 0.7
    boxed = right_margin >= 0.04 and (left_margin >= 0.03 or n >= 6)
    multi_column = n >= 6 and len(split_columns_from_rows(block)) >= 2
    regular = _pitch_regularity(block) <= 0.45
    if tab_like and regular and (boxed or multi_column):
        conf = 0.6 + min(0.3, n * 0.03) + (0.05 if w >= img_w * 0.4 else 0.0)
        return RegionKind.TAB, min(0.95, conf)
    # Chat: hugs the left edge; lower half, or fills the frame (zoomed photo of chat).
    if left_margin <= 0.08 and (cy >= 0.5 or (h >= img_h * 0.4 and width_cv >= 0.25)):
        conf = 0.6 + (0.2 if cy >= 0.6 else 0.0) + min(0.15, n * 0.03)
        return RegionKind.CHAT, min(0.95, conf)
    # Scoreboard: right-edge block of several short rows in the middle band.
    if x >= img_w * 0.6 and x + w >= img_w * 0.85 and n >= 3 and 0.1 <= cy <= 0.9 and w <= img_w * 0.35:
        return RegionKind.SCOREBOARD, 0.7
    # Nametag: a short isolated cluster away from the very top/bottom.
    if n <= 3 and w <= img_w * 0.5 and 0.05 <= cx <= 0.95 and 0.08 <= cy <= 0.92:
        return RegionKind.NAMETAG, 0.5 + (0.2 if n == 1 else 0.0)
    return RegionKind.UNKNOWN, 0.2


def _pad(bbox: BBox, img_w: int, img_h: int, pad_x: int, pad_y: int) -> BBox:
    x, y, w, h = bbox
    nx, ny = max(0, x - pad_x), max(0, y - pad_y)
    nw, nh = min(img_w, x + w + pad_x) - nx, min(img_h, y + h + pad_y) - ny
    return (nx, ny, nw, nh)


def detect_regions(img: Image.Image, *, min_val: int = 150, max_nametags: int = 4) -> list[DetectedRegion]:
    """Detect HUD regions heuristically. Returns an empty list when nothing is found."""
    bgr = pil_to_bgr(img)
    img_h, img_w = bgr.shape[:2]
    mask, glyphs = glyph_components(bgr, min_val=min_val)
    rows = find_text_rows(mask, glyphs=glyphs)
    if not rows:
        return []
    blocks = group_rows(rows)
    regions: list[DetectedRegion] = []
    for block in blocks:
        kind, conf = _classify(block, img_w, img_h)
        if kind == RegionKind.UNKNOWN:
            # Salvage clearly text-like runs (e.g. a nametag inside a particle field).
            for run in block.rows:
                if run.glyphs >= 8 and run.alignment <= 0.2 and run.density >= 0.45 and run.width <= img_w * 0.5:
                    rh = max(4, run.height)
                    bbox = _pad((run.x0, run.y0, run.width, run.height), img_w, img_h, pad_x=rh * 2, pad_y=rh // 2 + 2)
                    regions.append(DetectedRegion(kind=RegionKind.NAMETAG, bbox=bbox, columns=[], confidence=0.4, notes=f"1 rows, {run.glyphs} glyphs"))
            continue
        rh = max(4, int(block.row_height))
        bbox = _pad(block.bbox, img_w, img_h, pad_x=rh * 2, pad_y=rh // 2 + 2)
        columns: list[BBox] = []
        if kind == RegionKind.TAB:
            columns = [_pad(c, img_w, img_h, pad_x=rh, pad_y=rh // 2 + 2) for c in split_columns_from_rows(block)]
            if len(columns) <= 1:
                columns = []
        regions.append(DetectedRegion(kind=kind, bbox=bbox, columns=columns, confidence=conf, notes=f"{_distinct_rows(block)} rows, {sum(r.glyphs for r in block.rows)} glyphs"))
    # Keep the strongest TAB only (a HUD has one TAB list); other kinds may repeat.
    tabs = sorted((r for r in regions if r.kind == RegionKind.TAB), key=lambda r: (-r.confidence, -r.bbox[2] * r.bbox[3]))
    others = [r for r in regions if r.kind not in (RegionKind.TAB, RegionKind.NAMETAG)]
    nametags = sorted((r for r in regions if r.kind == RegionKind.NAMETAG), key=lambda r: -_glyphs_in_notes(r))
    others.extend(nametags[:max_nametags])
    if tabs:
        best = tabs[0]
        # demote the remaining TAB candidates to unknown-ish nametag candidates only if small
        for extra in tabs[1:]:
            if len(extra.columns) == 0 and extra.bbox[2] <= img_w * 0.5:
                others.append(DetectedRegion(RegionKind.NAMETAG, extra.bbox, [], 0.4, extra.notes))
        regions = [best, *others]
    else:
        regions = others
    return sorted(regions, key=lambda r: (r.bbox[1], r.bbox[0]))


def _glyphs_in_notes(region: DetectedRegion) -> int:
    try:
        return int(region.notes.split(",")[1].split()[0])
    except Exception:
        return 0


def fallback_full_image(img: Image.Image) -> DetectedRegion:
    return DetectedRegion(kind=RegionKind.FULL, bbox=(0, 0, img.width, img.height), confidence=0.0, notes="fallback")


def crop_and_upscale(img: Image.Image, bbox: BBox, factor: float, max_edge: int | None = None) -> tuple[Image.Image, CropMapping]:
    """Crop ``bbox`` and upscale with nearest-neighbour; never exceed ``max_edge`` on the longer side."""
    x, y, w, h = bbox
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(img.width, x + w), min(img.height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"empty crop {bbox}")
    crop = img.crop((x0, y0, x1, y1))
    scale = float(factor) if factor and factor > 0 else 1.0
    if max_edge:
        longest = max(crop.width, crop.height) * scale
        if longest > max_edge:
            scale = max_edge / max(crop.width, crop.height)
    if abs(scale - 1.0) > 1e-6:
        size = (max(1, int(round(crop.width * scale))), max(1, int(round(crop.height * scale))))
        resample = Image.Resampling.NEAREST if scale >= 1.0 else Image.Resampling.LANCZOS
        crop = crop.resize(size, resample)
        scale = crop.width / (x1 - x0)
    return crop, CropMapping(offset_x=x0, offset_y=y0, scale=scale)


def downscale_to(img: Image.Image, max_edge: int) -> tuple[Image.Image, float]:
    """Downscale so the longer side is at most ``max_edge``. Returns (image, scale)."""
    longest = max(img.width, img.height)
    if longest <= max_edge:
        return img, 1.0
    scale = max_edge / longest
    out = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)
    return out, out.width / img.width
