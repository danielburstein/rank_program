"""Synchronous HTTP client for the Rank Recovery server API.

Every method maps to one endpoint under ``/api/v1``. Calls are blocking; the
GUI wraps them in :class:`rankrecovery.client.workers.ApiWorker` so the event
loop never stalls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

CONFIG_DIR = Path.home() / ".rankrecovery"
CONFIG_FILE = CONFIG_DIR / "client.json"
UPLOAD_CHUNK = 10


class ServerError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


@dataclass
class ClientConfig:
    base_url: str = "http://127.0.0.1:8420"
    api_key: str = ""
    timeout: float = 120.0
    window: dict[str, Any] = field(default_factory=dict)
    path: Path = field(default=CONFIG_FILE, repr=False)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> ClientConfig:
        cfg = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        for key in ("base_url", "api_key", "timeout", "window"):
            if key in data:
                setattr(cfg, key, data[key])
        return cfg

    def save(self, path: Path | None = None) -> None:
        # The API key is stored in plain text in the user's home directory; the
        # file is created with owner-only permissions where the OS supports it.
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"base_url": self.base_url, "api_key": self.api_key, "timeout": self.timeout, "window": self.window}, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


class ServerClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0, *, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.Client(base_url=self.base_url + "/api/v1", headers={"X-API-Key": api_key}, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- core
    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None, data: dict[str, Any] | None = None, files: Any = None, raw: bool = False) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._client.request(method, path, params=clean_params, json=json_body, data=data, files=files)
        except httpx.HTTPError as exc:
            raise ServerError(0, f"cannot reach server: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ServerError(response.status_code, str(detail))
        if raw:
            return response.content
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: Any = None, **params: Any) -> Any:
        return self._request("POST", path, json_body=json_body, params=params)

    def put(self, path: str, json_body: Any = None) -> Any:
        return self._request("PUT", path, json_body=json_body)

    def delete(self, path: str, **params: Any) -> Any:
        return self._request("DELETE", path, params=params)

    # ----------------------------------------------------------------- meta
    def health(self) -> dict:
        return self.get("/health")

    def dashboard(self) -> dict:
        return self.get("/dashboard")

    def logs(self, limit: int = 200, level: str | None = None) -> list[dict]:
        return self.get("/admin/logs", limit=limit, level=level)

    # -------------------------------------------------------------- players
    def players(self, **params: Any) -> dict:
        return self.get("/players", **params)

    def player(self, player_id: int) -> dict:
        return self.get(f"/players/{player_id}")

    def player_by_name(self, username: str) -> dict:
        return self.get(f"/players/by-name/{username}")

    def recompute_player(self, player_id: int) -> dict:
        return self.post(f"/players/{player_id}/recompute")

    def override_rank(self, player_id: int, rank: str, reason: str) -> dict:
        return self.post(f"/players/{player_id}/override-rank", {"rank": rank, "reason": reason})

    def manual_staff(self, player_id: int, staff: str, reason: str) -> dict:
        return self.post(f"/players/{player_id}/staff", {"staff": staff, "reason": reason})

    def add_waiver(self, player_id: int, rank: str, reason: str) -> dict:
        return self.post(f"/players/{player_id}/waivers", {"rank": rank, "reason": reason})

    def revoke_waiver(self, waiver_id: int, reason: str) -> dict:
        return self.delete(f"/waivers/{waiver_id}", reason=reason)

    def set_manual_uuid(self, player_id: int, uuid: str | None, reason: str) -> dict:
        return self.post(f"/players/{player_id}/uuid", {"uuid": uuid, "reason": reason})

    def add_alias(self, player_id: int, username: str, reason: str) -> dict:
        return self.post(f"/players/{player_id}/aliases", {"username": username, "reason": reason})

    def set_review_status(self, player_id: int, status: str, note: str) -> dict:
        return self.post(f"/players/{player_id}/review-status", {"status": status, "note": note})

    def conversion_approval(self, player_id: int, approved: bool, reason: str) -> dict:
        return self.post(f"/players/{player_id}/conversion-approval", {"approved": approved, "reason": reason})

    def player_overlay(self, player_id: int, image_id: int) -> bytes:
        return self._request("GET", f"/players/{player_id}/overlay/{image_id}", raw=True)

    # --------------------------------------------------------------- images
    def create_batch(self, *, label: str, submitter: str | None, source: str = "client", notes: str = "") -> int:
        data = {"label": label, "source": source, "notes": notes}
        if submitter:
            data["submitter"] = submitter
        return int(self._request("POST", "/batches", data=data)["batch_id"])

    def upload_images(self, paths: list[Path], *, submitter: str | None, batch_id: int | None, extract: bool = True, progress_cb: Callable[[int, int], None] | None = None, submitter_for: Callable[[Path], str | None] | None = None) -> dict:
        """Upload files in chunks. Returns {"outcomes": [...], "job_ids": [...]}."""
        outcomes: list[dict] = []
        job_ids: list[int] = []
        groups: dict[str | None, list[Path]] = {}
        for p in paths:
            key = submitter_for(p) if submitter_for else submitter
            groups.setdefault(key, []).append(p)
        done = 0
        for group_submitter, group_paths in groups.items():
            for start in range(0, len(group_paths), UPLOAD_CHUNK):
                chunk = group_paths[start : start + UPLOAD_CHUNK]
                files = []
                handles = []
                data: dict[str, Any] = {"extract": "true" if extract else "false", "original_paths": [], "file_modified": []}
                if group_submitter:
                    data["submitter"] = group_submitter
                if batch_id is not None:
                    data["batch_id"] = str(batch_id)
                for p in chunk:
                    fh = p.open("rb")
                    handles.append(fh)
                    files.append(("files", (p.name, fh, "application/octet-stream")))
                    data["original_paths"].append(str(p.resolve()))
                    data["file_modified"].append(datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat())
                try:
                    result = self._request("POST", "/images", data=data, files=files)
                finally:
                    for fh in handles:
                        fh.close()
                outcomes.extend(result.get("outcomes", []))
                if result.get("job_id"):
                    job_ids.append(int(result["job_id"]))
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, len(paths))
        return {"outcomes": outcomes, "job_ids": job_ids}

    def images(self, **params: Any) -> dict:
        return self.get("/images", **params)

    def image_context(self, image_id: int) -> dict:
        return self.get(f"/images/{image_id}")

    def image_content(self, image_id: int) -> bytes:
        return self._request("GET", f"/images/{image_id}/content", raw=True)

    def image_overlay(self, image_id: int, highlight: str | None = None) -> bytes:
        return self._request("GET", f"/images/{image_id}/overlay", params={"highlight": highlight}, raw=True)

    def set_submitter(self, image_id: int, submitter: str | None, reason: str) -> dict:
        return self.post(f"/images/{image_id}/submitter", {"submitter": submitter, "reason": reason})

    def ai_review(self, image_id: int, flagged: bool, reason: str) -> dict:
        return self.post(f"/images/{image_id}/ai-review", {"flagged": flagged, "reason": reason})

    def date_review(self, image_id: int, capture_at: str | None, reason: str) -> dict:
        return self.post(f"/images/{image_id}/date-review", {"capture_at": capture_at, "reason": reason})

    def submitters(self) -> list[dict]:
        return self.get("/submitters")

    def batches(self, limit: int = 100) -> list[dict]:
        return self.get("/batches", limit=limit)

    # --------------------------------------------------------------- review
    def pending_detections(self, username: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        return self.get("/detections/pending", username=username, limit=limit, offset=offset)

    def review_detection(self, detection_id: int, accept: bool, note: str) -> dict:
        return self.post(f"/detections/{detection_id}/review", {"accept": accept, "note": note})

    def bulk_review(self, detection_ids: list[int], accept: bool, note: str) -> dict:
        return self.post("/detections/bulk-review", {"detection_ids": detection_ids, "accept": accept, "note": note})

    def verify_observation(self, observation_id: int, verified: bool, note: str) -> dict:
        return self.post(f"/observations/{observation_id}/verify", {"verified": verified, "note": note})

    def deactivate_observation(self, observation_id: int, note: str) -> dict:
        return self.post(f"/observations/{observation_id}/deactivate", {"note": note})

    def flags(self, entity_type: str, entity_id: int, include_resolved: bool = True) -> list[dict]:
        return self.get("/flags", entity_type=entity_type, entity_id=entity_id, include_resolved=include_resolved)

    def flag_catalogue(self) -> list[dict]:
        return self.get("/flags/catalogue")

    def add_flag(self, entity_type: str, entity_id: int, code: str, details: str, note: str = "") -> dict:
        return self.post("/flags", {"entity_type": entity_type, "entity_id": entity_id, "code": code, "details": details, "note": note})

    def resolve_flag(self, flag_id: int, note: str) -> dict:
        return self.post(f"/flags/{flag_id}/resolve", {"note": note})

    def reopen_flag(self, flag_id: int, note: str) -> dict:
        return self.post(f"/flags/{flag_id}/reopen", {"note": note})

    # ----------------------------------------------------------------- jobs
    def jobs(self, limit: int = 50, status: str | None = None) -> list[dict]:
        return self.get("/jobs", limit=limit, status=status)

    def job_kinds(self) -> list[str]:
        return self.get("/jobs/kinds")

    def job(self, job_id: int) -> dict:
        return self.get(f"/jobs/{job_id}")

    def create_job(self, kind: str, params: dict | None = None, label: str = "") -> dict:
        return self.post("/jobs", {"kind": kind, "params": params or {}, "label": label})

    def cancel_job(self, job_id: int) -> dict:
        return self.post(f"/jobs/{job_id}/cancel")

    def pause_job(self, job_id: int) -> dict:
        return self.post(f"/jobs/{job_id}/pause")

    def resume_job(self, job_id: int) -> dict:
        return self.post(f"/jobs/{job_id}/resume")

    def extract(self, image_ids: list[int] | None = None, force: bool = False, extractor: str | None = None) -> dict:
        return self.post("/extract", {"image_ids": image_ids, "force": force, "extractor": extractor})

    def recompute(self, player_ids: list[int] | None = None, all_players: bool = False, then_convert: bool = True) -> dict:
        return self.post("/recompute", {"player_ids": player_ids, "all": all_players, "then_convert": then_convert})

    # ------------------------------------------------------------------ csv
    def preview_csv(self, path: Path, mapping: dict | None = None, delimiter: str | None = None) -> dict:
        data: dict[str, Any] = {}
        if mapping:
            data["mapping"] = json.dumps(mapping)
        if delimiter:
            data["delimiter"] = delimiter
        with Path(path).open("rb") as fh:
            return self._request("POST", "/csv/preview", data=data, files={"file": (Path(path).name, fh, "text/csv")})

    def import_csv(self, path: Path, mapping: dict | None = None, delimiter: str | None = None, *, reject_invalid: bool = True, notes: str = "", recompute: bool = True) -> dict:
        data: dict[str, Any] = {"reject_invalid": "true" if reject_invalid else "false", "notes": notes, "recompute": "true" if recompute else "false"}
        if mapping:
            data["mapping"] = json.dumps(mapping)
        if delimiter:
            data["delimiter"] = delimiter
        with Path(path).open("rb") as fh:
            return self._request("POST", "/csv/import", data=data, files={"file": (Path(path).name, fh, "text/csv")})

    def csv_manual_row(self, **body: Any) -> dict:
        return self.post("/csv/manual-row", body)

    def csv_batches(self) -> list[dict]:
        return self.get("/csv/batches")

    def csv_rows(self, batch_id: int) -> list[dict]:
        return self.get(f"/csv/batches/{batch_id}/rows")

    def csv_deactivate(self, row_id: int, reason: str) -> dict:
        return self.post(f"/csv/rows/{row_id}/deactivate", {"reason": reason})

    def csv_reactivate(self, row_id: int, reason: str) -> dict:
        return self.post(f"/csv/rows/{row_id}/reactivate", {"reason": reason})

    # ------------------------------------------------------------- identity
    def lookup(self, **body: Any) -> dict:
        return self.post("/identity/lookup", body)

    def identity_table(self, limit: int = 500, offset: int = 0) -> list[dict]:
        return self.get("/identity", limit=limit, offset=offset)

    def merge_preview(self, survivor_id: int, merged_id: int) -> dict:
        return self.post("/identity/merge/preview", {"survivor_id": survivor_id, "merged_id": merged_id, "reason": "preview"})

    def merge(self, survivor_id: int, merged_id: int, reason: str, allow_uuid_conflict: bool = False) -> dict:
        return self.post("/identity/merge", {"survivor_id": survivor_id, "merged_id": merged_id, "reason": reason, "allow_uuid_conflict": allow_uuid_conflict})

    def unmerge(self, merge_id: int, note: str) -> dict:
        return self.post(f"/identity/unmerge/{merge_id}", {"note": note})

    def proxy_status(self) -> dict:
        return self.get("/identity/proxies")

    # ----------------------------------------------------------- conversion
    def run_conversion(self, sync: bool = False) -> dict:
        return self.post("/conversion/run", sync="true" if sync else None)

    def conversion_preview(self, included: bool | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
        return self.get("/conversion/preview", included=included, limit=limit, offset=offset)

    # --------------------------------------------------------------- export
    def export_preview(self) -> dict:
        return self.get("/export/preview")

    def export_write(self, path: str | None, staff_warning_acknowledged: bool, options: dict | None = None) -> dict:
        return self.post("/export/write", {"path": path, "staff_warning_acknowledged": staff_warning_acknowledged, "options": options or {}})

    def export_history(self, limit: int = 50) -> list[dict]:
        return self.get("/export/history", limit=limit)

    def export_detail(self, batch_id: int) -> dict:
        return self.get(f"/export/{batch_id}")

    # --------------------------------------------------------------- search
    def search(self, body: dict) -> dict:
        return self.post("/search", body)

    def audit(self, entity_type: str | None = None, entity_id: int | None = None, limit: int = 200) -> list[dict]:
        return self.get("/audit", entity_type=entity_type, entity_id=entity_id, limit=limit)

    # --------------------------------------------------------------- rescan
    def rescan_candidates(self, body: dict) -> dict:
        return self.post("/rescan/candidates", body)

    def rescan(self, body: dict) -> dict:
        return self.post("/rescan", body)

    def manual_correction(self, body: dict) -> dict:
        return self.post("/rescan/manual-correction", body)

    # ------------------------------------------------------------- settings
    def settings(self) -> dict:
        return self.get("/settings")

    def update_settings(self, values: dict, reason: str = "") -> dict:
        return self.put("/settings", {"values": values, "reason": reason})

    def reset_settings(self, keys: list[str] | None = None, prefix: str | None = None) -> dict:
        return self.post("/settings/reset", {"keys": keys or [], "prefix": prefix, "confirm": True})

    def settings_history(self, key: str | None = None, limit: int = 200) -> list[dict]:
        return self.get("/settings/history", key=key, limit=limit)

    def policy(self) -> dict:
        return self.get("/settings/policy")

    def policy_versions(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        return self.get("/settings/policy-versions", kind=kind, limit=limit)

    # ---------------------------------------------------------------- admin
    def api_keys(self) -> list[dict]:
        return self.get("/admin/keys")

    def create_api_key(self, name: str, role: str) -> dict:
        return self.post("/admin/keys", {"name": name, "role": role})

    def revoke_api_key(self, key_id: int) -> dict:
        return self.delete(f"/admin/keys/{key_id}")

    def backup(self, label: str = "") -> dict:
        return self.post("/admin/backup", label=label or None)

    def backups(self) -> list[dict]:
        return self.get("/admin/backups")

    def restore(self, backup_file: str) -> dict:
        return self.post("/admin/restore", {"backup_file": backup_file, "confirm": True})

    def discord_link(self, body: dict) -> dict:
        return self.post("/discord/link", body)
