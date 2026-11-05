"""Reconciliation engine: from retained evidence to an auditable rank summary.

The engine never mutates evidence. Every eligibility decision carries a
reason so the GUI can show exactly why an observation did or did not count.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rankrecovery.core.enums import AiStatus, DateStatus, ReviewStatus, SourceType, VerificationStatus
from rankrecovery.core.evidence import (
    UNKNOWN_SUBMITTER_KEY,
    PlayerEvidence,
    RankObservationFact,
    VerificationPolicy,
)
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.ranks import RankPolicy


@dataclass(slots=True)
class EvidenceAssessment:
    observation_id: int
    rank: str
    source: SourceType
    image_id: int | None
    submitter_key: str
    submitter_name: str
    duplicate_group: str | None
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    counted_as_source: bool = False
    source_key: str | None = None
    raw_text: str = ""
    confidence: float = 1.0
    manually_verified: bool = False
    basis: str = "prefix"


@dataclass(slots=True)
class RankCandidate:
    rank: str
    priority: int
    required_sources: int
    counted_sources: int
    source_keys: list[str]
    status: VerificationStatus
    evidence: list[EvidenceAssessment]
    confirmed_by: list[str] = field(default_factory=list)  # "csv", "manual_override", "screenshots", "waiver"
    fallback_rank: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.status in (VerificationStatus.CONFIRMED, VerificationStatus.WAIVED)

    @property
    def progress(self) -> str:
        return f"{self.counted_sources}/{self.required_sources}"


@dataclass(slots=True)
class PlayerSummary:
    player_id: int
    observed_ranks: list[str]
    highest_observed: str | None
    highest_confirmed: str | None
    effective_rank: str | None
    candidates: list[RankCandidate]
    staff_ranks: list[str]
    custom_prefixes: list[str]
    flags: list[FlagInstance]
    clean_rank_images: int
    distinct_submitters: int
    ai_rank_images: int
    csv_records: int
    manual_override_active: bool
    reasoning: list[str]
    policy_fingerprint: str
    provisional: list[RankCandidate] = field(default_factory=list)  # provisional candidates ABOVE the effective rank

    @property
    def verification_ok(self) -> bool:
        return self.effective_rank is not None

    @property
    def flag_codes(self) -> list[str]:
        return [f.code for f in self.flags]

    def compact(self) -> str:
        parts = [", ".join(self.observed_ranks) or "no ranks"]
        for c in self.provisional:
            parts.append(f"{c.rank} provisional {c.progress}")
        parts.append(f"Effective {self.effective_rank or '-'}")
        if self.staff_ranks:
            parts.append("Staff " + ", ".join(self.staff_ranks))
        for f in self.flags:
            parts.append(f.render())
        return " : ".join(parts)


class ReconciliationEngine:
    def __init__(self, policy: RankPolicy, verification: VerificationPolicy):
        self.policy = policy
        self.verification = verification

    # ---------------------------------------------------------------- public
    def summarise(self, evidence: PlayerEvidence) -> PlayerSummary:
        pol = self.policy
        reasoning: list[str] = []
        flags: list[FlagInstance] = []

        active = [o for o in evidence.rank_observations if o.is_active and pol.is_known_rank(o.rank)]
        for o in evidence.rank_observations:
            if o.is_active and not pol.is_known_rank(o.rank):
                flags.append(FlagInstance("AMBIGUOUS_PREFIX", f"observation {o.id} names unknown rank {o.rank!r}"))

        assessments = [self._assess(o) for o in active]
        by_rank: dict[str, list[EvidenceAssessment]] = defaultdict(list)
        obs_by_id = {o.id: o for o in active}
        for a in assessments:
            by_rank[a.rank].append(a)

        observed = pol.sort_ranks([o.rank for o in active])
        highest_observed = pol.max_rank(observed)

        candidates: list[RankCandidate] = []
        confirmed_ranks: list[str] = []
        fallback_ranks: list[str] = []
        manual_override_active = False
        waived = {w.rank: w for w in evidence.waivers}

        for rank in sorted(by_rank, key=pol.priority, reverse=True):
            ev = by_rank[rank]
            cand = self._build_candidate(rank, ev, obs_by_id, waived.get(rank) is not None)
            if cand.confirmed:
                confirmed_ranks.append(rank)
                if "manual_override" in cand.confirmed_by:
                    manual_override_active = True
                if cand.status == VerificationStatus.WAIVED:
                    flags.append(FlagInstance("VERIFICATION_WAIVED", f"{rank}: {waived[rank].reason} (by {waived[rank].created_by})"))
            elif cand.status == VerificationStatus.PROVISIONAL and cand.fallback_rank:
                fallback_ranks.append(cand.fallback_rank)
            candidates.append(cand)

        highest_confirmed = pol.max_rank(confirmed_ranks)
        effective = pol.max_rank(confirmed_ranks + fallback_ranks)
        if effective and effective not in confirmed_ranks:
            reasoning.append(f"effective rank {effective} comes from the single-source fallback rule")

        # Provisional candidates matter only when they would raise the effective rank.
        provisional: list[RankCandidate] = []
        for cand in candidates:
            if cand.status != VerificationStatus.PROVISIONAL or not pol.higher_than(cand.rank, effective):
                continue
            provisional.append(cand)
            flags.append(FlagInstance("VERIFICATION_INSUFFICIENT_SOURCES", f"{cand.rank} {cand.progress} sources"))
            if cand.fallback_rank:
                reasoning.append(f"{cand.rank} has {cand.progress} sources; eligible evidence counts as {cand.fallback_rank}-level")

        # ---- staff / custom
        staff_ranks = sorted({s.staff_name for s in evidence.staff_observations if s.is_active})
        custom_prefixes = sorted({o.custom_prefix for o in active if o.custom_prefix})

        # ---- flags about AI / dates / confidence / staff
        flags.extend(self._evidence_flags(active, assessments, effective, highest_observed))
        if staff_ranks and not active:
            flags.append(FlagInstance("ONLY_STAFF_RANK_DETECTED", ", ".join(staff_ranks)))
        elif staff_ranks and active:
            flags.append(FlagInstance("STAFF_AND_PLAYER_RANK_FOUND", ", ".join(staff_ranks)))
        if evidence.is_bedrock:
            flags.append(FlagInstance("BEDROCK_PLAYER", evidence.usernames[0] if evidence.usernames else ""))
        if evidence.name_only_images and not active and not staff_ranks:
            flags.append(FlagInstance("NAME_READ_RANK_NOT_FOUND", f"{len(evidence.name_only_images)} image(s) show the name without a rank"))
        if any((o.image and not o.image.submitter_known) for o in active) or any(not i.submitter_known for i in evidence.name_only_images):
            flags.append(FlagInstance("SUBMITTER_UNKNOWN", "at least one image has no recorded submitter"))
        if evidence.unresolved_review_count or any(o.review_status == ReviewStatus.PENDING for o in active):
            pending = evidence.unresolved_review_count + sum(1 for o in active if o.review_status == ReviewStatus.PENDING)
            flags.append(FlagInstance("MANUAL_REVIEW_REQUIRED", f"{pending} detection(s) await review"))
        for code in evidence.identity_flags:
            flags.append(FlagInstance(code))
        if manual_override_active:
            flags.append(FlagInstance("MANUAL_OVERRIDE", "an explicit manual rank override is active"))
        if not active and not staff_ranks:
            flags.append(FlagInstance("NO_RANK_EVIDENCE"))
        flags.extend(self._conflict_flags(active))

        # ---- counts
        clean_images = {a.image_id for a in assessments if a.image_id is not None and a.eligible}
        ai_images = {o.image.image_id for o in active if o.image and o.image.ai_status in (AiStatus.AI_INDICATOR_FOUND, AiStatus.MANUALLY_FLAGGED)}
        submitters = {a.submitter_key for a in assessments if a.image_id is not None and a.eligible}
        csv_records = sum(1 for o in active if o.is_csv)

        return PlayerSummary(
            player_id=evidence.player_id,
            observed_ranks=observed,
            highest_observed=highest_observed,
            highest_confirmed=highest_confirmed,
            effective_rank=effective,
            candidates=candidates,
            staff_ranks=staff_ranks,
            custom_prefixes=custom_prefixes,
            flags=_dedupe_flags(flags),
            clean_rank_images=len(clean_images),
            distinct_submitters=len(submitters),
            ai_rank_images=len(ai_images),
            csv_records=csv_records,
            manual_override_active=manual_override_active,
            reasoning=reasoning,
            policy_fingerprint=pol.fingerprint(),
            provisional=provisional,
        )

    # ------------------------------------------------------------ assessment
    def _assess(self, o: RankObservationFact) -> EvidenceAssessment:
        ver = self.verification
        reasons: list[str] = []
        img = o.image
        a = EvidenceAssessment(
            observation_id=o.id,
            rank=o.rank,
            source=o.source,
            image_id=img.image_id if img else None,
            submitter_key=img.submitter_key if img else _non_image_source_key(o),
            submitter_name=img.submitter_name if img else _non_image_source_name(o),
            duplicate_group=img.duplicate_group if img else None,
            eligible=True,
            raw_text=o.raw_text,
            confidence=o.confidence,
            manually_verified=o.manually_verified,
            basis=o.basis,
        )
        if o.review_status == ReviewStatus.REJECTED:
            reasons.append("rejected in review")
        if o.review_status == ReviewStatus.PENDING and not o.manually_verified:
            reasons.append("manual review pending")
        if img is not None:
            if img.ai_status in (AiStatus.AI_INDICATOR_FOUND, AiStatus.MANUALLY_FLAGGED):
                reasons.append(f"AI indicator on image ({img.ai_details or img.ai_status.value})")
            elif img.ai_status == AiStatus.AI_FILENAME_INDICATOR and ver.weak_ai_indicator_makes_ineligible:
                reasons.append("filename AI indicator (policy excludes)")
            if img.date_status == DateStatus.OUT_OF_DATE and ver.exclude_out_of_date:
                reasons.append(f"out of date ({img.date_details or 'after cutoff'})")
            if o.confidence < ver.min_confidence and not o.manually_verified:
                reasons.append(f"confidence {o.confidence:.2f} below minimum {ver.min_confidence:.2f}")
        a.eligible = not reasons
        a.reasons = reasons
        return a

    def _required_sources(self, rank: str) -> int:
        ver = self.verification
        if rank in ver.per_rank_thresholds:
            return max(1, int(ver.per_rank_thresholds[rank]))
        if ver.require_independent_submitters and self.policy.priority(rank) > self.policy.priority(ver.threshold_rank):
            return max(1, ver.required_sources_above_threshold)
        return 1

    def _fallback_rank(self, rank: str, counted: int) -> str | None:
        """Highest rank below ``rank`` whose requirement is met by ``counted`` sources."""
        if counted < 1:
            return None
        best: str | None = None
        for r in self.policy.ranks:
            if not r.enabled or r.priority >= self.policy.priority(rank):
                continue
            if self._required_sources(r.name) <= counted and (best is None or r.priority > self.policy.priority(best)):
                best = r.name
        return best

    def _build_candidate(
        self,
        rank: str,
        evidence: list[EvidenceAssessment],
        obs_by_id: dict[int, RankObservationFact],
        waived: bool,
    ) -> RankCandidate:
        ver = self.verification
        confirmed_by: list[str] = []
        notes: list[str] = []
        required = self._required_sources(rank)

        # Authoritative sources first.
        for a in evidence:
            o = obs_by_id[a.observation_id]
            if not a.eligible:
                continue
            if o.is_csv and ver.csv_exempt_from_verification:
                a.counted_as_source = True
                a.source_key = a.submitter_key
                if "csv" not in confirmed_by:
                    confirmed_by.append("csv")
            elif o.is_manual_override:
                a.counted_as_source = True
                a.source_key = a.submitter_key
                if "manual_override" not in confirmed_by:
                    confirmed_by.append("manual_override")

        # Independent-source counting for screenshot (and non-exempt CSV) evidence.
        counted_keys = self._count_sources(evidence, obs_by_id)
        counted = len(counted_keys)

        if confirmed_by:
            status = VerificationStatus.CONFIRMED
            notes.append("confirmed by " + ", ".join(confirmed_by))
        elif counted >= required:
            status = VerificationStatus.CONFIRMED
            confirmed_by.append("screenshots")
        elif waived and counted >= 1:
            status = VerificationStatus.WAIVED
            confirmed_by.append("waiver")
        elif any(a.eligible for a in evidence):
            status = VerificationStatus.PROVISIONAL
        else:
            status = VerificationStatus.PROVISIONAL
            notes.append("no eligible sources: " + "; ".join(sorted({r for a in evidence for r in a.reasons})) )

        fallback = None
        if status == VerificationStatus.PROVISIONAL and counted >= 1:
            fallback = self._fallback_rank(rank, counted)

        return RankCandidate(
            rank=rank,
            priority=self.policy.priority(rank),
            required_sources=required,
            counted_sources=counted,
            source_keys=sorted(counted_keys),
            status=status,
            evidence=evidence,
            confirmed_by=confirmed_by,
            fallback_rank=fallback,
            notes=notes,
        )

    def _count_sources(self, evidence: list[EvidenceAssessment], obs_by_id: dict[int, RankObservationFact]) -> set[str]:
        """Count independent sources.

        Rules:
        * a source is a submitter, not an image; several images from one
          submitter count once;
        * all unknown-submitter images together count as at most one source;
        * duplicate content never adds a source even across submitters: a
          content group seen under several submitters is credited to nobody
          unless no member submitter has independent evidence, in which case
          the group counts once as a shared source;
        * CSV rows (when not exempt) count as their own source key.
        """
        ver = self.verification
        group_submitters: dict[str, set[str]] = defaultdict(set)
        group_assessments: dict[str, list[EvidenceAssessment]] = defaultdict(list)
        non_image_keys: set[str] = set()
        for a in evidence:
            if not a.eligible:
                continue
            o = obs_by_id[a.observation_id]
            if a.image_id is None:
                if o.is_csv and ver.csv_exempt_from_verification:
                    continue  # already authoritative
                if o.is_manual_override:
                    continue
                non_image_keys.add(a.submitter_key)
                a.counted_as_source = True
                a.source_key = a.submitter_key
                continue
            if a.submitter_key == UNKNOWN_SUBMITTER_KEY and not ver.unknown_submitter_counts_as_one_source:
                a.reasons.append("unknown submitter does not count by policy")
                continue
            group = a.duplicate_group or f"image:{a.image_id}"
            group_submitters[group].add(a.submitter_key)
            group_assessments[group].append(a)

        credited: set[str] = set(non_image_keys)
        shared_groups: list[str] = []
        for group, submitters in group_submitters.items():
            if len(submitters) == 1:
                key = next(iter(submitters))
                credited.add(key)
                for a in group_assessments[group]:
                    a.counted_as_source = True
                    a.source_key = key
            else:
                shared_groups.append(group)
        for group in shared_groups:
            members = group_submitters[group]
            if members & credited:
                for a in group_assessments[group]:
                    a.reasons.append("duplicate of an image already credited to another submitter")
                continue
            key = f"shared:{group[:12]}"
            credited.add(key)
            for a in group_assessments[group]:
                a.counted_as_source = True
                a.source_key = key
        return credited

    # ----------------------------------------------------------------- flags
    def _evidence_flags(
        self,
        active: list[RankObservationFact],
        assessments: list[EvidenceAssessment],
        effective: str | None,
        highest_observed: str | None,
    ) -> list[FlagInstance]:
        pol = self.policy
        flags: list[FlagInstance] = []
        image_obs = [o for o in active if o.image is not None]
        if image_obs:
            ai_obs = [o for o in image_obs if o.image and o.image.ai_status in (AiStatus.AI_INDICATOR_FOUND, AiStatus.MANUALLY_FLAGGED)]
            if ai_obs and len({o.image.image_id for o in ai_obs if o.image}) == len({o.image.image_id for o in image_obs if o.image}):
                flags.append(FlagInstance("AI_DETECTED_FOR_ALL_RANK_IMAGES", f"{len(ai_obs)} observation(s)"))
            higher_ai = pol.max_rank([o.rank for o in ai_obs if pol.higher_than(o.rank, effective)])
            if higher_ai:
                flags.append(FlagInstance("AI_DETECTED_HIGHER_RANK", higher_ai))
            for o in image_obs:
                assert o.image is not None
                if o.image.date_status == DateStatus.OUT_OF_DATE:
                    flags.append(FlagInstance("OUT_OF_DATE", f"image {o.image.image_id}: {o.image.date_details or 'after cutoff'}", evidence_ref=f"image:{o.image.image_id}"))
                elif o.image.date_status == DateStatus.UNKNOWN:
                    flags.append(FlagInstance("DATE_UNKNOWN", f"image {o.image.image_id}", evidence_ref=f"image:{o.image.image_id}"))
                if o.image.ai_status == AiStatus.AI_FILENAME_INDICATOR:
                    flags.append(FlagInstance("AI_FILENAME_INDICATOR", f"image {o.image.image_id}: {o.image.ai_details}", evidence_ref=f"image:{o.image.image_id}"))
        for a in assessments:
            if a.image_id is not None and a.confidence < self.verification.min_confidence and not a.manually_verified:
                flags.append(FlagInstance("LOW_OCR_CONFIDENCE", f"observation {a.observation_id} confidence {a.confidence:.2f}", evidence_ref=f"observation:{a.observation_id}"))
        return flags

    def _conflict_flags(self, active: list[RankObservationFact]) -> list[FlagInstance]:
        """Different player ranks for the same player inside one image is a contradiction."""
        flags: list[FlagInstance] = []
        per_image: dict[int, set[str]] = defaultdict(set)
        for o in active:
            if o.image is not None and o.basis == "prefix":
                per_image[o.image.image_id].add(o.rank)
        for image_id, ranks in per_image.items():
            if len(ranks) > 1:
                flags.append(FlagInstance("CONFLICTING_RANK_EVIDENCE", f"image {image_id} shows {', '.join(sorted(ranks))}", evidence_ref=f"image:{image_id}"))
        return flags


def _non_image_source_key(o: RankObservationFact) -> str:
    if o.is_csv:
        return f"csv:{o.csv_batch_key or o.csv_row_id}"
    if o.is_manual_override:
        return f"manual:{o.manual_assertion_id}"
    return f"other:{o.id}"


def _non_image_source_name(o: RankObservationFact) -> str:
    if o.is_csv:
        return "trusted purchase CSV"
    if o.is_manual_override:
        return "manual override"
    return "other"


def _dedupe_flags(flags: list[FlagInstance]) -> list[FlagInstance]:
    seen: set[tuple[str, str, str | None]] = set()
    out: list[FlagInstance] = []
    for f in flags:
        key = (f.code, f.details, f.evidence_ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
