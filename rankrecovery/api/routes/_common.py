from __future__ import annotations

from fastapi import HTTPException, Request

from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext


def ctx_of(request: Request) -> AppContext:
    return request.app.state.ctx


def runner_of(request: Request) -> JobRunner:
    return request.app.state.runner


def not_found(what: str) -> HTTPException:
    return HTTPException(404, f"{what} not found")


def bad_request(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))
