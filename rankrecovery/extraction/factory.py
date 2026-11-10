"""Build the configured extractor from the persisted settings dictionary."""

from __future__ import annotations

from typing import Any

from rankrecovery.core.enums import ExtractorType
from rankrecovery.extraction.base import ExtractionSettings, ExtractorUnavailable, TextExtractor


def resolve_extractor_type(settings: dict[str, Any], api_key: str | None) -> ExtractorType:
    choice = str(settings.get("extraction.default_extractor", "auto")).lower()
    if choice == "vision":
        return ExtractorType.VISION
    if choice == "ocr":
        return ExtractorType.OCR
    return ExtractorType.VISION if api_key else ExtractorType.OCR


def make_extraction_settings(settings: dict[str, Any], extractor_type: ExtractorType) -> ExtractionSettings:
    if extractor_type == ExtractorType.VISION:
        return ExtractionSettings(
            extractor=ExtractorType.VISION,
            engine="anthropic",
            model=str(settings.get("extraction.vision_model", "claude-opus-5-5")),
            upscale_factor=int(settings.get("extraction.vision_upscale_factor", 2)),
            max_crop_edge=int(settings.get("extraction.vision_max_crop_edge", 1568)),
            effort=str(settings.get("extraction.vision_effort", "medium")),
            max_tokens=int(settings.get("extraction.vision_max_tokens", 16000)),
            preprocessing="nearest_upscale",
            prompt_version=str(settings.get("extraction.vision_prompt_version", "1")),
            extra={
                "timeout": float(settings.get("extraction.vision_timeout_seconds", 120)),
                "max_retries": int(settings.get("extraction.vision_max_retries", 3)),
                "model_layout": bool(settings.get("extraction.vision_model_layout", True)),
                "server_fallback": bool(settings.get("extraction.vision_server_fallback", True)),
            },
        )
    engine = str(settings.get("extraction.ocr_engine", "tesseract")).lower()
    return ExtractionSettings(
        extractor=ExtractorType.OCR,
        engine=engine,
        model="",
        upscale_factor=int(settings.get("extraction.ocr_upscale_factor", 3)),
        max_crop_edge=int(settings.get("extraction.ocr_max_crop_edge", 4000)),
        effort="",
        max_tokens=0,
        preprocessing=str(settings.get("extraction.ocr_preprocessing", "default")),
        prompt_version="",
        extra={"ocr_variants": list(settings.get("extraction.ocr_variants", ["text_only", "otsu_smooth"]))},
    )


def build_extractor(settings: dict[str, Any], api_key: str | None, *, client: Any | None = None, extractor_type: ExtractorType | None = None) -> TextExtractor:
    chosen = extractor_type or resolve_extractor_type(settings, api_key)
    ext_settings = make_extraction_settings(settings, chosen)
    if chosen == ExtractorType.VISION:
        from rankrecovery.extraction.vision import AnthropicVisionExtractor

        return AnthropicVisionExtractor(
            ext_settings,
            api_key,
            client=client,
            concurrency=int(settings.get("extraction.vision_region_concurrency", 3)),
            max_concurrent_calls=int(settings.get("extraction.vision_max_concurrent_calls", 8)),
        )
    if ext_settings.engine == "easyocr":
        from rankrecovery.extraction.ocr import EasyOcrExtractor

        try:
            return EasyOcrExtractor(ext_settings)
        except ExtractorUnavailable:
            ext_settings.engine = "tesseract"
    from rankrecovery.extraction.ocr import TesseractExtractor

    return TesseractExtractor(ext_settings)
