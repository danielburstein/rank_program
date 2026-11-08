"""Job handlers: long-running work wired into the JobRunner."""

from __future__ import annotations

import threading
import traceback
from typing import Any

from rankrecovery.jobs.runner import JobHandle, JobRunner
from rankrecovery.services.context import AppContext


def extract_images(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    """Extract every pending image (or the given ids) and recompute touched players."""
    from rankrecovery.extraction.base import ExtractorUnavailable
    from rankrecovery.services.extraction import ExtractionService
    from rankrecovery.services.reconcile import ReconcileService

    svc = ExtractionService(ctx)
    try:
        extractor = ctx.build_extractor(params.get("extractor"))
    except ExtractorUnavailable as exc:
        raise RuntimeError(f"no extractor available: {exc}") from exc
    ids: list[int] = list(params.get("image_ids") or [])
    force = bool(params.get("force", False))
    if not ids:
        ids = svc.pending_images(fingerprint=None if force else extractor.settings.fingerprint())
    checkpoint = job.checkpoint()
    done_ids = set(checkpoint.get("done", []))
    todo = [i for i in ids if i not in done_ids]
    touched: set[int] = set()
    failed = 0
    job.progress(len(done_ids), len(ids), f"extracting with {extractor.settings.engine}")
    for n, image_id in enumerate(todo, start=1):
        job.check_cancelled()
        try:
            result = svc.extract_image(image_id, extractor=extractor, force=force, job_id=job.job_id)
            if result.error:
                failed += 1
                job.error(f"image:{image_id}", result.error)
            touched.update(result.players_touched)
        except Exception as exc:
            failed += 1
            job.error(f"image:{image_id}", str(exc), traceback.format_exc())
        done_ids.add(image_id)
        if n % 5 == 0 or n == len(todo):
            job.progress(len(done_ids), len(ids), f"{len(done_ids)}/{len(ids)} images", failed=failed, checkpoint={"done": sorted(done_ids)})
    recon = ReconcileService(ctx)
    stale = sorted(set(recon.stale_player_ids()) | touched)
    for i, pid in enumerate(stale, start=1):
        job.check_cancelled()
        recon.recompute_player(pid)
        if i % 20 == 0:
            job.progress(len(ids), len(ids), f"recomputing players {i}/{len(stale)}")
    return {"images": len(ids), "failed": failed, "players_recomputed": len(stale)}


def recompute_players(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.reconcile import ReconcileService

    recon = ReconcileService(ctx)
    ids = list(params.get("player_ids") or []) or (recon.all_player_ids() if params.get("all") else recon.stale_player_ids())
    for i, pid in enumerate(ids, start=1):
        job.check_cancelled()
        recon.recompute_player(pid)
        if i % 10 == 0 or i == len(ids):
            job.progress(i, len(ids), f"{i}/{len(ids)} players")
    if params.get("then_convert", True):
        from rankrecovery.services.conversion import ConversionService

        ConversionService(ctx).recompute(ids or None)
    return {"players": len(ids)}


def convert_players(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.conversion import ConversionService

    def progress(done: int, total: int) -> None:
        if done % 25 == 0 or done == total:
            job.progress(done, total, f"{done}/{total} conversions")

    return ConversionService(ctx).recompute(params.get("player_ids") or None, progress=progress, cancel=lambda: job.cancelled)


def lookup_uuids(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.identity import IdentityService

    svc = IdentityService(ctx)
    ids = list(params.get("player_ids") or [])
    if not ids and params.get("all"):
        from rankrecovery.services.reconcile import ReconcileService as _R

        ids = _R(ctx).all_player_ids()
    if not ids:
        ids = svc.candidate_player_ids(
            only_unresolved=not params.get("retry_failed", False),
            only_failed=bool(params.get("retry_failed", False)),
            only_proxy_failures=bool(params.get("retry_proxy_failures", False)),
        )
    cancel = threading.Event()

    def progress(done: int, total: int) -> None:
        if job.cancelled:
            cancel.set()
        job.progress(done, total, f"{done}/{total} lookups")

    job.progress(0, len(ids), "starting lookups")
    summary = svc.lookup_players(ids, force=bool(params.get("force", False)), workers=params.get("workers"), job_id=job.job_id, progress=progress, cancel=cancel)
    from rankrecovery.services.reconcile import ReconcileService

    recon = ReconcileService(ctx)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    return {"resolved": summary.resolved, "not_found": summary.not_found, "failed": summary.failed, "skipped": summary.skipped, "merged": summary.merged}


def rescan_images(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.rescan import RescanService

    return RescanService(ctx).run_rescan_job(job, params)


def register_default_handlers(runner: JobRunner) -> None:
    runner.register("extract_images", extract_images)
    runner.register("recompute_players", recompute_players)
    runner.register("convert_players", convert_players)
    runner.register("lookup_uuids", lookup_uuids)
    runner.register("rescan_images", rescan_images)
