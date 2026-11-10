"""A server restart must leave running jobs queued, not cancelled, so they resume from their checkpoint."""

from __future__ import annotations

import threading
import time

from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext


def test_shutdown_requeues_running_job_and_next_start_resumes_it(tmp_path):
    db = Database(sqlite_url(tmp_path / "jobs.db"))
    db.run_migrations()
    ctx = AppContext(db)
    started = threading.Event()
    seen_checkpoints: list[dict] = []

    def slow(ctx_, job, params):
        done = set(job.checkpoint().get("done", []))
        seen_checkpoints.append(dict(job.checkpoint()))
        for n in range(1, 101):
            if n in done:
                continue
            job.check_cancelled()  # raises JobCancelled when the runner is stopping
            done.add(n)
            job.progress(len(done), 100, "working", checkpoint={"done": sorted(done)})
            started.set()
            time.sleep(0.02)
        return {"items": len(done)}

    runner = JobRunner(ctx, workers=1)
    runner.register("slow", slow)
    runner.start()
    job_id = runner.submit("slow", {})
    assert started.wait(5)
    runner.stop()  # the restart

    info = runner.get(job_id)
    assert info["status"] == "queued" and "restart" in info["progress_message"]
    assert info["checkpoint"]["done"], "progress made before the restart is kept"
    done_before = len(info["checkpoint"]["done"])

    # a user cancel still ends the job
    second = JobRunner(ctx, workers=1)
    second.register("slow", slow)
    second.start()
    try:
        finished = second.wait(job_id, timeout=30)
        assert finished["status"] == "completed", finished
        assert finished["result"]["items"] == 100
        assert seen_checkpoints[-1]["done"] and len(seen_checkpoints[-1]["done"]) >= done_before  # resumed, not restarted
        other = second.submit("slow", {})
        assert started.wait(5)
        time.sleep(0.05)
        second.cancel(other)
        assert second.wait(other, timeout=10)["status"] == "cancelled"
    finally:
        second.stop()
    db.dispose()
