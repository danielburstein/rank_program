"""Async client for the Rank Recovery server API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


class ApiError(Exception):
    """A failed API call. ``status`` is 0 for transport-level failures."""

    def __init__(self, status: int, message: str):
        super().__init__(f"API error {status}: {message}" if status else f"API unreachable: {message}")
        self.status = status
        self.message = message


def guess_content_type(filename: str) -> str:
    lowered = filename.lower()
    for ext, ctype in IMAGE_CONTENT_TYPES.items():
        if lowered.endswith(ext):
            return ctype
    return "application/octet-stream"


class RankApiClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0, upload_timeout: float = 300.0, transport: httpx.AsyncBaseTransport | None = None):
        self._timeout = timeout
        self._upload_timeout = upload_timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "User-Agent": "rankrecovery-bot/0.1"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def _request(self, method: str, path: str, *, allow_404: bool = False, timeout: float | None = None, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, f"/api/v1{path}", timeout=timeout or self._timeout, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("API transport error on %s %s: %s", method, path, type(exc).__name__)
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            detail = _detail(response)
            log.warning("API %s %s -> %s %s", method, path, response.status_code, detail)
            raise ApiError(response.status_code, detail)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, "invalid JSON in response") from exc

    # ------------------------------------------------------------- endpoints
    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def dashboard(self) -> dict[str, Any]:
        return await self._request("GET", "/dashboard")

    async def player_by_name(self, username: str) -> dict[str, Any] | None:
        from urllib.parse import quote

        return await self._request("GET", f"/players/by-name/{quote(username, safe='')}", allow_404=True)

    async def search_players(self, text: str, *, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._request("POST", "/search", json={"text": text, "entity_types": ["player"], "limit": limit})
        return list((data or {}).get("players", []))

    async def submit_images(self, files: list[tuple[str, bytes]], *, submitter: str | None, discord_user_id: str | None, batch_label: str) -> dict[str, Any]:
        batch_form: dict[str, Any] = {"label": batch_label, "source": "bot"}
        if submitter:
            batch_form["submitter"] = submitter
        batch = await self._request("POST", "/batches", data=batch_form)
        batch_id = (batch or {}).get("batch_id")
        form: dict[str, Any] = {"extract": "true"}
        if submitter:
            form["submitter"] = submitter
        if batch_id is not None:
            form["batch_id"] = str(batch_id)
        if discord_user_id:
            form["discord_user_id"] = discord_user_id
        multipart = [("files", (name, content, guess_content_type(name))) for name, content in files]
        result = await self._request("POST", "/images", data=form, files=multipart, timeout=self._upload_timeout)
        result = dict(result or {})
        result["batch_id"] = batch_id
        return result

    async def job(self, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/jobs/{job_id}")

    async def link(self, discord_user_id: str, discord_username: str, username: str, *, verified: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/discord/link", json={"discord_user_id": discord_user_id, "discord_username": discord_username, "username": username, "verified": verified})

    async def get_link(self, discord_user_id: str) -> dict[str, Any] | None:
        return await self._request("GET", f"/discord/link/{discord_user_id}", allow_404=True)

    async def unlink(self, discord_user_id: str) -> bool:
        result = await self._request("DELETE", f"/discord/link/{discord_user_id}", allow_404=True)
        return result is not None

    async def pending_detections(self, username: str | None = None, *, limit: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if username:
            params["username"] = username
        return await self._request("GET", "/detections/pending", params=params)

    async def review_detection(self, detection_id: int, *, accept: bool, note: str) -> dict[str, Any]:
        return await self._request("POST", f"/detections/{detection_id}/review", json={"accept": accept, "note": note})

    async def image_overlay(self, image_id: int, *, highlight: str | None = None) -> bytes:
        params = {"highlight": highlight} if highlight else None
        try:
            response = await self._client.get(f"/api/v1/images/{image_id}/overlay", params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"transport error: {exc}") from exc
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text[:200])
        return response.content

    async def recompute_player(self, player_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/players/{player_id}/recompute")


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    if isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        return detail if isinstance(detail, str) else str(detail)[:300]
    return str(data)[:300]
