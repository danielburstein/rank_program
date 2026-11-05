"""Image date policy: pick the most trustworthy capture date and apply the cutoff."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from rankrecovery.core.enums import DateSource, DateStatus

# Default Minecraft screenshot name: 2025-03-14_18.22.01.png (optionally with _2 suffix)
MC_FILENAME_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})_(\d{2})\.(\d{2})\.(\d{2})(?:_\d+)?")
# Common phone/screenshot patterns: Screenshot_20250314-182201, IMG_20250314_182201, 20250314_182201
GENERIC_FILENAME_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[-_ ]?(\d{2})(\d{2})(\d{2})(?!\d)")
DATE_ONLY_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")

# Metadata keys (lower-case) that carry a genuine capture/creation time.
EMBEDDED_DATE_KEYS = (
    "exif:datetimeoriginal",
    "exif:datetimedigitized",
    "exif:datetime",
    "xmp:createdate",
    "xmp:datecreated",
    "xmp:xmp:createdate",
    "png:creation time",
    "png:creationtime",
    "png:date:create",
    "png:create-date",
    "tiff:datetime",
)

_EXIF_DT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")


@dataclass(frozen=True, slots=True)
class DateEvidence:
    value: datetime | None
    source: DateSource
    raw: str = ""
    key: str = ""
    reliable: bool = False


@dataclass(frozen=True, slots=True)
class DatePolicy:
    cutoff: date
    trust_filename_pattern: bool = True
    trust_file_modified_time: bool = False


@dataclass(frozen=True, slots=True)
class DateDecision:
    status: DateStatus
    evidence: DateEvidence
    reason: str


def parse_metadata_datetime(raw: str) -> datetime | None:
    m = _EXIF_DT_RE.search(raw or "")
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=UTC)
    except ValueError:
        return None


def parse_filename_datetime(filename: str) -> DateEvidence | None:
    name = filename or ""
    m = MC_FILENAME_RE.search(name)
    if m:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        try:
            return DateEvidence(datetime(y, mo, d, h, mi, s, tzinfo=UTC), DateSource.FILENAME_PATTERN, raw=m.group(0), key="minecraft_filename")
        except ValueError:
            return None
    m = GENERIC_FILENAME_RE.search(name)
    if m:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        try:
            return DateEvidence(datetime(y, mo, d, h, mi, s, tzinfo=UTC), DateSource.FILENAME_PATTERN, raw=m.group(0), key="generic_timestamp_filename")
        except ValueError:
            return None
    m = DATE_ONLY_RE.search(name)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return DateEvidence(datetime(y, mo, d, tzinfo=UTC), DateSource.FILENAME_PATTERN, raw=m.group(0), key="date_only_filename")
        except ValueError:
            return None
    return None


def embedded_date_from_metadata(metadata: dict[str, str]) -> DateEvidence | None:
    lowered = {k.lower(): (k, v) for k, v in metadata.items()}
    for key in EMBEDDED_DATE_KEYS:
        if key in lowered:
            original_key, value = lowered[key]
            parsed = parse_metadata_datetime(str(value))
            if parsed:
                return DateEvidence(parsed, DateSource.EMBEDDED_METADATA, raw=str(value), key=original_key, reliable=True)
    # Any other key that obviously names a creation date
    for lk, (original_key, value) in lowered.items():
        if ("create" in lk or "original" in lk) and "date" in lk or lk.endswith("datetime"):
            parsed = parse_metadata_datetime(str(value))
            if parsed:
                return DateEvidence(parsed, DateSource.EMBEDDED_METADATA, raw=str(value), key=original_key, reliable=True)
    return None


def decide_image_date(
    *,
    metadata: dict[str, str],
    filename: str,
    file_modified: datetime | None,
    policy: DatePolicy,
    manual: DateEvidence | None = None,
) -> DateDecision:
    """Choose the best date evidence and evaluate it against the cutoff.

    Priority: manual review > embedded metadata > filename pattern (if trusted)
    > file modified time (if explicitly trusted). Import time is never used.
    """
    chosen: DateEvidence | None = manual
    if chosen is None:
        chosen = embedded_date_from_metadata(metadata)
    if chosen is None and policy.trust_filename_pattern:
        fn = parse_filename_datetime(filename)
        if fn:
            chosen = DateEvidence(fn.value, fn.source, fn.raw, fn.key, reliable=True)
    if chosen is None and policy.trust_file_modified_time and file_modified is not None:
        chosen = DateEvidence(file_modified, DateSource.FILE_MODIFIED, raw=file_modified.isoformat(), key="file_mtime", reliable=True)

    if chosen is None or chosen.value is None:
        untrusted = ""
        if file_modified is not None and not policy.trust_file_modified_time:
            untrusted = f" (file modified time {file_modified.date().isoformat()} ignored by policy)"
        return DateDecision(DateStatus.UNKNOWN, DateEvidence(None, DateSource.NONE), "no reliable capture date" + untrusted)

    if chosen.value.date() > policy.cutoff:
        reason = (
            f"{chosen.source.value} date {chosen.value.date().isoformat()} is after cutoff {policy.cutoff.isoformat()}"
            + (f" [{chosen.key}: {chosen.raw}]" if chosen.raw else "")
        )
        return DateDecision(DateStatus.OUT_OF_DATE, chosen, reason)
    return DateDecision(DateStatus.OK, chosen, f"{chosen.source.value} date {chosen.value.date().isoformat()} is on or before cutoff")
