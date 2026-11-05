"""UUID normalisation and Bedrock/Floodgate helpers (pure functions)."""

from __future__ import annotations

import re
import uuid as _uuid

UUID_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
UUID_DASHED_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalise_uuid(value: str | None) -> str | None:
    """Return the canonical dashed lower-case UUID, or None if invalid."""
    if value is None:
        return None
    text = value.strip()
    if UUID_HEX_RE.match(text):
        text = f"{text[0:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"
    if not UUID_DASHED_RE.match(text):
        return None
    return text.lower()


def undashed(value: str) -> str:
    return value.replace("-", "").lower()


def is_valid_uuid(value: str | None) -> bool:
    return normalise_uuid(value) is not None


def floodgate_uuid_from_xuid(xuid: int | str) -> str:
    """Floodgate UUID: 00000000-0000-0000-xxxx-xxxxxxxxxxxx with the XUID as 16 hex digits."""
    x = int(xuid)
    if x < 0 or x >= 1 << 64:
        raise ValueError("XUID out of range")
    hex16 = f"{x:016x}"
    return f"00000000-0000-0000-{hex16[0:4]}-{hex16[4:16]}"


def is_floodgate_uuid(value: str | None) -> bool:
    norm = normalise_uuid(value)
    return bool(norm and norm.startswith("00000000-0000-0000-"))


def offline_mode_uuid(name: str) -> str:
    """UUID an offline-mode server would assign (documented for completeness; never used for export)."""
    return str(_uuid.uuid3(_uuid.NAMESPACE_OID, f"OfflinePlayer:{name}"))
