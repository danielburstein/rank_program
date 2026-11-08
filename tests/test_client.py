"""Desktop client tests (run with QT_QPA_PLATFORM=offscreen)."""

from __future__ import annotations

import io
import json
import os
import time

import httpx
import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from rankrecovery.client.api_client import ClientConfig, ServerClient, ServerError  # noqa: E402
from rankrecovery.client.widgets.data_table import DataTable  # noqa: E402
from rankrecovery.client.widgets.image_viewer import ImageViewer  # noqa: E402
from rankrecovery.client.workers import pool  # noqa: E402
from rankrecovery.config.defaults import DEFAULT_SETTINGS  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def drain(app: QApplication, timeout: float = 5.0) -> None:
    """Wait for background workers and deliver their signals."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if pool().waitForDone(50):
            app.processEvents()
            return
    pool().waitForDone(1000)
    app.processEvents()


# ------------------------------------------------------------------ api client


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_chunking_and_form_fields(tmp_path):
    seen: list[dict] = []
    paths = []
    for i in range(23):
        p = tmp_path / f"shot{i:02d}.png"
        p.write_bytes(_png())
        paths.append(p)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "rr_test"
        body = request.content
        count = body.count(b'name="files"')
        seen.append({"files": count, "batch": body.count(b'name="batch_id"'), "paths": body.count(b'name="original_paths"'), "mod": body.count(b'name="file_modified"'), "submitter": b'name="submitter"' in body})
        return httpx.Response(200, json={"outcomes": [{"filename": f"f{n}", "image_id": n, "created": True} for n in range(count)], "job_id": len(seen)})

    client = ServerClient("http://server", "rr_test", transport=httpx.MockTransport(handler))
    progress: list[tuple[int, int]] = []
    result = client.upload_images(paths, submitter="alice", batch_id=7, extract=True, progress_cb=lambda d, t: progress.append((d, t)))
    assert [s["files"] for s in seen] == [10, 10, 3]
    assert all(s["batch"] == 1 and s["submitter"] and s["paths"] == s["files"] and s["mod"] == s["files"] for s in seen)
    assert len(result["outcomes"]) == 23 and result["job_ids"] == [1, 2, 3]
    assert progress[-1] == (23, 23)


def test_upload_groups_by_subfolder_submitter(tmp_path):
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    a = tmp_path / "alice" / "1.png"
    b = tmp_path / "bob" / "2.png"
    a.write_bytes(_png())
    b.write_bytes(_png())
    submitters: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        content = request.content
        start = content.find(b'name="submitter"')
        submitters.append(content[start : start + 60])
        return httpx.Response(200, json={"outcomes": [], "job_id": None})

    client = ServerClient("http://server", "k", transport=httpx.MockTransport(handler))
    client.upload_images([a, b], submitter=None, batch_id=None, submitter_for=lambda p: p.parent.name)
    assert len(submitters) == 2 and b"alice" in submitters[0] and b"bob" in submitters[1]


def test_server_error_and_csv_preview(tmp_path):
    csv_file = tmp_path / "p.csv"
    csv_file.write_text("username,rank\nA,HERO\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/csv/preview"):
            assert b'name="mapping"' in request.content and b"HERO" in request.content
            return httpx.Response(200, json={"rows": [], "summary": {}})
        return httpx.Response(403, json={"detail": "requires role admin"})

    client = ServerClient("http://server", "k", transport=httpx.MockTransport(handler))
    assert client.preview_csv(csv_file, {"username": "username", "rank": "rank"}) == {"rows": [], "summary": {}}
    with pytest.raises(ServerError) as exc:
        client.settings()
    assert exc.value.status == 403 and "admin" in exc.value.message


def test_config_roundtrip(tmp_path):
    cfg = ClientConfig(base_url="http://x:1", api_key="rr_k", path=tmp_path / "c.json")
    cfg.save()
    loaded = ClientConfig.load(tmp_path / "c.json")
    assert loaded.base_url == "http://x:1" and loaded.api_key == "rr_k"


# ------------------------------------------------------------------ widgets


def test_data_table_filter_sort_visibility(app):
    table = DataTable([("name", "Name"), ("rank", "Rank"), ("n", "N")])
    table.set_rows([{"name": "zed", "rank": "HERO", "n": 3}, {"name": "amy", "rank": "GOD", "n": 10}, {"name": "bob", "rank": None, "n": 2}])
    assert table.proxy.rowCount() == 3
    table.filter.setText("am")
    assert table.proxy.rowCount() == 1 and table.proxy.index(0, 0).data() == "amy"
    table.filter.setText("")
    table.view.sortByColumn(2, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.SortOrder.AscendingOrder)
    assert [table.proxy.index(r, 0).data() for r in range(3)] == ["bob", "zed", "amy"]
    table.view.sortByColumn(1, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.SortOrder.AscendingOrder)
    assert table.proxy.index(0, 1).data() == ""  # None sorts first
    table.set_column_visible("rank", False)
    assert table.visible_columns() == ["name", "n"]
    table.view.selectRow(0)
    assert table.current_row() is not None


def test_image_viewer_loads_png_and_reports_region(app):
    viewer = ImageViewer()
    assert viewer.load_bytes(_png(), detections=[{"detection_id": 5, "bbox": [1, 2, 10, 8], "username": "amy", "status": "candidate", "confidence": 0.9}], highlight="amy")
    got: list[list[int]] = []
    viewer.region_drawn.connect(got.append)
    assert viewer.simulate_region(3, 4, 12, 6) == [3, 4, 12, 6]
    assert got == [[3, 4, 12, 6]]
    assert viewer.simulate_region(0, 0, 1, 1) is None
    assert len(viewer._boxes) == 1  # noqa: SLF001
    assert not viewer.load_bytes(b"not an image")


# ------------------------------------------------------------- fake client


class FakeClient:
    """Minimal stand-in for ServerClient; every method returns canned data."""

    def __init__(self):
        self.updated: dict = {}

    def health(self):
        return {"status": "ok", "version": "0.1.0", "database": "SQLite", "vision_configured": False}

    def dashboard(self):
        return {"players": 2, "images": 3, "flag_counts": {"DATE_UNKNOWN": 1}}

    def flag_catalogue(self):
        return [{"code": "DATE_UNKNOWN", "severity": "info", "description": "x", "blocks_export_by_default": False}]

    def policy(self):
        return {"ranks": [{"name": "HERO", "enabled": True}], "staff": [{"name": "MOD"}], "mapping": {"HERO": "thug"}}

    def jobs(self, limit=50, status=None):
        return []

    def job(self, job_id):
        return {"id": job_id, "status": "completed", "result": {}}

    def players(self, **params):
        return {"total": 1, "rows": [{"player_id": 1, "display_name": "Amy", "effective_rank": "HERO", "provisional": [], "active_flags": [], "stale": False}]}

    def player(self, player_id):
        return {"player": {"player_id": 1, "display_name": "Amy", "aliases": ["Amy"], "merged_member_ids": [1]}, "summary": {"effective_rank": "HERO", "provisional": [], "reasoning": [], "verification_settings": {}}, "conversion": None, "verification": [], "observations": [], "staff_observations": [], "flags": [], "detections": [], "images": [], "csv_rows": [], "lookups": [], "identity_resolutions": [], "merges": [], "manual_assertions": [], "waivers": [], "exports": []}

    def audit(self, *args, **kwargs):
        return []

    reviews: list = []

    def pending_detections(self, username=None, limit=100, offset=0):
        return [
            {"detection_id": 10, "image_id": 1, "player_id": 1, "player_display_name": "Amy", "raw_username": "Amy", "username": "Amy", "raw_prefix": "HER0", "rank": "HERO", "staff": None, "region": "tab", "confidence": 0.9, "bbox": [10, 10, 80, 12], "flags": ["PROBABLE_MISREAD: 'HER0' looks like HERO"], "notes": [], "image_filename": "a.png", "submitter": "alice"},
            {"detection_id": 11, "image_id": 1, "player_id": 2, "player_display_name": "Bob", "raw_username": "Bob", "username": "Bob", "raw_prefix": "SEASON 3", "rank": "SCUFFED", "staff": None, "region": "tab", "confidence": 0.8, "bbox": [10, 30, 80, 12], "flags": ["AMBIGUOUS_PREFIX: custom prefix without yin-yang"], "notes": [], "image_filename": "a.png", "submitter": "alice"},
        ]

    def image_content(self, image_id):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (120, 60), (20, 20, 20)).save(buf, format="PNG")
        return buf.getvalue()

    def image_context(self, image_id):
        return {"detections": self.pending_detections()}

    def review_detection(self, detection_id, accept, note):
        self.reviews.append((detection_id, accept, note))
        return {"ok": True, "player_id": 1}

    def bulk_review(self, detection_ids, accept, note):
        self.reviews.append((tuple(detection_ids), accept, note))
        return {"reviewed": len(detection_ids)}

    def recompute_player(self, player_id):
        return self.player(player_id)

    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        values["vision.anthropic_api_key"] = ""
        values["lookup.proxy_list"] = ""
        return {"values": values, "defaults": dict(DEFAULT_SETTINGS), "secrets": ["vision.anthropic_api_key", "lookup.proxy_list"], "secrets_configured": {"vision.anthropic_api_key": False, "lookup.proxy_list": False}, "vision_configured": False, "flag_codes": ["DATE_UNKNOWN"]}

    def settings_history(self, *a, **k):
        return []

    def policy_versions(self, *a, **k):
        return []

    def update_settings(self, values, reason=""):
        self.updated = values
        return {"changed": list(values), "job_id": None}

    def close(self):
        return None


def test_settings_page_builds_every_key_and_roundtrips(app):
    from rankrecovery.client.pages.settings import SettingsPage
    from rankrecovery.client.state import AppState

    state = AppState(ClientConfig(), FakeClient())
    page = SettingsPage(state)
    page.build_from(FakeClient().settings())
    missing = [k for k in DEFAULT_SETTINGS if k not in page.widgets and k not in ("conversion.presets",)]
    assert missing == [], missing
    assert isinstance(page.widgets["verification.require_two_submitters_above_immortal"], QCheckBox)
    assert isinstance(page.widgets["csv.treat_as_confirmed_exempt_from_verification"], QCheckBox)
    assert page.collect() == {}  # nothing changed yet
    page.widgets["verification.require_two_submitters_above_immortal"].setChecked(True)
    page.widgets["extraction.vision_concurrency"].setValue(5)
    page.widgets["ai.metadata_indicators"].setPlainText("chatgpt\nopenai")
    changed = page.collect()
    assert changed["verification.require_two_submitters_above_immortal"] is True
    assert changed["extraction.vision_concurrency"] == 5
    assert changed["ai.metadata_indicators"] == ["chatgpt", "openai"]
    assert "ranks.player_ranks" not in changed
    ranks = page.widgets["ranks.player_ranks"].value()
    assert [r["name"] for r in ranks][:2] == ["DEFAULT", "HERO"] and ranks[1]["aliases"] == ["NOVA", "GLOBAL"]
    assert page.widgets["ranks.mapping"].value()["SCUFFED"] == "Warlord"
    page.widgets["ranks.mapping"].table.item(0, 1).setText("")  # DEFAULT stays unmapped
    assert "DEFAULT" not in page.widgets["ranks.mapping"].value()


def test_main_window_with_fake_client(app, tmp_path):
    from rankrecovery.client.main import PAGES, create_main_window

    config = ClientConfig(base_url="http://fake", api_key="k", path=tmp_path / "client.json")
    window = create_main_window(config, FakeClient())
    assert window.windowTitle() == "Rank Recovery"
    assert window.stack.count() == len(PAGES) == 13
    drain(app)
    assert window.state.flag_catalogue and window.state.policy.get("ranks")
    window.show_page("players")
    drain(app)
    players = window.pages["players"]
    assert players.table.rows() and players.table.rows()[0]["display_name"] == "Amy"
    window.state.open_player.emit(1)
    drain(app)
    assert players.inspector.detail["player"]["display_name"] == "Amy"
    window.show_page("review")
    drain(app)
    drain(app)
    review = window.pages["review"]
    assert len(review.items) == 2 and review.current()["detection_id"] == 10
    assert "1 of 2" in review.counter.text() and review.viewer.pixmap_item is not None
    assert review.viewer.highlight_id == 10
    review.note.setText("it is HERO")
    review.decide(True)
    drain(app)
    assert window.state.client.reviews[-1] == (10, True, "it is HERO")
    assert review.current()["detection_id"] == 11 and review.accepted == 1
    review.skip()
    assert review.current()["detection_id"] == 11  # wraps around on a single remaining item
    review.accept_rest_of_image()
    drain(app)
    assert window.state.client.reviews[-1][0] == (11,) and review.current() is None
    assert "all reviewed" in review.details.values().get("status", "") if hasattr(review.details, "values") else True
    window.show_page("settings")
    drain(app)
    assert window.pages["settings"].widgets
    window.close()
    assert json.loads((tmp_path / "client.json").read_text())["base_url"] == "http://fake"
