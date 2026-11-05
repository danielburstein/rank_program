"""Rank policy: ordered player ranks, seasonal aliases, staff prefixes and matching.

The policy is data-driven. Nothing in this module hard-codes a rank name
except the names used by the shipped defaults, which live in
:mod:`rankrecovery.config.defaults`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rankrecovery.core.enums import PrefixKind
from rankrecovery.core.text import confusion_fold, has_plus, levenshtein, normalise_prefix_text, squash_spaces


class PolicyError(ValueError):
    """Raised when a rank policy is invalid (duplicate or ambiguous names/aliases)."""


AMBIGUOUS = "__ambiguous__"


@dataclass(frozen=True, slots=True)
class RankDef:
    name: str
    priority: int
    aliases: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class StaffDef:
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrefixMatch:
    """Result of matching a raw prefix against the policy."""

    kind: PrefixKind
    raw_text: str
    normalised_text: str
    canonical: str | None = None  # canonical rank or staff name
    matched_alias: str | None = None  # the alias/spelling that matched, if not canonical
    misread_of: str | None = None  # for PROBABLE_MISREAD
    misread_kind: PrefixKind | None = None  # PLAYER_RANK or STAFF when misread
    confidence: float = 1.0
    notes: str = ""

    @property
    def is_player_rank(self) -> bool:
        return self.kind in (PrefixKind.PLAYER_RANK, PrefixKind.SEASONAL_ALIAS)


@dataclass(frozen=True)
class RankPolicy:
    """Immutable view of the configured rank rules."""

    ranks: tuple[RankDef, ...]
    staff: tuple[StaffDef, ...]
    scuffed_rank: str = "SCUFFED"
    allow_custom_prefix: bool = True
    version: int = 0
    _rank_lookup: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False, compare=False)
    _staff_lookup: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False, compare=False)
    _by_name: dict[str, RankDef] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------ build
    @classmethod
    def from_config(
        cls,
        player_ranks: list[dict[str, Any]],
        staff_prefixes: list[dict[str, Any]],
        *,
        scuffed_rank: str = "SCUFFED",
        allow_custom_prefix: bool = True,
        version: int = 0,
    ) -> RankPolicy:
        ranks: list[RankDef] = []
        for index, item in enumerate(player_ranks, start=1):
            name = normalise_prefix_text(str(item["name"]))
            if not name:
                raise PolicyError("Rank name cannot be empty")
            aliases = tuple(sorted({normalise_prefix_text(str(a)) for a in item.get("aliases", []) if str(a).strip()}))
            ranks.append(RankDef(name=name, priority=int(item.get("priority", index)), aliases=aliases, enabled=bool(item.get("enabled", True))))
        staff: list[StaffDef] = []
        for item in staff_prefixes:
            name = normalise_prefix_text(str(item["name"]))
            if not name:
                raise PolicyError("Staff prefix name cannot be empty")
            aliases = tuple(sorted({normalise_prefix_text(str(a)) for a in item.get("aliases", []) if str(a).strip()}))
            staff.append(StaffDef(name=name, aliases=aliases))
        policy = cls(
            ranks=tuple(sorted(ranks, key=lambda r: r.priority)),
            staff=tuple(staff),
            scuffed_rank=normalise_prefix_text(scuffed_rank),
            allow_custom_prefix=allow_custom_prefix,
            version=version,
        )
        policy._build_lookups()
        return policy

    def _build_lookups(self) -> None:
        rank_lookup: dict[str, tuple[str, str | None]] = {}
        staff_lookup: dict[str, tuple[str, str | None]] = {}
        by_name: dict[str, RankDef] = {}
        seen_priorities: set[int] = set()
        for rank in self.ranks:
            if rank.name in by_name:
                raise PolicyError(f"Duplicate rank name {rank.name}")
            if rank.priority in seen_priorities:
                raise PolicyError(f"Duplicate rank priority {rank.priority} for {rank.name}")
            seen_priorities.add(rank.priority)
            by_name[rank.name] = rank
        for rank in self.ranks:
            keys = [rank.name, *rank.aliases]
            for key in keys:
                existing = rank_lookup.get(key)
                if existing and existing[0] != rank.name:
                    raise PolicyError(f"Alias {key!r} is ambiguous between {existing[0]} and {rank.name}")
                rank_lookup[key] = (rank.name, None if key == rank.name else key)
        for staff in self.staff:
            for key in [staff.name, *staff.aliases]:
                squashed = squash_spaces(key)
                existing = staff_lookup.get(squashed)
                if existing and existing[0] != staff.name:
                    raise PolicyError(f"Staff alias {key!r} is ambiguous between {existing[0]} and {staff.name}")
                if key in rank_lookup:
                    raise PolicyError(f"{key!r} is both a player rank and a staff prefix")
                staff_lookup[squashed] = (staff.name, None if key == staff.name else key)
        if self.scuffed_rank not in by_name and self.allow_custom_prefix:
            raise PolicyError(f"SCUFFED rank {self.scuffed_rank!r} is not a configured rank")
        object.__setattr__(self, "_rank_lookup", rank_lookup)
        object.__setattr__(self, "_staff_lookup", staff_lookup)
        object.__setattr__(self, "_by_name", by_name)

    # --------------------------------------------------------------- queries
    def rank(self, name: str) -> RankDef | None:
        return self._by_name.get(normalise_prefix_text(name))

    def priority(self, name: str | None) -> int:
        """Priority of a canonical rank (0 when unknown/None)."""
        if name is None:
            return 0
        rank = self._by_name.get(normalise_prefix_text(name))
        return rank.priority if rank else 0

    def is_known_rank(self, name: str) -> bool:
        return normalise_prefix_text(name) in self._by_name

    def enabled_ranks(self) -> list[RankDef]:
        return [r for r in self.ranks if r.enabled]

    def rank_names(self) -> list[str]:
        return [r.name for r in self.ranks]

    def staff_names(self) -> list[str]:
        return [s.name for s in self.staff]

    def canonical_rank(self, text: str) -> str | None:
        """Map a rank name or alias to its canonical rank, or None."""
        hit = self._rank_lookup.get(normalise_prefix_text(text))
        return hit[0] if hit else None

    def canonical_staff(self, text: str) -> str | None:
        hit = self._staff_lookup.get(squash_spaces(normalise_prefix_text(text)))
        return hit[0] if hit else None

    def higher_than(self, a: str | None, b: str | None) -> bool:
        return self.priority(a) > self.priority(b)

    def max_rank(self, names: list[str | None]) -> str | None:
        best: str | None = None
        for name in names:
            if name is None:
                continue
            if best is None or self.priority(name) > self.priority(best):
                best = name
        return best

    def ranks_strictly_above(self, threshold: str) -> list[str]:
        p = self.priority(threshold)
        return [r.name for r in self.ranks if r.priority > p]

    def sort_ranks(self, names: list[str]) -> list[str]:
        return sorted(set(names), key=self.priority)

    # ------------------------------------------------------------- matching
    def match_prefix(self, raw_text: str | None, *, unreadable: bool = False) -> PrefixMatch:
        """Classify a raw prefix string.

        Order of checks (mandated by the specification):
          1. exact canonical player rank or seasonal alias
          2. exact staff prefix (spacing-insensitive, ``+`` sensitive)
          3. probable misread of a known rank/staff prefix -> review, never custom
          4. plausible custom prefix -> SCUFFED candidate
        """
        raw = raw_text or ""
        norm = normalise_prefix_text(raw)
        if unreadable or not norm:
            return PrefixMatch(PrefixKind.UNREADABLE if (unreadable or raw.strip()) else PrefixKind.NONE, raw, norm, confidence=0.0)

        hit = self._rank_lookup.get(norm)
        if hit:
            canonical, alias = hit
            kind = PrefixKind.SEASONAL_ALIAS if alias else PrefixKind.PLAYER_RANK
            return PrefixMatch(kind, raw, norm, canonical=canonical, matched_alias=alias)

        staff_hit = self._staff_lookup.get(squash_spaces(norm))
        if staff_hit:
            canonical, alias = staff_hit
            return PrefixMatch(PrefixKind.STAFF, raw, norm, canonical=canonical, matched_alias=alias)

        misread = self._find_misread(norm)
        if misread is not None:
            target, kind = misread
            if target == AMBIGUOUS:
                return PrefixMatch(PrefixKind.PROBABLE_MISREAD, raw, norm, misread_of=None, misread_kind=None, confidence=0.3, notes=f"{norm!r} resembles several known prefixes; review required")
            return PrefixMatch(
                PrefixKind.PROBABLE_MISREAD,
                raw,
                norm,
                misread_of=target,
                misread_kind=kind,
                confidence=0.5,
                notes=f"{norm!r} looks like a misread of {target}",
            )

        if self.allow_custom_prefix and _plausible_custom_prefix(norm):
            return PrefixMatch(PrefixKind.CUSTOM, raw, norm, canonical=self.scuffed_rank, confidence=0.9)

        return PrefixMatch(PrefixKind.UNREADABLE, raw, norm, confidence=0.0, notes="not a plausible prefix")

    def _find_misread(self, norm: str) -> tuple[str, PrefixKind] | None:
        """Return (canonical, kind) when ``norm`` is a probable OCR misread."""
        plus = has_plus(norm)
        folded = confusion_fold(norm)
        candidates: list[tuple[str, str, PrefixKind]] = []  # (key, canonical, kind)
        for key, (canonical, _alias) in self._rank_lookup.items():
            candidates.append((key, canonical, PrefixKind.PLAYER_RANK))
        for key, (canonical, _alias) in self._staff_lookup.items():
            candidates.append((key, canonical, PrefixKind.STAFF))

        scored: list[tuple[int, str, PrefixKind]] = []
        for key, canonical, kind in candidates:
            # ``+`` is a hard discriminator: IMMORTAL+ must never fuzz into IMMORTAL.
            if has_plus(key) != plus:
                continue
            key_cmp = squash_spaces(key) if kind == PrefixKind.STAFF else key
            norm_cmp = squash_spaces(norm) if kind == PrefixKind.STAFF else norm
            if folded == confusion_fold(key_cmp) or confusion_fold(norm_cmp) == confusion_fold(key_cmp):
                return canonical, kind
            if len(key_cmp) >= 3 and abs(len(norm_cmp) - len(key_cmp)) <= 2:
                distance = levenshtein(confusion_fold(norm_cmp), confusion_fold(key_cmp))
                allowed = 1 if len(key_cmp) < 7 else 2
                if distance <= allowed:
                    scored.append((distance, canonical, kind))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0])
        best_distance = scored[0][0]
        winners = {(c, k) for d, c, k in scored if d == best_distance}
        if len(winners) == 1:
            canonical, kind = next(iter(winners))
            return canonical, kind
        # Several known prefixes are equally plausible: do not guess.
        return AMBIGUOUS, PrefixKind.PROBABLE_MISREAD

    # ------------------------------------------------------------- export
    def to_config(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ranks = [{"name": r.name, "priority": r.priority, "aliases": list(r.aliases), "enabled": r.enabled} for r in self.ranks]
        staff = [{"name": s.name, "aliases": list(s.aliases)} for s in self.staff]
        return ranks, staff

    def fingerprint(self) -> str:
        import hashlib
        import json

        ranks, staff = self.to_config()
        payload = json.dumps({"ranks": ranks, "staff": staff, "scuffed": self.scuffed_rank, "custom": self.allow_custom_prefix}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _plausible_custom_prefix(norm: str) -> bool:
    """A custom SCUFFED prefix is a short word or phrase, not arbitrary chat text."""
    if not (2 <= len(norm) <= 24):
        return False
    if norm.count(" ") > 2:
        return False
    letters = sum(ch.isalpha() for ch in norm)
    if letters < max(2, int(len(norm.replace(" ", "")) * 0.6)):
        return False
    if any(ch in norm for ch in ":@#/\\"):
        return False
    return True


def default_policy(version: int = 0) -> RankPolicy:
    from rankrecovery.config.defaults import DEFAULT_PLAYER_RANKS, DEFAULT_STAFF_PREFIXES

    return RankPolicy.from_config(DEFAULT_PLAYER_RANKS, DEFAULT_STAFF_PREFIXES, version=version)
