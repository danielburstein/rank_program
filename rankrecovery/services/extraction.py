"""Run extraction on stored images and persist every result."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from rankrecovery.core.enums import (
    DetectionStatus,
    ExtractorType,
    LeadingIcon,
    RegionKind,
    ReviewStatus,
    TrailingIcon,
)
from rankrecovery.core.parsing import ParsedDetection, RawRow, RowParser
from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import (
    Detection,
    ExtractionRun,
    PlayerSummary,
    RankObservation,
    SourceImage,
    StaffObservation,
)
from rankrecovery.extraction.base import DetectedRegion, ExtractionOutput, ExtractorUnavailable, TextExtractor
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import sync_auto_flags, upsert_flags
from rankrecovery.services.players import add_alias, get_or_create_player

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExtractionSummary:
    image_id: int
    run_id: int | None
    cached: bool = False
    detections: int = 0
    rank_observations: int = 0
    staff_observations: int = 0
    players_touched: list[int] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class ExtractionService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def parser(self) -> RowParser:
        s = self.ctx.settings
        return RowParser(
            self.ctx.policy(),
            bedrock_prefix=str(s.get("lookup.bedrock_prefix", ".")),
            review_confidence=float(s.get("extraction.review_confidence", 0.8)),
            min_confidence=float(s.get("extraction.min_confidence", 0.6)),
        )

    # --------------------------------------------------------------- extract
    def extract_image(
        self,
        image_id: int,
        *,
        extractor: TextExtractor | None = None,
        extractor_override: str | None = None,
        purpose: str = "import",
        force: bool = False,
        regions: list[DetectedRegion] | None = None,
        job_id: int | None = None,
        actor: str = "system",
    ) -> ExtractionSummary:
        from rankrecovery.extraction.pipeline import run_extraction
        from rankrecovery.services.images import ImageService

        if extractor is None:
            extractor = self.ctx.build_extractor(extractor_override)
        fingerprint = extractor.settings.fingerprint()

        with self.ctx.db.read_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                raise KeyError(image_id)
            filename = image.original_filename
            if not force and bool(self.ctx.settings.get("extraction.cache_results", True)) and regions is None:
                cached = s.execute(
                    select(ExtractionRun).where(ExtractionRun.image_id == image_id, ExtractionRun.settings_fingerprint == fingerprint, ExtractionRun.status == "ok", ExtractionRun.purpose == "import")
                ).scalars().first()
                if cached is not None:
                    return ExtractionSummary(image_id, cached.id, cached=True, detections=len(cached.detections))

        content = ImageService(self.ctx).get_bytes(image_id)
        if content is None:
            return ExtractionSummary(image_id, None, error="image bytes unavailable")

        started = datetime.now(UTC)
        try:
            result = run_extraction(content, extractor, self.parser(), filename=filename, regions=regions)
            output: ExtractionOutput = result.output
            detections: list[ParsedDetection] = result.detections
        except ExtractorUnavailable:
            raise
        except Exception as exc:  # engine crash: record the failed run, keep going
            log.exception("extraction failed for image %s", image_id)
            with self.ctx.db.write_session() as s:
                run = ExtractionRun(image_id=image_id, extractor_type=extractor.settings.extractor.value, engine=extractor.settings.engine, model=extractor.settings.model, settings_fingerprint=fingerprint, status="error", error=str(exc), purpose=purpose, job_id=job_id, started_at=started, finished_at=datetime.now(UTC))
                s.add(run)
                s.flush()
                return ExtractionSummary(image_id, run.id, error=str(exc))

        return self.persist(image_id, output, detections, purpose=purpose, job_id=job_id, started=started, actor=actor)

    # --------------------------------------------------------------- persist
    def persist(self, image_id: int, output: ExtractionOutput, detections: list[ParsedDetection], *, purpose: str, job_id: int | None, started: datetime, actor: str = "system") -> ExtractionSummary:
        policy = self.ctx.policy()
        summary = ExtractionSummary(image_id, None, warnings=list(output.warnings))
        with self.ctx.db.write_session() as s:
            image = s.get(SourceImage, image_id)
            if image is None:
                raise KeyError(image_id)
            run = ExtractionRun(
                image_id=image_id,
                extractor_type=output.extractor.value,
                engine=output.engine,
                model=output.model,
                version=output.version,
                settings_fingerprint=output.settings_fingerprint,
                regions=[{"kind": r.kind.value, "bbox": list(r.bbox), "columns": [list(c) for c in r.columns], "confidence": r.confidence} for r in output.regions],
                raw_output=output.raw_output,
                full_text=output.full_text,
                words=output.words,
                status="ok" if output.ok else "error",
                error=output.error or "",
                warnings=list(output.warnings),
                cost=output.cost,
                purpose=purpose,
                job_id=job_id,
                started_at=started,
                finished_at=datetime.now(UTC),
            )
            s.add(run)
            s.flush()
            summary.run_id = run.id
            if output.error:
                summary.error = output.error
            image.last_extraction_run_id = run.id
            if output.full_text:
                image.extracted_text = (image.extracted_text + "\n" if image.extracted_text else "") + output.full_text
            touched: set[int] = set()

            for d in detections:
                row = self._detection_row(run.id, image_id, d, actor=actor)
                s.add(row)
                s.flush()
                summary.detections += 1
                upsert_flags(s, "detection", row.id, d.flags, actor=actor)
                if d.status in (DetectionStatus.PARTIAL, DetectionStatus.UNREADABLE) or not d.normalised_username:
                    continue
                if not d.username_valid and d.status != DetectionStatus.NEEDS_REVIEW:
                    continue
                player, _created = get_or_create_player(s, d.normalised_username, is_bedrock=d.is_bedrock)
                add_alias(s, player, d.normalised_username, source="extraction")
                row.player_id = player.id
                touched.add(player.id)
                if d.status == DetectionStatus.CONTEXT_ONLY:
                    continue
                review = ReviewStatus.PENDING if d.needs_review else ReviewStatus.NOT_REQUIRED
                for rank, basis in d.all_rank_observations:
                    s.add(
                        RankObservation(
                            player_id=player.id,
                            detection_id=row.id,
                            image_id=image_id,
                            source_type=d.source_type.value,
                            basis=basis,
                            raw_text=(d.raw_prefix if basis == "prefix" else f"{d.trailing_icon.value} icon")[:128],
                            rank_canonical=rank,
                            rank_priority=policy.priority(rank),
                            custom_prefix=d.custom_prefix_text if basis == "prefix" else None,
                            credibility=d.credibility.value,
                            confidence=d.confidence,
                            review_status=review.value,
                            created_by=actor,
                        )
                    )
                    summary.rank_observations += 1
                if d.staff_observation:
                    s.add(
                        StaffObservation(
                            player_id=player.id,
                            detection_id=row.id,
                            image_id=image_id,
                            raw_text=d.raw_prefix[:128],
                            staff_canonical=d.staff_observation,
                            has_yinyang=bool(d.yinyang_evidence),
                            review_status=review.value,
                            created_by=actor,
                        )
                    )
                    summary.staff_observations += 1
            for pid in touched:
                existing = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if existing is not None:
                    existing.stale = True
            summary.players_touched = sorted(touched)
        return summary

    @staticmethod
    def _detection_row(run_id: int | None, image_id: int, d: ParsedDetection, *, actor: str, manual: bool = False) -> Detection:
        return Detection(
            run_id=run_id,
            image_id=image_id,
            region=d.region.value,
            source_type=d.source_type.value,
            credibility=d.credibility.value,
            raw_text=d.raw_text,
            raw_prefix=d.raw_prefix[:128],
            normalised_prefix=d.normalised_prefix[:128],
            prefix_kind=d.prefix_match.kind.value,
            raw_username=d.raw_username[:64],
            normalised_username=d.normalised_username[:64],
            username_key=normalise_username_key(d.normalised_username)[:64],
            username_valid=d.username_valid,
            is_bedrock=d.is_bedrock,
            partial=d.partial,
            unreadable_username=d.unreadable_username,
            status=d.status.value,
            confidence=d.confidence,
            bbox=list(d.bbox) if d.bbox else None,
            prefix_bbox=list(d.prefix_bbox) if d.prefix_bbox else None,
            username_bbox=list(d.username_bbox) if d.username_bbox else None,
            leading_icon=d.leading_icon.value,
            trailing_icon=d.trailing_icon.value,
            rank_canonical=d.rank_observation,
            staff_canonical=d.staff_observation,
            custom_prefix=d.custom_prefix_text,
            trailing_icon_rank=d.trailing_icon_rank,
            yinyang_evidence=d.yinyang_evidence,
            misread_of=d.prefix_match.misread_of,
            needs_review=d.needs_review,
            notes=list(d.notes),
            manual=manual,
            created_by=actor,
        )

    # ------------------------------------------------------------- re-parse
    def reparse_detections(self, image_ids: list[int] | None = None, *, actor: str = "reparse") -> dict[str, int]:
        """Re-classify stored detections under the current rank policy.

        The raw text, boxes and confidence recorded at extraction time are
        kept; only the derived classification (rank, staff, custom prefix,
        review need) and the automatic observations are rebuilt. Detections a
        person already accepted or rejected are left exactly as they are.
        """
        parser = self.parser()
        policy = self.ctx.policy()
        counts = {"checked": 0, "changed": 0, "skipped_reviewed": 0, "players_touched": 0}
        touched: set[int] = set()
        with self.ctx.db.write_session() as s:
            stmt = select(Detection).where(Detection.superseded_by_id.is_(None), Detection.manual.is_(False))
            if image_ids:
                stmt = stmt.where(Detection.image_id.in_(image_ids))
            for row in s.execute(stmt.order_by(Detection.id)).scalars():
                counts["checked"] += 1
                if row.status in (DetectionStatus.ACCEPTED.value, DetectionStatus.REJECTED.value):
                    counts["skipped_reviewed"] += 1
                    continue
                raw = RawRow(
                    text=row.raw_text,
                    region=RegionKind(row.region) if row.region in RegionKind._value2member_map_ else RegionKind.UNKNOWN,
                    bbox=tuple(row.bbox) if row.bbox else None,  # type: ignore[arg-type]
                    prefix_text=row.raw_prefix,
                    username_text=row.raw_username,
                    prefix_bbox=tuple(row.prefix_bbox) if row.prefix_bbox else None,  # type: ignore[arg-type]
                    username_bbox=tuple(row.username_bbox) if row.username_bbox else None,  # type: ignore[arg-type]
                    leading_icon=LeadingIcon(row.leading_icon) if row.leading_icon in LeadingIcon._value2member_map_ else LeadingIcon.NONE,
                    trailing_icon=TrailingIcon(row.trailing_icon) if row.trailing_icon in TrailingIcon._value2member_map_ else TrailingIcon.NONE,
                    confidence=row.confidence,
                    prefix_unreadable=(row.prefix_kind == "unreadable" and not row.raw_prefix),
                    username_unreadable=row.unreadable_username,
                    username_cut_off=row.partial,
                )
                d = parser.parse(raw)
                if d is None:
                    continue
                before = (row.prefix_kind, row.rank_canonical, row.staff_canonical, row.custom_prefix, row.trailing_icon_rank, row.needs_review, row.status)
                after = (d.prefix_match.kind.value, d.rank_observation, d.staff_observation, d.custom_prefix_text, d.trailing_icon_rank, d.needs_review, d.status.value)
                if before == after:
                    continue
                counts["changed"] += 1
                row.normalised_prefix = d.normalised_prefix[:128]
                row.prefix_kind = d.prefix_match.kind.value
                row.rank_canonical = d.rank_observation
                row.staff_canonical = d.staff_observation
                row.custom_prefix = d.custom_prefix_text
                row.trailing_icon_rank = d.trailing_icon_rank
                row.yinyang_evidence = d.yinyang_evidence
                row.misread_of = d.prefix_match.misread_of
                row.needs_review = d.needs_review
                row.status = d.status.value
                row.notes = list(d.notes) + [f"re-parsed under policy {policy.fingerprint()}"]
                sync_auto_flags(s, "detection", row.id, d.flags, actor=actor)
                # retire the automatic observations of this detection (kept for history) and rebuild them
                reason = f"superseded by re-parse under policy {policy.fingerprint()}"
                for obs in s.execute(select(RankObservation).where(RankObservation.detection_id == row.id, RankObservation.is_active.is_(True))).scalars():
                    if not obs.manually_verified:
                        obs.is_active = False
                        obs.deactivated_reason = reason
                for st in s.execute(select(StaffObservation).where(StaffObservation.detection_id == row.id, StaffObservation.is_active.is_(True))).scalars():
                    st.is_active = False
                s.flush()
                if row.player_id and d.status not in (DetectionStatus.PARTIAL, DetectionStatus.UNREADABLE, DetectionStatus.CONTEXT_ONLY):
                    review = ReviewStatus.PENDING if d.needs_review else ReviewStatus.NOT_REQUIRED
                    for rank, basis in d.all_rank_observations:
                        s.add(RankObservation(player_id=row.player_id, detection_id=row.id, image_id=row.image_id, source_type=d.source_type.value, basis=basis, raw_text=(d.raw_prefix if basis == "prefix" else f"{d.trailing_icon.value} icon")[:128], rank_canonical=rank, rank_priority=policy.priority(rank), custom_prefix=d.custom_prefix_text if basis == "prefix" else None, credibility=d.credibility.value, confidence=d.confidence, review_status=review.value, created_by=actor))
                    if d.staff_observation:
                        s.add(StaffObservation(player_id=row.player_id, detection_id=row.id, image_id=row.image_id, raw_text=d.raw_prefix[:128], staff_canonical=d.staff_observation, has_yinyang=bool(d.yinyang_evidence), review_status=review.value, created_by=actor))
                if row.player_id:
                    touched.add(row.player_id)
            for pid in touched:
                existing = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if existing is not None:
                    existing.stale = True
        counts["players_touched"] = len(touched)
        return counts

    # ------------------------------------------------------------ manual rows
    def add_manual_detection(self, image_id: int, d: ParsedDetection, *, actor: str, reason: str) -> ExtractionSummary:
        """Persist a human-drawn/corrected detection as its own run (purpose=manual)."""
        output = ExtractionOutput(rows=[], regions=[], extractor=ExtractorType.MANUAL, engine="manual", model="", version="1", raw_output=reason, full_text=f"{d.raw_prefix} {d.raw_username}".strip())
        return self.persist(image_id, output, [d], purpose="manual", job_id=None, started=datetime.now(UTC), actor=actor)

    # ---------------------------------------------------------------- queries
    def pending_images(self, *, fingerprint: str | None = None, image_ids: list[int] | None = None) -> list[int]:
        with self.ctx.db.read_session() as s:
            stmt = select(SourceImage.id).where(SourceImage.availability == "available")
            if image_ids:
                stmt = stmt.where(SourceImage.id.in_(image_ids))
            ids = list(s.execute(stmt.order_by(SourceImage.id)).scalars().all())
            if fingerprint is None:
                done = set(s.execute(select(ExtractionRun.image_id).where(ExtractionRun.status == "ok", ExtractionRun.purpose == "import")).scalars().all())
            else:
                done = set(s.execute(select(ExtractionRun.image_id).where(ExtractionRun.status == "ok", ExtractionRun.purpose == "import", ExtractionRun.settings_fingerprint == fingerprint)).scalars().all())
            return [i for i in ids if i not in done]

