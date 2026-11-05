"""Old-rank to new-rank conversion."""

from __future__ import annotations

from dataclasses import dataclass, field

from rankrecovery.core.ranks import RankPolicy
from rankrecovery.core.text import normalise_prefix_text


@dataclass(frozen=True)
class RankMapping:
    """Mapping from canonical old rank to new group name (exact spelling preserved)."""

    mapping: dict[str, str]
    version: int = 0

    @classmethod
    def from_config(cls, raw: dict[str, str], version: int = 0) -> RankMapping:
        return cls({normalise_prefix_text(k): str(v) for k, v in raw.items() if str(v).strip()}, version)

    def new_rank_for(self, old_rank: str | None, policy: RankPolicy | None = None) -> str | None:
        if old_rank is None:
            return None
        key = normalise_prefix_text(old_rank)
        if policy is not None:
            canonical = policy.canonical_rank(key)
            if canonical is not None:
                key = canonical
        return self.mapping.get(key)

    def fingerprint(self) -> str:
        import hashlib
        import json

        return hashlib.sha256(json.dumps(self.mapping, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FlagFilterPolicy:
    """Which flag codes are allowed to remain on an exported/converted player."""

    allowed_active_flags: frozenset[str] = frozenset()
    allow_resolved_flags: bool = False

    def blocking_flags(self, active: list[str], resolved: list[str]) -> list[str]:
        blocked = [code for code in active if code not in self.allowed_active_flags]
        if not self.allow_resolved_flags:
            blocked += [f"{code} (resolved)" for code in resolved if code not in self.allowed_active_flags]
        return blocked


@dataclass(frozen=True, slots=True)
class ConversionDecision:
    old_rank: str | None
    new_rank: str | None
    included: bool
    reasons: list[str] = field(default_factory=list)


def decide_conversion(
    *,
    effective_old_rank: str | None,
    verification_ok: bool,
    active_flags: list[str],
    resolved_flags: list[str],
    mapping: RankMapping,
    policy: RankPolicy,
    flag_policy: FlagFilterPolicy,
    manual_approval: bool = False,
) -> ConversionDecision:
    """Apply the rank mapping and the flag filter.

    Rank verification and flag filtering are separate policies: allowing a flag
    never bypasses a failed verification.
    """
    reasons: list[str] = []
    new_rank = mapping.new_rank_for(effective_old_rank, policy)
    if effective_old_rank is None:
        reasons.append("no eligible effective old rank")
    elif new_rank is None:
        reasons.append(f"old rank {effective_old_rank} has no new-rank mapping")
    if not verification_ok:
        reasons.append("rank verification not satisfied")
    blocking = flag_policy.blocking_flags(active_flags, resolved_flags)
    if blocking and not manual_approval:
        reasons.append("blocked by flags: " + ", ".join(sorted(blocking)))
    included = not reasons
    return ConversionDecision(effective_old_rank, new_rank, included, reasons)
