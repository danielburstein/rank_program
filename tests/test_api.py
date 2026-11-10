from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from rankrecovery.api.app import create_app
from rankrecovery.api.auth import create_api_key
from rankrecovery.core.enums import ApiRole, LeadingIcon, TrailingIcon
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.services.context import AppContext
from tests.helpers import StubExtractor, png_bytes, tab


@pytest.fixture
def client(tmp_path):
    db = Database(sqlite_url(tmp_path / "api.db"))
    db.run_migrations()
    rows = {
        "a.png": [tab("◇ Hero ◇ Player123"), tab("◇ Mod ◇ Staffy", trailing=TrailingIcon.YINYANG)],
        "b.png": [tab("◇ Scuffed ◇ Player123", leading=LeadingIcon.YINYANG)],
        "c.png": [tab("◇ Scuffed ◇ Player123", leading=LeadingIcon.YINYANG)],
    }
    stub = StubExtractor(rows)
    ctx = AppContext(db, extractor_factory=lambda _c, _o: stub)
    ctx.stub = stub  # type: ignore[attr-defined]
    ctx.settings.set("verification.require_two_submitters_above_immortal", True, changed_by="test")
    app = create_app(ctx, job_workers=1)
    admin_key, _ = create_api_key(ctx, name="admin", role=ApiRole.ADMIN)
    bot_key, _ = create_api_key(ctx, name="bot", role=ApiRole.BOT)
    ro_key, _ = create_api_key(ctx, name="ro", role=ApiRole.READONLY)
    with TestClient(app) as c:
        c.keys = {"admin": {"X-API-Key": admin_key}, "bot": {"X-API-Key": bot_key}, "ro": {"X-API-Key": ro_key}}  # type: ignore[attr-defined]
        c.ctx = ctx  # type: ignore[attr-defined]
        yield c
    db.dispose()


def wait_job(client: TestClient, job_id: int) -> dict:
    return client.app.state.runner.wait(job_id, timeout=60)


