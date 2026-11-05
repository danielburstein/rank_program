"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rankrecovery import __version__
from rankrecovery.api import logbuffer
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.jobs.handlers import register_default_handlers
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext

log = logging.getLogger(__name__)


def default_database_url() -> str:
    url = os.environ.get("RANKRECOVERY_DATABASE_URL")
    if url:
        return url
    data_dir = Path(os.environ.get("RANKRECOVERY_DATA_DIR", "data"))
    return sqlite_url(data_dir / "rankrecovery.db")


def build_context(database_url: str | None = None) -> AppContext:
    db = Database(database_url or default_database_url())
    db.run_migrations()
    return AppContext(db)


def create_app(ctx: AppContext | None = None, *, start_jobs: bool = True, job_workers: int | None = None) -> FastAPI:
    handler = logbuffer.install()
    ctx = ctx or build_context()
    workers = job_workers or int(os.environ.get("RANKRECOVERY_JOB_WORKERS", "2"))
    runner = JobRunner(ctx, workers=workers)
    register_default_handlers(runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_jobs:
            runner.start()
        log.info("rankrecovery server %s started (%s)", __version__, ctx.db.describe_concurrency())
        try:
            yield
        finally:
            runner.stop()
            ctx.db.checkpoint()
            ctx.db.dispose()

    app = FastAPI(title="Rank Recovery", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx
    app.state.runner = runner
    app.state.logbuffer = handler
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    from rankrecovery.api.routes import (
        admin,
        conversion,
        csvimport,
        discord,
        export,
        identity,
        images,
        jobs,
        players,
        rescan,
        review,
        search,
        settings,
    )

    for module in (players, images, review, jobs, csvimport, identity, conversion, export, search, rescan, settings, discord, admin):
        app.include_router(module.router, prefix="/api/v1")

    @app.get("/api/v1/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": __version__, "database": ctx.db.describe_concurrency(), "vision_configured": ctx.vision_available()}

    return app
