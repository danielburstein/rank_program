"""Job handlers: long-running work wired into the JobRunner."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any

from rankrecovery.jobs.runner import JobHandle, JobRunner
from rankrecovery.services.context import AppContext
from rankrecovery.services.uuidranks import refresh_uuid_ranks

log = logging.getLogger(__name__)


SLEEP = time.sleep  # patched by tests


def extract_images(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    """Extract every pending image (or the given ids), several at a time, then recompute touched players.

    Vision images run ``extraction.vision_concurrency`` at a time (OCR: ``extraction.ocr_workers``);
    inside each image the crops are read in parallel too, all capped by one shared limit on model
    calls in flight. An image the API rate-limits is paused and retried with a growing delay.
    """
    from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
    from concurrent.futures import wait as wait_futures

    from rankrecovery.extraction.base import ExtractorUnavailable
    from rankrecovery.extraction.vision import RATE_LIMIT_MARKER
    from rankrecovery.jobs.runner import JobCancelled
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
    engine = extractor.settings.engine
    key = "extraction.vision_concurrency" if engine == "anthropic" else "extraction.ocr_workers"
    workers = max(1, min(16, int(params.get("workers") or ctx.settings.get(key, 4 if engine == "anthropic" else 2))))
    rate_limit_retries = max(0, int(ctx.settings.get("extraction.rate_limit_retries", 4)))

    def work(image_id: int):
        pause = 15.0
        for attempt in range(rate_limit_retries + 1):
            result = svc.extract_image(image_id, extractor=extractor, force=force, job_id=job.job_id)
            if result.error and RATE_LIMIT_MARKER in result.error and attempt < rate_limit_retries and not job.cancelled:
                log.warning("image %s rate limited; pausing %.0fs before try %d/%d", image_id, pause, attempt + 2, rate_limit_retries + 1)
                SLEEP(pause)
                pause = min(pause * 2, 300.0)
                continue
            return result
        return result  # pragma: no cover - loop always returns

    job.progress(len(done_ids), len(ids), f"extracting with {engine}, {workers} image(s) at a time")
    completed = 0
    pending: dict[Future, int] = {}
    next_index = 0
    window = workers * 2
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract") as pool:
        try:
            while next_index < len(todo) or pending:
                while next_index < len(todo) and len(pending) < window:
                    job.check_cancelled()
                    image_id = todo[next_index]
                    pending[pool.submit(work, image_id)] = image_id
                    next_index += 1
                if not pending:
                    break
                finished, _ = wait_futures(list(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    image_id = pending.pop(future)
                    try:
                        result = future.result()
                        if result.error:
                            failed += 1
                            job.error(f"image:{image_id}", result.error)
                        touched.update(result.players_touched)
                    except ExtractorUnavailable as exc:
                        raise RuntimeError(f"extractor became unavailable: {exc}") from exc
                    except Exception as exc:
                        failed += 1
                        job.error(f"image:{image_id}", str(exc), traceback.format_exc())
                    done_ids.add(image_id)
                    completed += 1
                    if completed % 5 == 0 or completed == len(todo):
                        job.progress(len(done_ids), len(ids), f"{len(done_ids)}/{len(ids)} images ({workers} at a time)", failed=failed, checkpoint={"done": sorted(done_ids)})
        except JobCancelled:
            for future in pending:
                future.cancel()
            raise
    recon = ReconcileService(ctx)
    stale = sorted(set(recon.stale_player_ids()) | touched)
    for i, pid in enumerate(stale, start=1):
        job.check_cancelled()
        recon.recompute_player(pid)
        if i % 20 == 0:
            job.progress(len(ids), len(ids), f"recomputing players {i}/{len(stale)}")
    from rankrecovery.services.conversion import ConversionService

    job.progress(len(ids), len(ids), "refreshing old-to-new rank conversions")
    ConversionService(ctx).refresh_stale(cancel=lambda: job.cancelled)  # a new rank must never be shown against an outdated old rank
    refresh_uuid_ranks(ctx, actor="extraction")
    return {"images": len(ids), "failed": failed, "players_recomputed": len(stale), "workers": workers}


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
    refresh_uuid_ranks(ctx, actor="recompute")
    return {"players": len(ids)}


def convert_players(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.conversion import ConversionService

    def progress(done: int, total: int) -> None:
        if done % 25 == 0 or done == total:
            job.progress(done, total, f"{done}/{total} conversions")

    svc = ConversionService(ctx)
    if params.get("only_stale"):
        out = svc.refresh_stale(progress=progress, cancel=lambda: job.cancelled)
    else:
        out = svc.recompute(params.get("player_ids") or None, progress=progress, cancel=lambda: job.cancelled)
    refresh_uuid_ranks(ctx, actor="conversion")
    return out


def ingest_folder(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    """Submit every image waiting in the newuploads folder (and its sub-folders), with progress per file."""
    from rankrecovery.services.ingest import FolderIngestService

    def progress(done: int, total: int, message: str) -> None:
        job.check_cancelled()
        job.progress(done, total, f"{done}/{total} files: {message}")

    summary = FolderIngestService(ctx).ingest(submitter=params.get("submitter") or None, actor=str(params.get("actor") or "ingest"), extract=bool(params.get("extract", True)), progress=progress)
    for detail in summary.details[:50]:
        job.error("file", detail)
    return summary.as_dict()


def download_drive(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    """Pull the photos behind a shared Google Drive link into the newuploads folder and, by default, submit them."""
    import os

    from rankrecovery.services.drive import DriveDownloader
    from rankrecovery.services.ingest import FolderIngestService

    folder = FolderIngestService(ctx)
    dest = folder.folder_path()
    api_key = str(ctx.settings.get("drive.api_key") or os.environ.get("RANKRECOVERY_GOOGLE_API_KEY") or "") or None

    def progress(done: int, total: int | None, name: str) -> None:
        job.check_cancelled()
        job.progress(done, total or 0, f"downloaded {done}" + (f"/{total}" if total else "") + f": {name}"[:120])

    job.progress(0, 0, "listing the Drive link")
    result = DriveDownloader(dest, api_key=api_key, progress=progress).download(str(params.get("url") or ""))
    out: dict[str, Any] = result.as_dict()
    for warning in result.warnings:
        job.error("drive", warning)
    if params.get("ingest", True) and result.downloaded:

        def ingest_progress(done: int, total: int, message: str) -> None:
            job.check_cancelled()
            job.progress(done, total, f"submitting {done}/{total}: {message}")

        summary = folder.ingest(submitter=params.get("submitter") or None, actor=str(params.get("actor") or "drive"), extract=bool(params.get("extract", True)), progress=ingest_progress)
        out["ingest"] = summary.as_dict()
    return out


def lookup_uuids(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.identity import IdentityService

    svc = IdentityService(ctx)
    force = bool(params.get("force", False))
    chosen = list(params.get("player_ids") or [])
    if chosen:
        # explicitly selected players: still skip those with a UUID unless the caller forces a re-lookup
        ids = svc.candidate_player_ids(player_ids=chosen, only_unresolved=False, skip_with_uuid=not force)
    elif params.get("all"):
        ids = svc.candidate_player_ids(only_unresolved=False)  # every player without a UUID, whatever the last outcome was
    else:
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
    from rankrecovery.services.conversion import ConversionService

    ConversionService(ctx).refresh_stale(cancel=lambda: job.cancelled)
    out = {"resolved": summary.resolved, "not_found": summary.not_found, "failed": summary.failed, "skipped": summary.skipped, "merged": summary.merged}
    ranks = refresh_uuid_ranks(ctx, actor="lookup")
    if ranks:
        out["uuid_ranks"] = ranks["uuids"]
    return out


def reparse_detections(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    """Re-classify stored readings under the current rank policy, then recompute every player."""
    from rankrecovery.services.conversion import ConversionService
    from rankrecovery.services.extraction import ExtractionService
    from rankrecovery.services.reconcile import ReconcileService

    job.progress(0, 0, "re-parsing stored detections")
    counts = ExtractionService(ctx).reparse_detections(params.get("image_ids") or None)
    recon = ReconcileService(ctx)
    ids = recon.all_player_ids() if params.get("all", True) else recon.stale_player_ids()
    for i, pid in enumerate(ids, start=1):
        job.check_cancelled()
        recon.recompute_player(pid)
        if i % 20 == 0 or i == len(ids):
            job.progress(i, len(ids), f"recomputing players {i}/{len(ids)}")
    ConversionService(ctx).recompute()
    return {**counts, "players_recomputed": len(ids)}


def refresh_names(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.identity import IdentityService

    cancel = threading.Event()

    def progress(done: int, total: int) -> None:
        if job.cancelled:
            cancel.set()
        job.progress(done, total, f"{done}/{total} profiles")

    return IdentityService(ctx).refresh_current_names(params.get("player_ids") or None, force=bool(params.get("force", False)), job_id=job.job_id, progress=progress, cancel=cancel)


def rescan_images(ctx: AppContext, job: JobHandle, params: dict[str, Any]) -> dict[str, Any]:
    from rankrecovery.services.rescan import RescanService

    return RescanService(ctx).run_rescan_job(job, params)


def register_default_handlers(runner: JobRunner) -> None:
    runner.register("extract_images", extract_images)
    runner.register("recompute_players", recompute_players)
    runner.register("convert_players", convert_players)
    runner.register("lookup_uuids", lookup_uuids)
    runner.register("download_drive", download_drive)
    runner.register("ingest_folder", ingest_folder)
    runner.register("rescan_images", rescan_images)
    runner.register("refresh_names", refresh_names)
    runner.register("reparse_detections", reparse_detections)
