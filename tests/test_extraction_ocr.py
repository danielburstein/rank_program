from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from rankrecovery.core.enums import ExtractorType, RegionKind
from rankrecovery.core.parsing import RowParser
from rankrecovery.core.ranks import default_policy
from rankrecovery.core.text import confusion_fold
from rankrecovery.extraction.base import DetectedRegion, ExtractionSettings
from rankrecovery.extraction.factory import build_extractor, make_extraction_settings, resolve_extractor_type
from rankrecovery.extraction.pipeline import run_extraction

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "screenshots"
HAS_TESSERACT = shutil.which("tesseract") is not None


def ocr_settings():
    return ExtractionSettings(extractor=ExtractorType.OCR, engine="tesseract", upscale_factor=3, max_crop_edge=4000, extra={"ocr_variants": ["text_only", "otsu_smooth"]})


def render(text: str, size=(640, 90)) -> bytes:
    img = Image.new("RGB", size, (15, 15, 25))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    draw.text((20, 20), text, fill=(240, 240, 240), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_factory_resolution():
    assert resolve_extractor_type({"extraction.default_extractor": "auto"}, None) == ExtractorType.OCR
    assert resolve_extractor_type({"extraction.default_extractor": "auto"}, "key") == ExtractorType.VISION
    assert resolve_extractor_type({"extraction.default_extractor": "ocr"}, "key") == ExtractorType.OCR
    s = make_extraction_settings({"extraction.vision_model": "claude-opus-5-5"}, ExtractorType.VISION)
    assert s.engine == "anthropic" and s.fingerprint() != make_extraction_settings({}, ExtractorType.OCR).fingerprint()


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_tesseract_reads_synthetic_row():
    from rankrecovery.extraction.ocr import TesseractExtractor

    ext = TesseractExtractor(ocr_settings())
    data = render("Titan AndrewIsFire")
    out = ext.extract(data, regions=[DetectedRegion(kind=RegionKind.TAB, bbox=(0, 0, 640, 90))])
    assert out.ok and out.extractor == ExtractorType.OCR
    # Tesseract confuses I/l in sans-serif fonts; compare after folding that confusion.
    def fold(text: str) -> str:
        return confusion_fold(text.replace("l", "I"))

    assert fold("AndrewIsFire") in fold(out.full_text.replace(" ", ""))
    result = run_extraction(data, ext, RowParser(default_policy()), regions=[DetectedRegion(kind=RegionKind.TAB, bbox=(0, 0, 640, 90))])
    det = next(d for d in result.detections if fold(d.normalised_username) == fold("AndrewIsFire"))
    assert det.rank_observation == "TITAN"


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_build_extractor_defaults_to_ocr_without_key():
    ext = build_extractor({"extraction.default_extractor": "auto"}, None)
    assert ext.settings.extractor == ExtractorType.OCR


@pytest.mark.skipif(not HAS_TESSERACT or not (SAMPLES / "scuffed_nametag.png").exists(), reason="tesseract or samples missing")
@pytest.mark.xfail(reason="Tesseract's stock model reads the Minecraft pixel font unreliably", strict=False)
def test_tesseract_on_scuffed_nametag_sample():
    ext = build_extractor({"extraction.default_extractor": "ocr"}, None)
    out = ext.extract((SAMPLES / "scuffed_nametag.png").read_bytes())
    haystack = (out.full_text + " " + " ".join(w["text"] for w in out.words)).lower()
    assert "zspades" in haystack.replace(" ", "")
