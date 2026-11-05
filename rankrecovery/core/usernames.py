"""Minecraft username validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

JAVA_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
# Bedrock players through Geyser/Floodgate carry a prefix (default ".") and may contain spaces
# in their real gamertag, but in TAB they appear without spaces. Allow up to 16 chars after the dot.
BEDROCK_USERNAME_RE = re.compile(r"^\.[A-Za-z0-9_]{1,16}$")
# Looser pattern used to recognise *candidate* names in OCR output (may need review).
CANDIDATE_RE = re.compile(r"^\.?[A-Za-z0-9_]{2,20}$")

_CHAT_SYSTEM_WORDS = {"died", "won", "joined", "left", "quit", "killed", "was", "slain", "fell", "drowned", "burned"}


@dataclass(frozen=True, slots=True)
class UsernameCheck:
    raw: str
    normalised: str
    valid: bool
    is_bedrock: bool
    reason: str = ""


def validate_username(raw: str, *, bedrock_prefix: str = ".") -> UsernameCheck:
    text = (raw or "").strip()
    text = text.rstrip(":")  # chat lines end with a colon after the name
    if not text:
        return UsernameCheck(raw, "", False, False, "empty")
    is_bedrock = text.startswith(bedrock_prefix) and bedrock_prefix != ""
    if is_bedrock:
        candidate = text
        if bedrock_prefix != "." and candidate.startswith(bedrock_prefix):
            candidate = "." + candidate[len(bedrock_prefix):]
        if BEDROCK_USERNAME_RE.match(candidate):
            return UsernameCheck(raw, candidate, True, True)
        return UsernameCheck(raw, candidate, False, True, "invalid bedrock name")
    if JAVA_USERNAME_RE.match(text):
        return UsernameCheck(raw, text, True, False)
    if CANDIDATE_RE.match(text):
        return UsernameCheck(raw, text, False, False, "length outside 3-16")
    return UsernameCheck(raw, text, False, False, "invalid characters")


def normalise_username_key(name: str) -> str:
    """Case-insensitive key used for alias matching."""
    return (name or "").strip().rstrip(":").lower()


def looks_like_system_message(tokens: list[str]) -> bool:
    lowered = {t.lower() for t in tokens}
    return bool(lowered & _CHAT_SYSTEM_WORDS)
