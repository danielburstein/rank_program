"""Image decoding: formats, animated sources and basic facts.

Only one decoded full-resolution frame is held at a time when iterating
animated sources, so very large batches do not exhaust memory.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass

from PIL import Image, ImageFile, UnidentifiedImageError

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:  # AVIF support is optional; Pillow >= 11 may ship it natively as well.
    import pillow_avif  # noqa: F401

    AVIF_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    try:
        from PIL import features as _features

        AVIF_AVAILABLE = bool(_features.check("avif"))
    except Exception:
        AVIF_AVAILABLE = False

SUPPORTED_FORMATS: frozenset[str] = frozenset({"PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF", "AVIF"})
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".avif"})


class DecodeError(ValueError):
    """Raised when bytes cannot be decoded as a supported image."""


@dataclass(frozen=True, slots=True)
class ImageFacts:
    format: str
    width: int
    height: int
    mode: str
    frame_count: int
    animated: bool

    @property
    def supported(self) -> bool:
        return self.format in SUPPORTED_FORMATS


def _open(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
    except UnidentifiedImageError as exc:
        raise DecodeError(f"unrecognised image data: {exc}") from exc
    except Exception as exc:  # corrupted container
        raise DecodeError(f"cannot open image: {exc}") from exc
    fmt = (img.format or "").upper()
    if fmt == "AVIF" and not AVIF_AVAILABLE:
        raise DecodeError("AVIF images need the pillow-avif-plugin package")
    if fmt and fmt not in SUPPORTED_FORMATS:
        raise DecodeError(f"unsupported image format {fmt}")
    return img


def image_facts(data: bytes) -> ImageFacts:
    """Return format/size/frame information without decoding pixel data of every frame."""
    img = _open(data)
    try:
        frames = int(getattr(img, "n_frames", 1) or 1)
    except Exception:
        frames = 1
    return ImageFacts(
        format=(img.format or "").upper(),
        width=int(img.width),
        height=int(img.height),
        mode=str(img.mode),
        frame_count=frames,
        animated=frames > 1,
    )


def decode_image(data: bytes, *, frame: int = 0) -> Image.Image:
    """Decode one frame to an RGB :class:`PIL.Image.Image`."""
    img = _open(data)
    try:
        if frame and getattr(img, "n_frames", 1) > frame:
            img.seek(frame)
        rgb = img.convert("RGB")
        rgb.load()
    except Exception as exc:
        raise DecodeError(f"cannot decode image pixels: {exc}") from exc
    return rgb


def select_frame_indices(frame_count: int, max_frames: int) -> list[int]:
    """Evenly spaced frame indices (always includes the first frame)."""
    if frame_count <= 1 or max_frames <= 1:
        return [0]
    n = min(max_frames, frame_count)
    if n == frame_count:
        return list(range(frame_count))
    step = (frame_count - 1) / (n - 1)
    return sorted({int(round(i * step)) for i in range(n)})


def iter_frames(data: bytes, *, max_frames: int = 8) -> Iterator[tuple[int, Image.Image]]:
    """Yield ``(frame_index, rgb_image)`` for up to ``max_frames`` evenly spaced frames.

    Each frame is decoded lazily; the previous frame is released before the
    next one is produced.
    """
    facts = image_facts(data)
    for index in select_frame_indices(facts.frame_count, max_frames):
        yield index, decode_image(data, frame=index)


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