def upload(client: TestClient, name: str, seed: int, submitter: str, headers: dict) -> dict:
    files = [("files", (name, io.BytesIO(png_bytes(seed)), "image/png"))]
    r = client.post("/api/v1/images", files=files, data={"submitter": submitter}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_ingest_endpoints(client, tmp_path):
    admin = client.keys["admin"]
    client.ctx.settings.set("ingest.folder", str(tmp_path / "newuploads"), changed_by="test")
    assert client.post("/api/v1/ingest/folder", json={}, headers=client.keys["ro"]).status_code == 403
    r = client.get("/api/v1/ingest/folder", headers=admin)
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["folder"].endswith("newuploads")
    (tmp_path / "newuploads" / "a.png").write_bytes(png_bytes(1))
    (tmp_path / "newuploads" / "junk.txt").write_text("x")
    assert client.get("/api/v1/ingest/folder", headers=admin).json()["waiting"] == ["a.png", "junk.txt"]
    r = client.post("/api/v1/ingest/folder", json={"submitter": "alice"}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 1 and body["ignored"] == 1 and body["job_id"]
    assert wait_job(client, body["job_id"])["status"] == "completed"
    assert client.get("/api/v1/players/by-name/player123", headers=admin).status_code == 200
    assert not (tmp_path / "newuploads" / "a.png").exists()
    r = client.post("/api/v1/ingest/drive", json={"url": "https://example.com/nope"}, headers=admin)
    assert r.status_code == 400 and "Google Drive" in r.json()["detail"]


def test_auth_and_roles(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/dashboard").status_code == 401
    assert client.get("/api/v1/dashboard", headers=client.keys["ro"]).status_code == 200
    assert client.post("/api/v1/images", files=[("files", ("x.png", io.BytesIO(png_bytes(1)), "image/png"))], headers=client.keys["ro"]).status_code == 403
    assert client.get("/api/v1/settings", headers=client.keys["bot"]).status_code == 403
    assert client.get("/api/v1/settings", headers=client.keys["admin"]).status_code == 200


def test_submit_lookup_review_export_flow(client, tmp_path):
    admin = client.keys["admin"]
    bot = client.keys["bot"]
    out = upload(client, "a.png", 1, "alice", bot)
    assert out["outcomes"][0]["created"] and out["job_id"]
    assert wait_job(client, out["job_id"])["status"] == "completed"
    out = upload(client, "b.png", 2, "alice", bot)
    wait_job(client, out["job_id"])
    r = client.get("/api/v1/players/by-name/player123", headers=bot)
    assert r.status_code == 200
    data = r.json()
    assert data["summary"]["effective_rank"] == "IMMORTAL" and data["summary"]["provisional"][0]["rank"] == "SCUFFED"
    assert client.get("/api/v1/players/by-name/nobody", headers=bot).status_code == 404

    # duplicate content under a different submitter does not add a source
    dup = upload(client, "copy.png", 2, "bob", bot)
    assert not dup["outcomes"][0]["created"] and dup["job_id"] is None
    r = client.post(f"/api/v1/players/{data['player']['player_id']}/recompute", headers=admin)
    assert r.json()["summary"]["effective_rank"] == "IMMORTAL"

    # second genuine submitter confirms SCUFFED
    out = upload(client, "c.png", 3, "bob", bot)
    wait_job(client, out["job_id"])
    data = client.get("/api/v1/players/by-name/Player123", headers=bot).json()
    assert data["summary"]["effective_rank"] == "SCUFFED"

    # table + search + image context + overlay
    table = client.get("/api/v1/players", headers=client.keys["ro"]).json()
    assert table["total"] >= 2 and any(r["display_name"] == "Player123" for r in table["rows"])
    search = client.post("/api/v1/search", json={"text": "player", "entity_types": ["player"]}, headers=client.keys["ro"]).json()
    assert search["players"]
    image_id = data["images"][0]["image_id"]
    ctxinfo = client.get(f"/api/v1/images/{image_id}", headers=client.keys["ro"]).json()
    assert ctxinfo["detections"] and ctxinfo["image"]["submitter"] in ("alice", "bob")
    overlay = client.get(f"/api/v1/images/{image_id}/overlay", params={"highlight": "Player123"}, headers=client.keys["ro"])
    assert overlay.status_code == 200 and overlay.headers["content-type"] == "image/png"

    # discord link
    r = client.post("/api/v1/discord/link", json={"discord_user_id": "123", "discord_username": "dan", "username": "Player123"}, headers=bot)
    assert r.status_code == 200 and r.json()["player_id"] == data["player"]["player_id"]
    assert client.get("/api/v1/discord/link/123", headers=bot).json()["username"] == "Player123"

    # manual uuid, settings change triggers recompute job, conversion, export
    pid = data["player"]["player_id"]
    r = client.post(f"/api/v1/players/{pid}/uuid", json={"uuid": "069a79f444e94726a5befca90e38aaf5", "reason": "known"}, headers=admin)
    assert r.status_code == 200
    r = client.put("/api/v1/settings", json={"values": {"conversion.allowed_flags": ["STAFF_AND_PLAYER_RANK_FOUND", "DATE_UNKNOWN", "DUPLICATE_IMAGE"]}, "reason": "test"}, headers=admin)
    assert r.status_code == 200 and r.json()["job_id"]
    wait_job(client, r.json()["job_id"])
    client.post("/api/v1/conversion/run", params={"sync": "true"}, headers=admin)
    preview = client.get("/api/v1/export/preview", headers=admin).json()
    assert preview["command_count"] == 1 and preview["commands"][0] == "lp user 069a79f4-44e9-4726-a5be-fca90e38aaf5 parent set Warlord"
    r = client.post("/api/v1/export/write", json={"path": str(tmp_path / "cmds.txt"), "staff_warning_acknowledged": False}, headers=admin)
    assert r.status_code in (200, 409)
    r = client.post("/api/v1/export/write", json={"path": str(tmp_path / "cmds.txt"), "staff_warning_acknowledged": True}, headers=admin)
    assert r.status_code == 200 and (tmp_path / "cmds.txt").read_text().startswith("lp user ")
    assert client.get("/api/v1/export/history", headers=admin).json()[0]["command_count"] == 1

    # settings: secrets masked; policy validation rejects ambiguous aliases
    settings = client.get("/api/v1/settings", headers=admin).json()
    assert "vision.anthropic_api_key" in settings["secrets"]
    bad = client.put("/api/v1/settings", json={"values": {"ranks.player_ranks": [{"name": "HERO", "aliases": ["X"]}, {"name": "LEGEND", "aliases": ["X"]}, {"name": "SCUFFED"}]}}, headers=admin)
    assert bad.status_code == 400
    r = client.put("/api/v1/settings", json={"values": {"vision.anthropic_api_key": "sk-ant-test"}}, headers=admin)
    assert r.status_code == 200
    assert client.get("/api/v1/settings", headers=admin).json()["values"]["vision.anthropic_api_key"] == "***"
    assert client.get("/api/v1/settings", headers=admin).json()["secrets_configured"]["vision.anthropic_api_key"] is True

    # jobs list and audit
    assert client.get("/api/v1/jobs", headers=bot).json()
    assert client.get("/api/v1/audit", params={"entity_type": "player"}, headers=admin).json()
    assert client.get("/api/v1/admin/logs", headers=admin).status_code == 200


def test_csv_preview_and_import_endpoints(client):
    admin = client.keys["admin"]
    csv = b"username,rank\nPlayer123,SCUFFED\nBad!,HERO\n"
    r = client.post("/api/v1/csv/preview", files={"file": ("p.csv", io.BytesIO(csv), "text/csv")}, headers=admin)
    assert r.status_code == 200 and r.json()["summary"]["valid"] == 1 and r.json()["summary"]["invalid"] == 1
    r = client.post("/api/v1/csv/import", files={"file": ("p.csv", io.BytesIO(csv), "text/csv")}, data={"mapping": json.dumps({"username": "username", "rank": "rank", "uuid": None, "purchase_date": None, "transaction_id": None})}, headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "imported"
    wait_job(client, r.json()["job_id"])
    data = client.get("/api/v1/players/by-name/Player123", headers=admin).json()
    assert data["summary"]["effective_rank"] == "SCUFFED" and data["csv_rows"]
    batches = client.get("/api/v1/csv/batches", headers=admin).json()
    rows = client.get(f"/api/v1/csv/batches/{batches[0]['id']}/rows", headers=admin).json()
    assert len(rows) == 2


def test_pending_detections_review_flow(client):
    admin = client.keys["admin"]
    client.ctx.stub.rows_by_filename["m.png"] = [tab("◇ HER0 ◇ Misread1")]  # type: ignore[attr-defined]
    out = upload(client, "m.png", 11, "alice", admin)
    wait_job(client, out["job_id"])
    pending = client.get("/api/v1/detections/pending", headers=admin).json()
    assert len(pending) == 1 and pending[0]["username"] == "Misread1" and pending[0]["submitter"] == "alice" and pending[0]["flags"]
    assert client.get("/api/v1/detections/pending", params={"username": "misread1"}, headers=admin).json()
    r = client.post(f"/api/v1/detections/{pending[0]['detection_id']}/review", json={"accept": True, "note": "clearly HERO"}, headers=admin)
    assert r.status_code == 200 and r.json()["player_id"]
    assert client.get("/api/v1/detections/pending", headers=admin).json() == []
    assert client.get("/api/v1/players/by-name/Misread1", headers=admin).json()["summary"]["effective_rank"] == "HERO"


def test_rescan_endpoints(client):
    admin = client.keys["admin"]
    out = upload(client, "a.png", 1, "alice", admin)
    wait_job(client, out["job_id"])
    r = client.post("/api/v1/rescan/candidates", json={"username": "Player12", "mode": "all"}, headers=admin)
    assert r.status_code == 200 and r.json()["candidates"]
    image_id = out["outcomes"][0]["image_id"]
    r = client.post("/api/v1/rescan/manual-correction", json={"image_id": image_id, "username": "Zeyos4Danger", "prefix_text": "Galactic", "rank": "GALACTIC", "note": "read from original"}, headers=admin)
    assert r.status_code == 200 and r.json()["detection_id"]
    assert client.get("/api/v1/players/by-name/Zeyos4Danger", headers=admin).json()["summary"]["effective_rank"] == "IMMORTAL"
    r = client.post("/api/v1/rescan", json={"username": "Player123", "mode": "all", "full": True}, headers=admin)
    assert wait_job(client, r.json()["job_id"])["status"] == "completed"
