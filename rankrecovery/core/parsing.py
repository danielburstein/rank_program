"""Turn raw extractor rows into structured detections.

Both the vision extractor and the OCR extractor produce :class:`RawRow`
objects; this module applies the server-specific prefix rules so that the
rest of the pipeline is extractor-agnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rankrecovery.core.enums import (
    Credibility,
    DetectionStatus,
    LeadingIcon,
    PrefixKind,
    RegionKind,
    SourceType,
    TrailingIcon,
)
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.ranks import PrefixMatch, RankPolicy
from rankrecovery.core.text import (
    DECORATION_CHARS,
    collapse_whitespace,
    normalise_prefix_text,
    strip_formatting_codes,
)
from rankrecovery.core.usernames import CANDIDATE_RE, looks_like_system_message, validate_username

BBox = tuple[int, int, int, int]  # x, y, w, h in full-image pixels

_JOIN_RE = re.compile(r"^\[?\+\]?$")
_MENTION_RE = re.compile(r"^@")
_LABEL_RE = re.compile(r"^[A-Za-z ]{2,20}:$")  # "Player:" style scoreboard labels


@dataclass(slots=True)
class RawRow:
    """One player row as reported by an extractor, before rule application."""

    text: str
    region: RegionKind = RegionKind.UNKNOWN
    bbox: BBox | None = None
    prefix_text: str | None = None
    username_text: str | None = None
    prefix_bbox: BBox | None = None
    username_bbox: BBox | None = None
    leading_icon: LeadingIcon = LeadingIcon.NONE
    trailing_icon: TrailingIcon = TrailingIcon.NONE
    prefix_colour: str | None = None
    confidence: float = 1.0
    prefix_unreadable: bool = False
    username_unreadable: bool = False
    username_cut_off: bool = False
    has_colon_after_name: bool | None = None
    extractor_notes: str = ""


@dataclass(slots=True)
class ParsedDetection:
    raw_text: str
    region: RegionKind
    source_type: SourceType
    credibility: Credibility
    bbox: BBox | None
    prefix_bbox: BBox | None
    username_bbox: BBox | None
    raw_prefix: str
    normalised_prefix: str
    prefix_match: PrefixMatch
    raw_username: str
    normalised_username: str
    username_valid: bool
    is_bedrock: bool
    partial: bool
    unreadable_username: bool
    status: DetectionStatus
    confidence: float
    rank_observation: str | None = None  # canonical rank implied by the prefix
    staff_observation: str | None = None  # canonical staff designation
    custom_prefix_text: str | None = None
    trailing_icon: TrailingIcon = TrailingIcon.NONE
    leading_icon: LeadingIcon = LeadingIcon.NONE
    trailing_icon_rank: str | None = None  # SCUFFED / INFINITE from icon after a staff name
    yinyang_evidence: str | None = None
    needs_review: bool = False
    flags: list[FlagInstance] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all_rank_observations(self) -> list[tuple[str, str]]:
        """(rank, basis) pairs this detection asserts."""
        out: list[tuple[str, str]] = []
        if self.rank_observation:
            out.append((self.rank_observation, "prefix"))
        if self.trailing_icon_rank:
            out.append((self.trailing_icon_rank, "trailing_icon"))
        return out


REGION_SOURCE: dict[RegionKind, tuple[SourceType, Credibility]] = {
    RegionKind.TAB: (SourceType.TAB, Credibility.HIGH),
    RegionKind.NAMETAG: (SourceType.NAMETAG, Credibility.HIGH),
    RegionKind.CHAT: (SourceType.CHAT, Credibility.NORMAL),
    RegionKind.SCOREBOARD: (SourceType.SCOREBOARD, Credibility.LOW),
    RegionKind.FULL: (SourceType.SCREENSHOT_OTHER, Credibility.NORMAL),
    RegionKind.UNKNOWN: (SourceType.SCREENSHOT_OTHER, Credibility.NORMAL),
}


class RowParser:
    def __init__(self, policy: RankPolicy, *, bedrock_prefix: str = ".", review_confidence: float = 0.8, min_confidence: float = 0.6):
        self.policy = policy
        self.bedrock_prefix = bedrock_prefix
        self.review_confidence = review_confidence
        self.min_confidence = min_confidence

    # ------------------------------------------------------------------ api
    def parse(self, row: RawRow) -> ParsedDetection | None:
        """Parse one row. Returns None for rows that carry no player at all."""
        source_type, credibility = REGION_SOURCE.get(row.region, (SourceType.SCREENSHOT_OTHER, Credibility.NORMAL))
        prefix_text, username_text, context_only, notes = self._split(row)
        if username_text is None and not prefix_text and not row.username_unreadable:
            return None

        flags: list[FlagInstance] = []
        needs_review = False
        status = DetectionStatus.CANDIDATE

        # ---- username
        raw_username = username_text or ""
        check = validate_username(raw_username, bedrock_prefix=self.bedrock_prefix)
        normalised_username = check.normalised
        unreadable_username = row.username_unreadable or (not raw_username and not row.username_cut_off)
        partial = row.username_cut_off
        if partial:
            status = DetectionStatus.PARTIAL
            flags.append(FlagInstance("PARTIAL_USERNAME", f"username {raw_username!r} is cut off at the image edge"))
        elif unreadable_username:
            status = DetectionStatus.UNREADABLE
            flags.append(FlagInstance("UNREADABLE_USERNAME", "username could not be read"))
        elif not check.valid:
            needs_review = True
            flags.append(FlagInstance("UNREADABLE_USERNAME", f"{raw_username!r} is not a valid username ({check.reason})"))
        if check.is_bedrock and check.valid:
            flags.append(FlagInstance("BEDROCK_PLAYER", f"{normalised_username} uses the Bedrock prefix"))

        # ---- prefix
        match = self.policy.match_prefix(prefix_text, unreadable=row.prefix_unreadable)
        rank_obs: str | None = None
        staff_obs: str | None = None
        custom_text: str | None = None
        yinyang_evidence: str | None = None
        trailing_rank: str | None = None

        if match.kind in (PrefixKind.PLAYER_RANK, PrefixKind.SEASONAL_ALIAS):
            rank_obs = match.canonical
            if row.trailing_icon in (TrailingIcon.YINYANG, TrailingIcon.INFINITY):
                notes.append(f"trailing {row.trailing_icon.value} icon after a non-staff row ignored")
        elif match.kind == PrefixKind.STAFF:
            staff_obs = match.canonical
            if row.trailing_icon == TrailingIcon.YINYANG:
                trailing_rank = self.policy.scuffed_rank
                yinyang_evidence = "yin-yang icon after staff username"
            elif row.trailing_icon == TrailingIcon.INFINITY:
                trailing_rank = self.policy.canonical_rank("INFINITE") or "INFINITE"
            elif row.leading_icon == LeadingIcon.YINYANG:
                trailing_rank = self.policy.scuffed_rank
                yinyang_evidence = "yin-yang icon decorating staff prefix"
        elif match.kind == PrefixKind.CUSTOM:
            rank_obs = self.policy.scuffed_rank
            custom_text = match.normalised_text
            if row.leading_icon == LeadingIcon.YINYANG:
                yinyang_evidence = "yin-yang icon around custom prefix"
            else:
                needs_review = True
                flags.append(FlagInstance("AMBIGUOUS_PREFIX", f"custom prefix {custom_text!r} without yin-yang decoration"))
        elif match.kind == PrefixKind.PROBABLE_MISREAD:
            needs_review = True
            flags.append(FlagInstance("PROBABLE_MISREAD", match.notes))
            if match.misread_of is None:
                flags.append(FlagInstance("AMBIGUOUS_PREFIX", f"{match.normalised_text!r} resembles several known prefixes"))
            else:
                flags.append(FlagInstance("AMBIGUOUS_PREFIX", f"{match.normalised_text!r} may be {match.misread_of}"))
                if match.misread_kind == PrefixKind.STAFF:
                    staff_obs = match.misread_of
                else:
                    rank_obs = match.misread_of
        elif match.kind == PrefixKind.UNREADABLE and (prefix_text or row.prefix_unreadable):
            needs_review = True
            flags.append(FlagInstance("AMBIGUOUS_PREFIX", "prefix present but unreadable"))
            if row.leading_icon == LeadingIcon.YINYANG and self.policy.allow_custom_prefix:
                rank_obs = self.policy.scuffed_rank
                custom_text = match.normalised_text or "(unreadable)"
                yinyang_evidence = "yin-yang icon around unreadable custom prefix"
            elif row.leading_icon == LeadingIcon.INFINITY:
                rank_obs = self.policy.canonical_rank("INFINITE")
                notes.append("prefix unreadable but infinity icon present")
            elif self.policy.allow_custom_prefix and row.leading_icon == LeadingIcon.DIAMOND and row.region in (RegionKind.TAB, RegionKind.NAMETAG):
                rank_obs = self.policy.scuffed_rank
                custom_text = match.normalised_text or "(unreadable)"
                notes.append("unreadable prefix in rank position treated as custom-prefix candidate pending review")
        elif match.kind == PrefixKind.NONE and not context_only and status == DetectionStatus.CANDIDATE:
            flags.append(FlagInstance("NAME_READ_RANK_NOT_FOUND", f"{normalised_username} read without a rank prefix"))

        if context_only:
            status = DetectionStatus.CONTEXT_ONLY
            rank_obs = None
            staff_obs = None
            trailing_rank = None

        # ---- confidence
        if row.confidence < self.review_confidence and status == DetectionStatus.CANDIDATE and (rank_obs or staff_obs):
            flags.append(FlagInstance("LOW_OCR_CONFIDENCE", f"confidence {row.confidence:.2f} below review threshold {self.review_confidence:.2f}"))
            needs_review = True

        if status in (DetectionStatus.PARTIAL, DetectionStatus.UNREADABLE):
            rank_obs = None
            trailing_rank = None
            # staff evidence without a readable name is not attributable either
            staff_obs = None
        elif needs_review and status == DetectionStatus.CANDIDATE:
            status = DetectionStatus.NEEDS_REVIEW

        return ParsedDetection(
            raw_text=row.text,
            region=row.region,
            source_type=source_type,
            credibility=credibility,
            bbox=row.bbox,
            prefix_bbox=row.prefix_bbox,
            username_bbox=row.username_bbox,
            raw_prefix=prefix_text or "",
            normalised_prefix=match.normalised_text,
            prefix_match=match,
            raw_username=raw_username,
            normalised_username=normalised_username,
            username_valid=check.valid,
            is_bedrock=check.is_bedrock,
            partial=partial,
            unreadable_username=unreadable_username,
            status=status,
            confidence=row.confidence,
            rank_observation=rank_obs,
            staff_observation=staff_obs,
            custom_prefix_text=custom_text,
            trailing_icon=row.trailing_icon,
            leading_icon=row.leading_icon,
            trailing_icon_rank=trailing_rank,
            yinyang_evidence=yinyang_evidence,
            needs_review=needs_review,
            flags=flags,
            notes=notes,
        )

    # -------------------------------------------------------------- helpers
    def _split(self, row: RawRow) -> tuple[str | None, str | None, bool, list[str]]:
        """Return (prefix_text, username_text, context_only, notes)."""
        notes: list[str] = []
        if row.username_text is not None or row.prefix_text is not None:
            username = (row.username_text or "").strip().rstrip(":") or None
            prefix = (row.prefix_text or "").strip() or None
            if username is None and row.username_unreadable:
                username = None
            return prefix, username, False, notes

        text = collapse_whitespace(strip_formatting_codes(row.text or ""))
        tokens = [t for t in text.split(" ") if t]
        if not tokens:
            return None, None, False, notes

        # Drop scoreboard-style labels ("Player:") that precede the actual row.
        while tokens and _LABEL_RE.match(tokens[0]) and row.region != RegionKind.CHAT:
            tokens.pop(0)

        if not tokens:
            return None, None, True, notes
        if row.region == RegionKind.CHAT or row.has_colon_after_name:
            return self._split_chat(tokens, notes)
        return self._split_tab(tokens, notes)

    def _split_chat(self, tokens: list[str], notes: list[str]) -> tuple[str | None, str | None, bool, list[str]]:
        # join message "[+] Name" or "+ Name"
        if tokens and _JOIN_RE.match(tokens[0]) and len(tokens) >= 2:
            notes.append("join message")
            return None, _clean_token(tokens[1]), True, notes
        # a line consisting of mentions only
        colon_index = next((i for i, t in enumerate(tokens) if t.endswith(":") and len(t) > 1), None)
        if colon_index is None:
            # system message: "GamerKasp died", "Yankster_08 won ..."
            first = _clean_token(tokens[0])
            if first and CANDIDATE_RE.match(first) and (looks_like_system_message(tokens[1:3]) or len(tokens) > 1):
                notes.append("system message or hologram; no rank context")
                return None, first, True, notes
            return None, None, True, notes
        username = _clean_token(tokens[colon_index].rstrip(":"))
        if _MENTION_RE.match(username):
            notes.append("mention")
            return None, username.lstrip("@"), True, notes
        prefix_tokens = [t for t in (_clean_token(x) for x in tokens[:colon_index]) if t]
        prefix = " ".join(prefix_tokens) if prefix_tokens else None
        return prefix, username, False, notes

    def _split_tab(self, tokens: list[str], notes: list[str]) -> tuple[str | None, str | None, bool, list[str]]:
        cleaned = [(_clean_token(t), t) for t in tokens]
        cleaned = [(c, o) for c, o in cleaned if c]
        if not cleaned:
            return None, None, False, notes
        # Ping numbers at the right of TAB rows are not names.
        while cleaned and cleaned[-1][0].isdigit() and len(cleaned) > 1:
            cleaned.pop()
        # The username is the last token that is a plausible username.
        for idx in range(len(cleaned) - 1, -1, -1):
            token = cleaned[idx][0]
            if CANDIDATE_RE.match(token):
                prefix_tokens = [c for c, _ in cleaned[:idx]]
                prefix = " ".join(prefix_tokens) if prefix_tokens else None
                # A single rank word alone (no name) is a prefix with an unreadable name.
                if prefix is None and self.policy.match_prefix(token).kind in (PrefixKind.PLAYER_RANK, PrefixKind.SEASONAL_ALIAS, PrefixKind.STAFF) and len(cleaned) == 1:
                    return token, None, False, notes
                return prefix, token, False, notes
        return " ".join(c for c, _ in cleaned), None, False, notes


_CLEAN_RE = re.compile("[" + re.escape(DECORATION_CHARS) + "]")


def _clean_token(token: str) -> str:
    return _CLEAN_RE.sub("", token).strip()


def normalise_for_search(text: str) -> str:
    return normalise_prefix_text(text).lower()
