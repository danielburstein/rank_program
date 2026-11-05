"""Plain evidence records handed to the reconciliation engine.

The database layer converts ORM rows into these objects so the engine stays
pure, deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rankrecovery.core.enums import AiStatus, Credibility, DateStatus, ReviewStatus, SourceType

UNKNOWN_SUBMITTER_KEY = "unknown"


@dataclass(frozen=True, slots=True)
class ImageFacts:
    image_id: int
    submitter_key: str  # "submitter:<id>" or UNKNOWN_SUBMITTER_KEY
    submitter_name: str
    ai_status: AiStatus
    date_status: DateStatus
    duplicate_group: str  # sha256 or perceptual cluster key; identical content shares a group
    filename: str = ""
    ai_details: str = ""
    date_details: str = ""

    @property
    def submitter_known(self) -> bool:
        return self.submitter_key != UNKNOWN_SUBMITTER_KEY


@dataclass(frozen=True, slots=True)
class RankObservationFact:
    id: int
    rank: str  # canonical rank name
    raw_text: str
    source: SourceType
    image: ImageFacts | None
    credibility: Credibility
    confidence: float = 1.0
    review_status: ReviewStatus = ReviewStatus.NOT_REQUIRED
    manually_verified: bool = False
    is_active: bool = True
    csv_row_id: int | None = None
    csv_batch_key: str | None = None
    manual_assertion_id: int | None = None
    detection_id: int | None = None
    custom_prefix: str | None = None
    basis: str = "prefix"  # prefix | trailing_icon | csv | manual

    @property
    def is_csv(self) -> bool:
        return self.source == SourceType.TRUSTED_CSV

    @property
    def is_manual_override(self) -> bool:
        return self.source == SourceType.MANUAL_OVERRIDE

    @property
    def is_screenshot(self) -> bool:
        return self.image is not None


@dataclass(frozen=True, slots=True)
class StaffObservationFact:
    id: int
    staff_name: str
    raw_text: str
    image: ImageFacts | None
    has_yinyang: bool = False
    detection_id: int | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class Waiver:
    rank: str
    reason: str
    created_by: str


@dataclass(slots=True)
class PlayerEvidence:
    player_id: int
    usernames: list[str]
    is_bedrock: bool = False
    rank_observations: list[RankObservationFact] = field(default_factory=list)
    staff_observations: list[StaffObservationFact] = field(default_factory=list)
    waivers: list[Waiver] = field(default_factory=list)
    name_only_images: list[ImageFacts] = field(default_factory=list)  # username read but no prefix
    unresolved_review_count: int = 0
    identity_flags: list[str] = field(default_factory=list)  # e.g. IDENTITY_AMBIGUOUS from Mode 3


@dataclass(frozen=True)
class VerificationPolicy:
    require_independent_submitters: bool = True
    threshold_rank: str = "IMMORTAL"
    required_sources_above_threshold: int = 2
    per_rank_thresholds: dict[str, int] = field(default_factory=dict)
    csv_exempt_from_verification: bool = True
    exclude_out_of_date: bool = True
    min_confidence: float = 0.6
    unknown_submitter_counts_as_one_source: bool = True
    weak_ai_indicator_makes_ineligible: bool = False
