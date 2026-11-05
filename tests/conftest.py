from __future__ import annotations

import pytest

from rankrecovery.core.enums import AiStatus, Credibility, DateStatus, ReviewStatus, SourceType
from rankrecovery.core.evidence import (
    ImageFacts,
    PlayerEvidence,
    RankObservationFact,
    StaffObservationFact,
    VerificationPolicy,
)
from rankrecovery.core.ranks import default_policy
from rankrecovery.core.reconciliation import ReconciliationEngine


@pytest.fixture
def policy():
    return default_policy()


@pytest.fixture
def engine(policy):
    return ReconciliationEngine(policy, VerificationPolicy())


class EvidenceBuilder:
    """Tiny helper to build PlayerEvidence in tests."""

    def __init__(self) -> None:
        self._next = 1
        self.ev = PlayerEvidence(player_id=1, usernames=["Player123"])

    def _id(self) -> int:
        self._next += 1
        return self._next

    def image(self, image_id: int, submitter: str | None, *, ai: bool = False, out_of_date: bool = False, group: str | None = None, date_unknown: bool = False) -> ImageFacts:
        return ImageFacts(
            image_id=image_id,
            submitter_key=f"submitter:{submitter}" if submitter else "unknown",
            submitter_name=submitter or "unknown",
            ai_status=AiStatus.AI_INDICATOR_FOUND if ai else AiStatus.NO_AI_INDICATOR_FOUND,
            date_status=DateStatus.OUT_OF_DATE if out_of_date else (DateStatus.UNKNOWN if date_unknown else DateStatus.OK),
            duplicate_group=group or f"sha:{image_id}",
            filename=f"img{image_id}.png",
            ai_details="OpenAI" if ai else "",
            date_details="after cutoff" if out_of_date else "",
        )

    def screenshot(self, rank: str, image: ImageFacts, *, confidence: float = 0.95, source: SourceType = SourceType.TAB, review: ReviewStatus = ReviewStatus.NOT_REQUIRED, custom: str | None = None, basis: str = "prefix", verified: bool = False) -> RankObservationFact:
        o = RankObservationFact(
            id=self._id(), rank=rank, raw_text=rank.title(), source=source, image=image,
            credibility=Credibility.HIGH, confidence=confidence, review_status=review, custom_prefix=custom, basis=basis, manually_verified=verified,
        )
        self.ev.rank_observations.append(o)
        return o

    def csv(self, rank: str, *, batch: str = "b1") -> RankObservationFact:
        o = RankObservationFact(id=self._id(), rank=rank, raw_text=rank, source=SourceType.TRUSTED_CSV, image=None, credibility=Credibility.AUTHORITATIVE, csv_row_id=self._id(), csv_batch_key=batch, basis="csv")
        self.ev.rank_observations.append(o)
        return o

    def manual_override(self, rank: str) -> RankObservationFact:
        o = RankObservationFact(id=self._id(), rank=rank, raw_text=rank, source=SourceType.MANUAL_OVERRIDE, image=None, credibility=Credibility.MANUAL, manual_assertion_id=self._id(), basis="manual")
        self.ev.rank_observations.append(o)
        return o

    def staff(self, name: str, image: ImageFacts, *, yinyang: bool = False) -> StaffObservationFact:
        s = StaffObservationFact(id=self._id(), staff_name=name, raw_text=name, image=image, has_yinyang=yinyang)
        self.ev.staff_observations.append(s)
        return s


@pytest.fixture
def builder():
    return EvidenceBuilder()
