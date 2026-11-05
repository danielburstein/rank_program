"""Extract every piece of embedded metadata into a flat, namespaced dict.

Keys look like ``EXIF:DateTimeOriginal``, ``PNG:Software``,
``XMP:xmp:CreatorTool``, ``GIF:comment`` or ``FILE:name``. Values are strings
truncated to a sane length. Malformed metadata never raises; the problem is
recorded under ``METADATA:error`` instead.
"""

from __future__ import annotations

import io
import re
from typing import Any

from PIL import ExifTags, Image

MAX_VALUE_LEN = 2000

_XMP_PACKET_RE = re.compile(rb"<x:xmpmeta.*?</x:xmpmeta>", re.DOTALL)
_XMP_ELEMENT_RE = re.compile(r"<([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(\s[^>]*)?>([^<]{1,2000})</\1>", re.DOTALL)
_XMP_ATTR_RE = re.compile(r"\b([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)=\"([^\"]{1,2000})\"")
_XMP_LI_RE = re.compile(r"<([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)>\s*<rdf:(?:Seq|Bag|Alt)>(.*?)</rdf:(?:Seq|Bag|Alt)>\s*</\1>", re.DOTALL)
_LI_RE = re.compile(r"<rdf:li[^>]*>([^<]{1,2000})</rdf:li>")
_C2PA_MARKERS = (b"c2pa", b"urn:c2pa", b"jumbf", b"contentauth")

_IFD_NAMES = {
    getattr(ExifTags.IFD, "Exif", 0x8769): "EXIF",
    getattr(ExifTags.IFD, "GPSInfo", 0x8825): "GPS",
    getattr(ExifTags.IFD, "Interop", 0xA005): "INTEROP",
    getattr(ExifTags.IFD, "IFD1", 1): "IFD1",
}


def _clean(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            text = repr(value)
    else:
        text = str(value)
    text = text.replace("\x00", "").strip()
    if len(text) > MAX_VALUE_LEN:
        text = text[:MAX_VALUE_LEN] + "..."
    return text


def _put(out: dict[str, str], key: str, value: Any) -> None:
    text = _clean(value)
    if not text:
        return
    if key in out and out[key] != text:
        # keep the first value but record extras with a suffix
        n = 2
        while f"{key}#{n}" in out:
            n += 1
        key = f"{key}#{n}"
    out[key] = text


def _exif_tag_name(tag: int, ifd: str) -> str:
    if ifd == "GPS":
        return str(ExifTags.GPSTAGS.get(tag, f"Tag{tag}"))
    return str(ExifTags.TAGS.get(tag, f"Tag{tag}"))


def _extract_exif(img: Image.Image, out: dict[str, str]) -> None:
    try:
        exif = img.getexif()
    except Exception as exc:
        _put(out, "METADATA:error", f"exif: {exc}")
        return
    if not exif:
        return
    for tag, value in exif.items():
        if tag in _IFD_NAMES:
            continue
        _put(out, f"EXIF:{_exif_tag_name(int(tag), 'IFD0')}", value)
    for ifd_id, prefix in _IFD_NAMES.items():
        try:
            sub = exif.get_ifd(ifd_id)
        except Exception:
            continue
        if not sub:
            continue
        for tag, value in sub.items():
            _put(out, f"{prefix}:{_exif_tag_name(int(tag), prefix)}", value)


def _extract_info(img: Image.Image, out: dict[str, str]) -> None:
    fmt = (img.format or "IMAGE").upper()
    for key, value in (img.info or {}).items():
        if key in ("exif", "icc_profile", "xmp", "photoshop", "mpinfo"):
            continue
        if isinstance(value, (bytes, str, int, float)):
            _put(out, f"{fmt}:{key}", value)
        elif isinstance(value, (tuple, list)) and len(value) <= 8:
            _put(out, f"{fmt}:{key}", ", ".join(str(v) for v in value))
    text_chunks = getattr(img, "text", None)
    if isinstance(text_chunks, dict):
        for key, value in text_chunks.items():
            _put(out, f"{fmt}:{key}", value)


def extract_xmp(data: bytes, out: dict[str, str]) -> None:
    """Scan raw bytes for XMP packets and pull element/attribute values."""
    try:
        for packet in _XMP_PACKET_RE.findall(data):
            text = packet.decode("utf-8", errors="replace")
            for name, _attrs, value in _XMP_ELEMENT_RE.findall(text):
                _put(out, f"XMP:{name}", value)
            for name, value in _XMP_ATTR_RE.findall(text):
                if name.startswith("xmlns:") or name.startswith("rdf:"):
                    continue
                _put(out, f"XMP:{name}", value)
            for name, body in _XMP_LI_RE.findall(text):
                items = [_clean(i) for i in _LI_RE.findall(body)]
                if items:
                    _put(out, f"XMP:{name}", "; ".join(items))
    except Exception as exc:
        _put(out, "METADATA:error", f"xmp: {exc}")


def _scan_c2pa(data: bytes, out: dict[str, str]) -> None:
    lowered = data[: 4 * 1024 * 1024].lower()
    hits = [m.decode() for m in _C2PA_MARKERS if m in lowered]
    if hits:
        _put(out, "C2PA:manifest_markers", ", ".join(hits))


def extract_metadata(data: bytes, *, filename: str | None = None) -> dict[str, str]:
    """Return every metadata key/value we can find. Never raises."""
    out: dict[str, str] = {}
    if filename:
        _put(out, "FILE:name", filename)
    try:
        img = Image.open(io.BytesIO(data))
        _put(out, "FILE:format", img.format or "")
        _put(out, "FILE:size", f"{img.width}x{img.height}")
        _extract_exif(img, out)
        _extract_info(img, out)
        xmp_blob = (img.info or {}).get("xmp")
        if isinstance(xmp_blob, (bytes, bytearray)):
            extract_xmp(bytes(xmp_blob), out)
    except Exception as exc:
        _put(out, "METADATA:error", f"open: {exc}")
    extract_xmp(data, out)
    _scan_c2pa(data, out)
    return out
