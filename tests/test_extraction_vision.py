from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from rankrecovery.core.enums import ExtractorType, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.parsing import RowParser
from rankrecovery.core.ranks import default_policy
from rankrecovery.extraction.base import DetectedRegion, ExtractionSettings, ExtractorUnavailable
from rankrecovery.extraction.pipeline import run_extraction
from rankrecovery.extraction.vision import FORMAT_DESCRIPTION, AnthropicVisionExtractor


class FakeMessages:
    def __init__(self, responses, *, raise_exc=None):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.raise_exc = raise_exc

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        payload = self.responses.pop(0)
        if isinstance(payload, dict) and "stop_reason" in payload:
            stop = payload["stop_reason"]
            text = payload.get("text", "{}")
            details = payload.get("stop_details")
        else:
            stop, text, details = "end_turn", json.dumps(payload), None
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop,
            stop_details=details,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


class FakeClient:
    def __init__(self, responses, **kw):
        self.messages = FakeMessages(responses, **kw)


def settings(**extra):
    return ExtractionSettings(extractor=ExtractorType.VISION, engine="anthropic", model="claude-opus-5-5", upscale_factor=2, max_crop_edge=1568, effort="medium", max_tokens=4000, extra={"model_layout": True, **extra})


def png(width=400, height=200):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


LAYOUT = {"regions": [{"kind": "tab", "bbox": [100, 100, 900, 600], "columns": [], "confidence": 0.9}]}
ROWS = {
    "rows": [
        {"raw_prefix_text": "Titan", "prefix_unreadable": False, "username": "AndrewIsFire", "username_unreadable": False, "username_cut_off": False, "prefix_colour": "red", "leading_icon": "diamond", "trailing_icon": "none", "region": "tab", "bbox": [0, 0, 500, 500], "has_colon_after_name": False, "confidence": 0.95, "notes": ""},
        {"raw_prefix_text": "Mod", "prefix_unreadable": False, "username": "Player4995", "username_unreadable": False, "username_cut_off": False, "prefix_colour": "blue", "leading_icon": "diamond", "trailing_icon": "yinyang", "region": "tab", "bbox": [0, 500, 500, 1000], "has_colon_after_name": False, "confidence": 0.9, "notes": ""},
        {"raw_prefix_text": "Galactic", "prefix_unreadable": False, "username": "Zeyos4Dan", "username_unreadable": False, "username_cut_off": True, "prefix_colour": "red", "leading_icon": "diamond", "trailing_icon": "none", "region": "tab", "bbox": [500, 0, 1000, 500], "has_colon_after_name": False, "confidence": 0.8, "notes": "cut off"},
    ],
    "all_text": "Titan AndrewIsFire Mod Player4995 Galactic Zeyos4Dan",
}


def test_requires_api_key_without_client():
    with pytest.raises(ExtractorUnavailable):
        AnthropicVisionExtractor(settings(), None)


def test_prompt_contains_icon_rules():
    assert "yin-yang" in FORMAT_DESCRIPTION and "∞" in FORMAT_DESCRIPTION and "swirl" in FORMAT_DESCRIPTION
    assert "cut off" in FORMAT_DESCRIPTION and "[+] Name" in FORMAT_DESCRIPTION


def test_extract_with_fake_client_maps_boxes_and_icons():
    client = FakeClient([LAYOUT, ROWS])
    ext = AnthropicVisionExtractor(settings(), "unused", client=client, region_detector=lambda img: [])
    out = ext.extract(png(), filename="x.png")
    assert out.ok and out.extractor == ExtractorType.VISION
    assert len(client.messages.calls) == 2
    first = client.messages.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"]["format"]["type"] == "json_schema" and first["output_config"]["effort"] == "medium"
    assert "thinking" not in first
    assert first["messages"][0]["content"][0]["type"] == "image"
    # layout region: 100..900 x 100..600 of a 400x200 image -> (40,20,320,100)
    assert out.regions[0].bbox == (40, 20, 320, 100)
    rows = {r.username_text: r for r in out.rows}
    assert rows["AndrewIsFire"].bbox == (40, 20, 160, 50)  # left half, top half of the region
    assert rows["Player4995"].trailing_icon == TrailingIcon.YINYANG and rows["Player4995"].leading_icon == LeadingIcon.DIAMOND
    assert rows["Zeyos4Dan"].username_cut_off
    assert out.cost["api_calls"] == 2 and out.cost["input_tokens"] == 200
    assert "Titan AndrewIsFire" in out.full_text
    assert '"call": "layout"' in out.raw_output


def test_pipeline_parses_vision_rows():
    client = FakeClient([LAYOUT, ROWS])
    ext = AnthropicVisionExtractor(settings(), "k", client=client, region_detector=lambda img: [])
    result = run_extraction(png(), ext, RowParser(default_policy()))
    by_name = {d.normalised_username: d for d in result.detections}
    assert by_name["AndrewIsFire"].rank_observation == "TITAN"
    assert by_name["Player4995"].staff_observation == "MOD" and by_name["Player4995"].trailing_icon_rank == "SCUFFED"
    assert by_name["Zeyos4Dan"].partial and by_name["Zeyos4Dan"].rank_observation is None


def test_explicit_regions_skip_layout_call():
    client = FakeClient([ROWS])
    ext = AnthropicVisionExtractor(settings(), "k", client=client)
    region = DetectedRegion(kind=RegionKind.NAMETAG, bbox=(0, 0, 200, 100))
    out = ext.extract(png(), regions=[region])
    assert len(client.messages.calls) == 1 and out.ok


def test_refusal_and_bad_json_are_warnings():
    client = FakeClient([{"stop_reason": "refusal", "text": "", "stop_details": SimpleNamespace(category="cyber")}, {"stop_reason": "end_turn", "text": "not json"}])
    ext = AnthropicVisionExtractor(settings(), "k", client=client, region_detector=lambda img: [])
    out = ext.extract(png())
    assert out.ok and out.rows == []
    assert any("refused" in w for w in out.warnings) and any("invalid JSON" in w for w in out.warnings)


def test_api_errors_are_wrapped():
    import anthropic

    request = SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=request)  # type: ignore[arg-type]
    client = FakeClient([], raise_exc=exc)
    ext = AnthropicVisionExtractor(settings(), "k", client=client, region_detector=lambda img: [])
    out = ext.extract(png())
    assert not out.ok and "connection" in (out.error or "").lower()


def test_wide_crops_are_tiled_and_deduped():
    row = dict(ROWS["rows"][0])
    client = FakeClient([{"rows": [row], "all_text": ""}, {"rows": [row], "all_text": ""}, {"rows": [row], "all_text": ""}])
    s = settings()
    s.max_crop_edge = 400  # with upscale 2 -> tiles of 200px
    ext = AnthropicVisionExtractor(s, "k", client=client)
    region = DetectedRegion(kind=RegionKind.TAB, bbox=(0, 0, 400, 50))
    out = ext.extract(png(400, 50), regions=[region])
    assert len(client.messages.calls) >= 2
    assert len(out.rows) >= 1
