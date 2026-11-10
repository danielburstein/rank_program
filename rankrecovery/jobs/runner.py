"""Persistent background jobs with cancellation, pause and checkpoints."""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from rankrecovery.core.enums import JobStatus
from rankrecovery.db.models import Job, JobError
from rankrecovery.services.context import AppContext

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


@dataclass
class JobHandle:
    """Passed to job functions for progress reporting and cooperative cancellation."""

    runner: JobRunner
    job_id: int
    cancel_event: threading.Event

    def progress(self, done: int, total: int | None = None, message: str = "", *, failed: int | None = None, checkpoint: dict[str, Any] | None = None) -> None:
        self.runner._update(self.job_id, done=done, total=total, message=message, failed=failed, checkpoint=checkpoint)

    def error(self, item_ref: str, message: str, tb: str = "") -> None:
        self.runner._record_error(self.job_id, item_ref, message, tb)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()
        self.runner._wait_if_paused(self.job_id, self.cancel_event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def checkpoint(self) -> dict[str, Any]:
        return self.runner.get(self.job_id).get("checkpoint", {}) or {}


JobFunc = Callable[[AppContext, JobHandle, dict[str, Any]], dict[str, Any]]


class JobRunner:
    def __init__(self, ctx: AppContext, *, workers: int = 2, max_queue: int = 1000):
        self.ctx = ctx
        self.workers = workers
        self._queue: queue.Queue[int] = queue.Queue(maxsize=max_queue)
        self._registry: dict[str, JobFunc] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._pause_flags: dict[int, bool] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        ctx.job_runner = self

    # --------------------------------------------------------------- registry
    def register(self, kind: str, func: JobFunc) -> None:
        self._registry[kind] = func

    def kinds(self) -> list[str]:
        return sorted(self._registry)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._threads:
            return
        self._resume_interrupted()
        for n in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"job-worker-{n}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        self._stop.set()
        for ev in list(self._cancel_events.values()):
            ev.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait(-1)
            except queue.Full:
                pass
        if wait:
            for t in self._threads:
                t.join(timeout)
        self._threads.clear()

    def _resume_interrupted(self) -> None:
        """Jobs that were running when the process died are re-queued (they resume from their checkpoint)."""
        with self.ctx.db.write_session() as s:
            for job in s.execute(select(Job).where(Job.status.in_([JobStatus.RUNNING.value, JobStatus.PAUSED.value, JobStatus.QUEUED.value])).order_by(Job.id)).scalars():
                job.status = JobStatus.QUEUED.value
                job.progress_message = "re-queued after restart; resuming from checkpoint"
                try:
                    self._queue.put_nowait(job.id)
                except queue.Full:
                    break

    # ----------------------------------------------------------------- submit
    def submit(self, kind: str, params: dict[str, Any] | None = None, *, label: str = "", created_by: str = "", total: int = 0) -> int:
        if kind not in self._registry:
            raise KeyError(f"unknown job kind {kind}")
        with self.ctx.db.write_session() as s:
            job = Job(kind=kind, status=JobStatus.QUEUED.value, label=label or kind, params=params or {}, created_by=created_by, total=total)
            s.add(job)
            s.flush()
            job_id = job.id
        self._queue.put(job_id)
        return job_id

    def cancel(self, job_id: int) -> bool:
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                return False
            job.cancel_requested = True
            if job.status == JobStatus.QUEUED.value:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = datetime.now(UTC)
        ev = self._cancel_events.get(job_id)
        if ev:
            ev.set()
        return True

    def pause(self, job_id: int, paused: bool) -> None:
        with self._lock:
            self._pause_flags[job_id] = paused
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is not None and job.status in (JobStatus.RUNNING.value, JobStatus.PAUSED.value):
                job.pause_requested = paused
                job.status = JobStatus.PAUSED.value if paused else JobStatus.RUNNING.value

    def _wait_if_paused(self, job_id: int, cancel: threading.Event) -> None:
        while True:
            with self._lock:
                paused = self._pause_flags.get(job_id, False)
            if not paused or cancel.is_set():
                return
            cancel.wait(0.25)

    # ------------------------------------------------------------------ worker
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id < 0:
                break
            try:
                self._run(job_id)
            except Exception:  # pragma: no cover - last resort
                log.exception("job %s crashed outside handler", job_id)
            finally:
                self._queue.task_done()

    def _run(self, job_id: int) -> None:
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is None or job.status == JobStatus.CANCELLED.value:
                return
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = datetime.now(UTC)
                return
            job.status = JobStatus.RUNNING.value
            job.started_at = job.started_at or datetime.now(UTC)
            kind, params = job.kind, dict(job.params or {})
        func = self._registry.get(kind)
        cancel = threading.Event()
        self._cancel_events[job_id] = cancel
        handle = JobHandle(self, job_id, cancel)
        try:
            if func is None:
                raise RuntimeError(f"no handler registered for job kind {kind}")
            result = func(self.ctx, handle, params)
            if cancel.is_set() and self._stop.is_set():
                self._interrupted(job_id)
            else:
                self._finish(job_id, JobStatus.CANCELLED if cancel.is_set() else JobStatus.COMPLETED, result=result)
        except JobCancelled:
            if self._stop.is_set():
                self._interrupted(job_id)  # the process is shutting down, not the user cancelling: resume after the restart
            else:
                self._finish(job_id, JobStatus.CANCELLED)
        except Exception as exc:
            log.exception("job %s (%s) failed", job_id, kind)
            self._record_error(job_id, "job", str(exc), traceback.format_exc())
            self._finish(job_id, JobStatus.FAILED, error=str(exc))
        finally:
            self._cancel_events.pop(job_id, None)
            with self._lock:
                self._pause_flags.pop(job_id, None)

    def _interrupted(self, job_id: int) -> None:
        """Put a job stopped by shutdown back in the queue; ``start()`` re-queues it from its checkpoint."""
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is None or job.cancel_requested:
                if job is not None:
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = datetime.now(UTC)
                return
            job.status = JobStatus.QUEUED.value
            job.progress_message = "interrupted by a restart; resumes from its checkpoint when the server is back"
        log.info("job %s interrupted by shutdown; it resumes after the restart", job_id)

    # ------------------------------------------------------------- persistence
    def _update(self, job_id: int, *, done: int, total: int | None, message: str, failed: int | None, checkpoint: dict[str, Any] | None) -> None:
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.done = done
            if total is not None:
                job.total = total
            if failed is not None:
                job.failed = failed
            if message:
                job.progress_message = message
            if checkpoint is not None:
                job.checkpoint = checkpoint

    def _record_error(self, job_id: int, item_ref: str, message: str, tb: str) -> None:
        with self.ctx.db.write_session() as s:
            s.add(JobError(job_id=job_id, item_ref=item_ref[:255], message=message[:4000], traceback=tb[:8000]))

    def _finish(self, job_id: int, status: JobStatus, *, result: dict[str, Any] | None = None, error: str = "") -> None:
        with self.ctx.db.write_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.status = status.value
            job.finished_at = datetime.now(UTC)
            if result is not None:
                job.result = result
            if error:
                job.error = error

    # ----------------------------------------------------------------- queries
    def get(self, job_id: int) -> dict[str, Any]:
        with self.ctx.db.read_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            return job_dict(job, include_errors=True)

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            stmt = select(Job).order_by(Job.id.desc()).limit(limit)
            if status:
                stmt = stmt.where(Job.status == status)
            return [job_dict(j) for j in s.execute(stmt).scalars()]

    def wait(self, job_id: int, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the job finishes (tests and CLI)."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.get(job_id)
            if info["status"] in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                return info
            time.sleep(0.05)
        return self.get(job_id)


def job_dict(job: Job, *, include_errors: bool = False) -> dict[str, Any]:
    d = {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "label": job.label,
        "total": job.total,
        "done": job.done,
        "failed": job.failed,
        "progress_message": job.progress_message,
        "params": job.params,
        "checkpoint": job.checkpoint,
        "result": job.result,
        "error": job.error,
        "created_by": job.created_by,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "cancel_requested": job.cancel_requested,
        "pause_requested": job.pause_requested,
    }
    if job.total:
        d["percent"] = round(100.0 * job.done / job.total, 1)
    if include_errors:
        d["errors"] = [{"item": e.item_ref, "message": e.message, "at": e.created_at.isoformat() if e.created_at else None} for e in job.errors]
    return d
