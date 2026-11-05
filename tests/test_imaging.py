from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from rankrecovery.core.enums import RegionKind
from rankrecovery.imaging.decode import decode_image, image_facts, iter_frames, select_frame_indices
from rankrecovery.imaging.hashing import (
    DuplicateClass,
    DuplicateThresholds,
    classify_duplicate,
    hamming_distance,
    perceptual_hash,
    sha256_hex,
)
from rankrecovery.imaging.metadata import extract_metadata, extract_xmp
from rankrecovery.imaging.overlay import OverlayBox, overlay_png
from rankrecovery.imaging.regions import crop_and_upscale, detect_regions, fallback_full_image

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "screenshots"


def _png_bytes(img: Image.Image, text: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    for k, v in (text or {}).items():
        info.add_text(k, v)
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def test_decode_and_facts():
    img = Image.new("RGB", (40, 30), (10, 20, 30))
    data = _png_bytes(img)
    facts = image_facts(data)
    assert facts.format == "PNG" and (facts.width, facts.height) == (40, 30) and facts.frame_count == 1
    assert decode_image(data).size == (40, 30)


def test_animated_frames():
    frames = [Image.new("RGB", (16, 16), (i * 20, 0, 0)) for i in range(10)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    data = buf.getvalue()
    facts = image_facts(data)
    assert facts.animated and facts.frame_count == 10
    assert select_frame_indices(10, 4) == [0, 3, 6, 9]
    seen = [i for i, _ in iter_frames(data, max_frames=4)]
    assert seen == [0, 3, 6, 9]


@pytest.mark.skipif(not (SAMPLES / "tab_phone.png").exists(), reason="samples missing")
def test_hashing_and_duplicate_classes():
    a = Image.open(SAMPLES / "tab_phone.png").convert("RGB")
    buf = io.BytesIO()
    a.resize((a.width * 6 // 10, a.height * 6 // 10)).save(buf, format="JPEG", quality=70)  # resized + re-encoded copy
    b = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    c = Image.open(SAMPLES / "owner_nametag.png").convert("RGB")
    ha, hb, hc = perceptual_hash(a), perceptual_hash(b), perceptual_hash(c)
    assert hamming_distance(ha, ha) == 0
    assert hamming_distance(ha, hb) <= 6
    assert hamming_distance(ha, hc) > 12
    sha_a = sha256_hex(_png_bytes(Image.new("RGB", (4, 4))))
    assert classify_duplicate(sha_a=sha_a, sha_b=sha_a, phash_a=ha, phash_b=ha)[0] == DuplicateClass.EXACT
    assert classify_duplicate(sha_a=sha_a, sha_b="x", phash_a=ha, phash_b=hb)[0] == DuplicateClass.DUPLICATE
    assert classify_duplicate(sha_a=sha_a, sha_b="x", phash_a=ha, phash_b=hc)[0] == DuplicateClass.DISTINCT
    assert classify_duplicate(sha_a=sha_a, sha_b="x", phash_a=ha, phash_b=hb, thresholds=DuplicateThresholds(0, 64))[0] == DuplicateClass.NEAR_DUPLICATE


def test_metadata_png_text_and_filename():
    data = _png_bytes(Image.new("RGB", (8, 8)), {"Software": "ChatGPT image export", "Comment": "hello"})
    meta = extract_metadata(data, filename="2025-03-14_18.22.01.png")
    assert meta["PNG:Software"] == "ChatGPT image export"
    assert meta["PNG:Comment"] == "hello"
    assert meta["FILE:name"] == "2025-03-14_18.22.01.png"


def test_metadata_jpeg_exif():
    img = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    exif[0x0110] = "Pixel 7"  # Model
    exif[0x0132] = "2024:05:01 10:11:12"  # DateTime
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = "2024:05:01 10:11:12"  # DateTimeOriginal
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    meta = extract_metadata(buf.getvalue())
    assert meta["EXIF:Model"] == "Pixel 7"
    assert meta["EXIF:DateTimeOriginal"] == "2024:05:01 10:11:12"


def test_xmp_scan_finds_creator_tool():
    packet = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description xmp:CreatorTool="DALL-E 3" Iptc4xmpExt:DigitalSourceType="trainedAlgorithmicMedia"><dc:creator><rdf:Seq><rdf:li>OpenAI</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF></x:xmpmeta>'
    out: dict[str, str] = {}
    extract_xmp(b"garbage" + packet + b"trailer", out)
    assert out["XMP:xmp:CreatorTool"] == "DALL-E 3"
    assert out["XMP:Iptc4xmpExt:DigitalSourceType"] == "trainedAlgorithmicMedia"
    assert out["XMP:dc:creator"] == "OpenAI"


def test_metadata_never_raises_on_garbage():
    meta = extract_metadata(b"not an image at all", filename="x.png")
    assert "METADATA:error" in meta and meta["FILE:name"] == "x.png"


@pytest.mark.skipif(not (SAMPLES / "tab_phone.png").exists(), reason="samples missing")
def test_region_detector_finds_tab_on_phone_sample():
    img = Image.open(SAMPLES / "tab_phone.png").convert("RGB")
    regions = detect_regions(img)
    tabs = [r for r in regions if r.kind == RegionKind.TAB]
    assert tabs, [r.kind for r in regions]
    x, y, w, h = tabs[0].bbox
    assert y < img.height * 0.4 and y + h > img.height * 0.4  # spans the list area
    assert w > img.width * 0.5


@pytest.mark.skipif(not (SAMPLES / "owner_nametag.png").exists(), reason="samples missing")
def test_region_detector_finds_chat_and_scoreboard_on_nametag_sample():
    img = Image.open(SAMPLES / "owner_nametag.png").convert("RGB")
    regions = detect_regions(img)
    kinds = {r.kind for r in regions}
    assert RegionKind.CHAT in kinds and RegionKind.SCOREBOARD in kinds
    chat = next(r for r in regions if r.kind == RegionKind.CHAT)
    assert chat.bbox[0] < 50 and chat.bbox[1] > img.height * 0.8


def test_crop_and_upscale_mapping():
    img = Image.new("RGB", (200, 100))
    crop, mapping = crop_and_upscale(img, (50, 20, 60, 30), 3, 1000)
    assert crop.size == (180, 90)
    assert mapping.to_full((30, 30, 60, 30)) == (60, 30, 20, 10)
    crop2, mapping2 = crop_and_upscale(img, (0, 0, 200, 100), 10, 500)
    assert max(crop2.size) == 500 and abs(mapping2.scale - 2.5) < 1e-6
    fb = fallback_full_image(img)
    assert fb.kind == RegionKind.FULL and fb.bbox == (0, 0, 200, 100)


def test_overlay_png_renders():
    img = Image.new("RGB", (120, 80), (0, 0, 0))
    data = overlay_png(img, [OverlayBox((10, 10, 50, 20), "row", "Titan AndrewIsFire"), OverlayBox((12, 12, 20, 16), "prefix")])
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(io.BytesIO(data)).size == (120, 80)
