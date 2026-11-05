from __future__ import annotations

from datetime import UTC, date, datetime

from rankrecovery.config.defaults import DEFAULT_AI_FILENAME_PATTERNS, DEFAULT_AI_METADATA_INDICATORS
from rankrecovery.core.ai_indicators import scan_metadata
from rankrecovery.core.dates import DatePolicy, decide_image_date, parse_filename_datetime
from rankrecovery.core.enums import AiStatus, DateSource, DateStatus
from rankrecovery.core.identity import floodgate_uuid_from_xuid, is_floodgate_uuid, normalise_uuid


def test_minecraft_filename_date():
    ev = parse_filename_datetime("2025-03-14_18.22.01.png")
    assert ev and ev.value == datetime(2025, 3, 14, 18, 22, 1, tzinfo=UTC) and ev.source == DateSource.FILENAME_PATTERN


def test_cutoff_uses_reliable_evidence_not_mtime():
    policy = DatePolicy(cutoff=date(2026, 1, 1))
    d = decide_image_date(metadata={}, filename="2026-02-01_10.00.00.png", file_modified=datetime(2024, 1, 1, tzinfo=UTC), policy=policy)
    assert d.status == DateStatus.OUT_OF_DATE and "filename_pattern" in d.reason
    d = decide_image_date(metadata={}, filename="random.png", file_modified=datetime(2026, 5, 1, tzinfo=UTC), policy=policy)
    assert d.status == DateStatus.UNKNOWN and "ignored by policy" in d.reason
    d = decide_image_date(metadata={"EXIF:DateTimeOriginal": "2026:03:01 12:00:00"}, filename="2024-01-01_00.00.00.png", file_modified=None, policy=policy)
    assert d.status == DateStatus.OUT_OF_DATE and d.evidence.source == DateSource.EMBEDDED_METADATA


def test_ai_scan():
    r = scan_metadata({"PNG:Software": "ChatGPT image generator"}, indicators=DEFAULT_AI_METADATA_INDICATORS)
    assert r.status == AiStatus.AI_INDICATOR_FOUND and r.hits[0].field == "PNG:Software"
    r = scan_metadata({}, indicators=DEFAULT_AI_METADATA_INDICATORS, filename="DALL-E 2025.png", filename_patterns=DEFAULT_AI_FILENAME_PATTERNS)
    assert r.status == AiStatus.AI_FILENAME_INDICATOR and r.hits[0].weak
    r = scan_metadata({}, indicators=DEFAULT_AI_METADATA_INDICATORS, filename="2025-03-14_18.22.01.png", filename_patterns=DEFAULT_AI_FILENAME_PATTERNS)
    assert r.status == AiStatus.NO_AI_INDICATOR_FOUND and not r.flagged


def test_uuid_helpers():
    assert normalise_uuid("069A79F444E94726A5BEFCA90E38AAF5") == "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    assert normalise_uuid("nope") is None
    fg = floodgate_uuid_from_xuid(2535459493735633)
    assert fg.startswith("00000000-0000-0000-") and is_floodgate_uuid(fg)
