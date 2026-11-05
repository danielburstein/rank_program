"""Text normalisation helpers for Minecraft prefixes and usernames."""

from __future__ import annotations

import re
import unicodedata

# Minecraft formatting codes: §a, &l etc.
_FORMATTING_CODE_RE = re.compile(r"[§&][0-9a-fk-orA-FK-OR]")

# Decorative glyphs that appear around prefixes in this server's chat format.
DIAMOND_CHARS = "◇◆⬥⬦◈♦⋄◊"
YINYANG_CHARS = "☯"
INFINITY_CHARS = "∞"
SWIRL_CHARS = "🌀✧✦✨❋❂☸"
DECORATION_CHARS = DIAMOND_CHARS + YINYANG_CHARS + INFINITY_CHARS + SWIRL_CHARS + "«»<>[]{}()|•·*~"

_DECORATION_RE = re.compile("[" + re.escape(DECORATION_CHARS) + "]")
_WS_RE = re.compile(r"\s+")


def strip_formatting_codes(text: str) -> str:
    return _FORMATTING_CODE_RE.sub("", text)


def strip_decorations(text: str) -> str:
    return _DECORATION_RE.sub(" ", text)


def collapse_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalise_prefix_text(text: str) -> str:
    """Normalise a raw prefix for comparison: no codes, no decorations, upper-case.

    The ``+`` character is preserved because IMMORTAL+ and ADMIN+ are distinct
    designations from IMMORTAL and ADMIN.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = strip_formatting_codes(text)
    text = strip_decorations(text)
    text = collapse_whitespace(text)
    return text.upper()


def squash_spaces(text: str) -> str:
    """Remove all spaces, used to compare SR MOD with SRMOD."""
    return text.replace(" ", "").replace("-", "").replace(".", "")


def has_plus(text: str) -> bool:
    return "+" in text


# OCR confusion classes. Each inner string lists characters that OCR commonly
# confuses with each other when reading the Minecraft pixel font.
CONFUSION_CLASSES: tuple[str, ...] = (
    "0OoDQ",
    "1Il|!",
    "5Ss",
    "8B",
    "2Z",
    "6Gb",
    "9gq",
    "4A",
    "7T",
    "UuVv",
    "Cc(",
    "Rr",
    "MmNn",
    "EeF",
    "KkX",
)

_CONFUSION_MAP: dict[str, str] = {}
for _cls in CONFUSION_CLASSES:
    _canon = _cls[0]
    for _ch in _cls:
        _CONFUSION_MAP[_ch] = _canon


def confusion_fold(text: str) -> str:
    """Map every character to the canonical member of its confusion class."""
    return "".join(_CONFUSION_MAP.get(ch, ch) for ch in text.upper())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
