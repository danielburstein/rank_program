"""Mode 6: targeted username rescan and manual evidence correction."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select

from rankrecovery.core.enums import DetectionStatus, LeadingIcon, RegionKind, TrailingIcon
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.parsing import RawRow
from rankrecovery.core.text import confusion_fold, levenshtein
from rankrecovery.core.usernames import normalise_username_key
from rankrecovery.db.models import (
    Detection,
    Flag,
    ImagePath,
    ImportBatch,
    Player,
    PlayerAlias,
    SourceImage,
    Submitter,
)
from rankrecovery.extraction.base import DetectedRegion
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.flags import upsert_flags
from rankrecovery.services.search import detection_dict, image_dict

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RescanScope:
    mode: str = "all"  # all | player | flagged | batches | submitters | images
    player_id: int | None = None
    batch_ids: list[int] = field(default_factory=list)
    submitter_names: list[str] = field(default_factory=list)
    image_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class CandidateMatch:
    image_id: int
    filename: str
    score: float
    how: str  # exact_detection | fuzzy_detection | text_index | word_box
    detection_id: int | None = None
    bbox: list[int] | None = None
    rank: str | None = None
    staff: str | None = None
    status: str | None = None
    previous_flags: list[str] = field(default_factory=list)


class RescanService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ---------------------------------------------------------------- scope
    def scope_image_ids(self, scope: RescanScope) -> list[int]:
        with self.ctx.db.read_session() as s:
            if scope.mode == "images":
                return list(scope.image_ids)
            stmt = select(SourceImage.id).where(SourceImage.availability == "available")
            if scope.mode == "player" and scope.player_id:
                stmt = stmt.where(SourceImage.id.in_(select(Detection.image_id).where(Detection.player_id == scope.player_id)))
            elif scope.mode == "flagged":
                stmt = stmt.where(SourceImage.id.in_(select(Flag.entity_id).where(Flag.entity_type == "image", Flag.is_active.is_(True))))
            elif scope.mode == "batches":
                stmt = stmt.where(or_(SourceImage.import_batch_id.in_(scope.batch_ids), SourceImage.id.in_(select(ImagePath.image_id).where(ImagePath.import_batch_id.in_(scope.batch_ids)))))
            elif scope.mode == "submitters":
                keys = [normalise_username_key(n) for n in scope.submitter_names]
                stmt = stmt.where(SourceImage.submitter_id.in_(select(Submitter.id).where(Submitter.name_key.in_(keys))))
            return list(s.execute(stmt.order_by(SourceImage.id)).scalars().all())

    # ------------------------------------------------------------ candidates
    def find_candidates(self, username: str, scope: RescanScope, *, max_distance: int = 2) -> list[CandidateMatch]:
        """Cheap pass over indexed detections and extracted text."""
        key = normalise_username_key(username)
        folded = confusion_fold(key)
        image_ids = set(self.scope_image_ids(scope))
        out: dict[tuple[int, int | None], CandidateMatch] = {}
        with self.ctx.db.read_session() as s:
            det_stmt = select(Detection).where(Detection.superseded_by_id.is_(None))
            if image_ids:
                det_stmt = det_stmt.where(Detection.image_id.in_(image_ids))
            for d in s.execute(det_stmt).scalars():
                if not d.username_key:
                    continue
                how: str | None = None
                score = 0.0
                if d.username_key == key:
                    how, score = "exact_detection", 1.0
                else:
                    dist = levenshtein(confusion_fold(d.username_key), folded)
                    if dist <= max_distance and abs(len(d.username_key) - len(key)) <= max_distance:
                        how, score = "fuzzy_detection", max(0.0, 1.0 - dist / max(len(key), 1))
                    elif d.partial and key.startswith(d.username_key[:3]) and d.username_key and key.startswith(d.username_key.rstrip("…")):
                        how, score = "fuzzy_detection", 0.5
                if how:
                    img = s.get(SourceImage, d.image_id)
                    flags = list(s.execute(select(Flag.code).where(Flag.entity_type == "image", Flag.entity_id == d.image_id, Flag.is_active.is_(True))).scalars().all())
                    flags += [f"detection:{c}" for c in s.execute(select(Flag.code).where(Flag.entity_type == "detection", Flag.entity_id == d.id, Flag.is_active.is_(True))).scalars().all()]
                    out[(d.image_id, d.id)] = CandidateMatch(d.image_id, img.original_filename if img else "", score, how, d.id, d.bbox, d.rank_canonical, d.staff_canonical, d.status, flags)
            text_stmt = select(SourceImage).where(SourceImage.extracted_text.like(f"%{username}%"))
            if image_ids:
                text_stmt = text_stmt.where(SourceImage.id.in_(image_ids))
            for img in s.execute(text_stmt).scalars():
                if not any(k[0] == img.id for k in out):
                    flags = list(s.execute(select(Flag.code).where(Flag.entity_type == "image", Flag.entity_id == img.id, Flag.is_active.is_(True))).scalars().all())
                    out[(img.id, None)] = CandidateMatch(img.id, img.original_filename, 0.6, "text_index", previous_flags=flags)
        return sorted(out.values(), key=lambda c: (-c.score, c.image_id))

    # ---------------------------------------------------------------- rescan
    def run_rescan_job(self, job: Any, params: dict[str, Any]) -> dict[str, Any]:
        username = str(params.get("username", "")).strip()
        scope = RescanScope(mode=params.get("mode", "all"), player_id=params.get("player_id"), batch_ids=list(params.get("batch_ids") or []), submitter_names=list(params.get("submitter_names") or []), image_ids=list(params.get("image_ids") or []))
        full = bool(params.get("full", False))
        if full:
            ids = self.scope_image_ids(scope)
        else:
            ids = sorted({c.image_id for c in self.find_candidates(username, scope)})
            if not ids and scope.mode != "images":
                ids = self.scope_image_ids(scope)
        extractor = self.ctx.build_extractor(params.get("extractor"))
        svc = ExtractionService(self.ctx)
        key = normalise_username_key(username)
        findings: list[dict[str, Any]] = []
        checkpoint = job.checkpoint()
        done = set(checkpoint.get("done", []))
        touched: set[int] = set()
        for image_id in [i for i in ids if i not in done]:
            job.check_cancelled()
            try:
                result = svc.extract_image(image_id, extractor=extractor, purpose="rescan", force=True, job_id=job.job_id)
                touched.update(result.players_touched)
                findings.extend(self._findings_for_image(image_id, key, result.run_id))
            except Exception as exc:
                job.error(f"image:{image_id}", str(exc), traceback.format_exc())
            done.add(image_id)
            job.progress(len(done), len(ids), f"rescanned {len(done)}/{len(ids)}", checkpoint={"done": sorted(done), "findings": findings[-200:]})
        from rankrecovery.services.reconcile import ReconcileService

        recon = ReconcileService(self.ctx)
        for pid in sorted(touched | set(recon.stale_player_ids())):
            recon.recompute_player(pid)
        return {"username": username, "images_scanned": len(ids), "findings": findings}

    def _findings_for_image(self, image_id: int, key: str, run_id: int | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.ctx.db.write_session() as s:
            dets = s.execute(select(Detection).where(Detection.image_id == image_id, Detection.run_id == run_id)).scalars().all()
            for d in dets:
                if d.username_key != key and levenshtein(confusion_fold(d.username_key or ""), confusion_fold(key)) > 2:
                    continue
                finding = detection_dict(d)
                if d.username_key == key and d.rank_canonical is None and d.staff_canonical is None and d.status in (DetectionStatus.CANDIDATE.value, DetectionStatus.NEEDS_REVIEW.value):
                    finding["finding"] = "NAME_FOUND_RANK_UNREADABLE"
                    upsert_flags(s, "detection", d.id, [FlagInstance("NAME_FOUND_RANK_UNREADABLE", f"{d.normalised_username} found in image {image_id} but no rank could be read")], actor="rescan")
                elif d.username_key == key:
                    finding["finding"] = "MATCH"
                else:
                    finding["finding"] = "POSSIBLE_MATCH"
                finding["image_flags"] = list(s.execute(select(Flag.code).where(Flag.entity_type == "image", Flag.entity_id == image_id, Flag.is_active.is_(True))).scalars().all())
                out.append(finding)
        return out

    # ------------------------------------------------------- manual correction
    def save_manual_correction(
        self,
        image_id: int,
        *,
        username: str,
        prefix_text: str | None,
        rank: str | None,
        staff: str | None,
        region: str = "tab",
        username_bbox: list[int] | None = None,
        prefix_bbox: list[int] | None = None,
        trailing_icon: str = "none",
        leading_icon: str = "none",
        actor: str,
        note: str,
        supersedes_detection_id: int | None = None,
    ) -> dict[str, Any]:
        """Save a human-confirmed observation attached to the screenshot.

        The correction counts under the screenshot's submitter (one source);
        it never fabricates a second independent source.
        """
        if not note.strip():
            raise ValueError("a review note is required for a manual correction")
        policy = self.ctx.policy()
        if rank and policy.canonical_rank(rank) is None:
            raise ValueError(f"unknown rank {rank!r}")
        if staff and policy.canonical_staff(staff) is None:
            raise ValueError(f"unknown staff prefix {staff!r}")
        prefix = prefix_text or rank or staff or ""
        row = RawRow(
            text=f"{prefix} {username}".strip(),
            region=RegionKind(region),
            bbox=tuple(username_bbox) if username_bbox else None,  # type: ignore[arg-type]
            prefix_text=prefix,
            username_text=username,
            prefix_bbox=tuple(prefix_bbox) if prefix_bbox else None,  # type: ignore[arg-type]
            username_bbox=tuple(username_bbox) if username_bbox else None,  # type: ignore[arg-type]
            leading_icon=LeadingIcon(leading_icon),
            trailing_icon=TrailingIcon(trailing_icon),
            confidence=1.0,
        )
        parsed = ExtractionService(self.ctx).parser().parse(row)
        if parsed is None:
            raise ValueError("nothing to record")
        if rank and parsed.rank_observation != policy.canonical_rank(rank) and not staff:
            # the human chose a rank explicitly; honour it as a manual-screenshot observation
            parsed.rank_observation = policy.canonical_rank(rank)
            parsed.custom_prefix_text = prefix_text if policy.canonical_rank(rank) == policy.scuffed_rank and prefix_text and policy.canonical_rank(prefix_text) is None else None
        parsed.needs_review = False
        parsed.status = DetectionStatus.ACCEPTED
        parsed.flags = [f for f in parsed.flags if f.code in ("BEDROCK_PLAYER",)]
        parsed.notes.append(f"manual correction by {actor}: {note}")
        summary = ExtractionService(self.ctx).add_manual_detection(image_id, parsed, actor=actor, reason=note)
        with self.ctx.db.write_session() as s:
            new = s.execute(select(Detection).where(Detection.run_id == summary.run_id)).scalars().first()
            if new is not None:
                new.manual = True
                new.status = DetectionStatus.ACCEPTED.value
                if supersedes_detection_id:
                    old = s.get(Detection, supersedes_detection_id)
                    if old is not None:
                        old.superseded_by_id = new.id
                for obs in new_observations(s, new.id):
                    obs.manually_verified = True
                    obs.verified_by = actor
                    obs.verified_note = note
                    obs.review_status = "accepted"
                    obs.source_type = "manual_screenshot"
            audit(s, actor=actor, action="rescan.manual_correction", entity_type="image", entity_id=image_id, after={"username": username, "rank": rank, "staff": staff, "detection_id": new.id if new else None}, note=note)
        from rankrecovery.services.reconcile import ReconcileService

        recon = ReconcileService(self.ctx)
        for pid in summary.players_touched:
            recon.recompute_player(pid)
        return {"run_id": summary.run_id, "detection_id": new.id if new else None, "players": summary.players_touched}

    def image_context(self, image_id: int) -> dict[str, Any]:
        """Everything the reviewer needs for one image: previous detections, flags, metadata."""
        with self.ctx.db.read_session() as s:
            img = s.get(SourceImage, image_id)
            if img is None:
                raise KeyError(image_id)
            dets = s.execute(select(Detection).where(Detection.image_id == image_id).order_by(Detection.id)).scalars().all()
            flags = s.execute(select(Flag).where(Flag.entity_type == "image", Flag.entity_id == image_id).order_by(Flag.is_active.desc(), Flag.id)).scalars().all()
            batch = s.get(ImportBatch, img.import_batch_id) if img.import_batch_id else None
            return {
                "image": image_dict(img),
                "batch": {"id": batch.id, "label": batch.label, "source": batch.source} if batch else None,
                "paths": [{"path": p.path, "filename": p.filename, "submitter_id": p.submitter_id, "imported_at": p.imported_at.isoformat() if p.imported_at else None} for p in img.paths],
                "metadata": [{"key": m.key, "value": m.value, "category": m.category, "reason": m.reason} for m in img.metadata_entries],
                "detections": [detection_dict(d) for d in dets],
                "flags": [{"id": f.id, "code": f.code, "details": f.details, "is_active": f.is_active, "source": f.source, "resolved_by": f.resolved_by, "resolution_note": f.resolution_note} for f in flags],
                "runs": [{"id": r.id, "extractor": r.extractor_type, "engine": r.engine, "model": r.model, "status": r.status, "purpose": r.purpose, "started_at": r.started_at.isoformat() if r.started_at else None, "warnings": r.warnings, "error": r.error, "cost": r.cost} for r in img.runs],
            }


def new_observations(s: Any, detection_id: int) -> list[Any]:
    from rankrecovery.db.models import RankObservation

    return list(s.execute(select(RankObservation).where(RankObservation.detection_id == detection_id)).scalars().all())


def unresolved_candidates_for(ctx: AppContext, username: str) -> list[dict[str, Any]]:
    """Unresolved Mode 1 username candidates that may correspond to ``username`` (not asserted)."""
    key = normalise_username_key(username)
    folded = confusion_fold(key)
    with ctx.db.read_session() as s:
        out = []
        for d in s.execute(select(Detection).where(Detection.status.in_(["needs_review", "partial", "unreadable"]), Detection.superseded_by_id.is_(None))).scalars():
            uk = d.username_key or ""
            if not uk:
                continue
            if uk == key or levenshtein(confusion_fold(uk), folded) <= 2 or (d.partial and key.startswith(uk)):
                out.append(detection_dict(d))
        return out


def regions_from_params(raw: list[dict[str, Any]] | None) -> list[DetectedRegion] | None:
    if not raw:
        return None
    out = []
    for r in raw:
        out.append(DetectedRegion(kind=RegionKind(r.get("kind", "unknown")), bbox=tuple(int(v) for v in r["bbox"])))  # type: ignore[arg-type]
    return out


def player_alias_names(s: Any, player_id: int) -> list[str]:
    return list(s.execute(select(PlayerAlias.username).where(PlayerAlias.player_id == player_id)).scalars().all())


def player_exists(s: Any, player_id: int) -> bool:
    return s.get(Player, player_id) is not None
