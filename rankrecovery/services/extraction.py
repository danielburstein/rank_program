"""Run extraction on stored images and persist every result."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from rankrecovery.core.enums import DetectionStatus, ExtractorType, ReviewStatus
from rankrecovery.core.parsing import ParsedDetection, RowParser
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
from rankrecovery.services.flags import upsert_flags
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

