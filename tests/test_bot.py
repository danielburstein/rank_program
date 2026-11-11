from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from rankrecovery.bot.api_client import ApiError, RankApiClient
from rankrecovery.bot.formatting import (
    FIELD_LIMIT,
    friendly_flag,
    job_status_text,
    rank_embed,
    submission_summary,
    truncate,
)


def sample_detail(**overrides):
    detail = {
        "player": {"player_id": 7, "display_name": "Player123", "aliases": ["Player123", "OldName"], "uuid": None, "identity_status": "unresolved", "is_bedrock": False},
        "summary": {
            "effective_rank": "IMMORTAL",
            "highest_observed": "SCUFFED",
            "observed_ranks": ["HERO", "SCUFFED"],
            "provisional": [{"rank": "SCUFFED", "counted": 1, "required": 2, "fallback": "IMMORTAL"}],
            "staff_ranks": ["MOD"],
            "custom_prefixes": ["ItsMob"],
            "clean_rank_images": 3,
            "distinct_submitters": 1,
            "ai_rank_images": 1,
            "csv_records": 0,
            "stale": False,
            "computed_at": "2026-11-05T01:00:00+00:00",
        },
        "conversion": {"new_rank": "bandit", "included": False, "reasons": ["blocked by flags: VERIFICATION_INSUFFICIENT_SOURCES"]},
        "flags": [
            {"code": "VERIFICATION_INSUFFICIENT_SOURCES", "details": "SCUFFED 1/2 sources", "is_active": True},
            {"code": "AI_DETECTED_HIGHER_RANK", "details": "INFINITE", "is_active": True},
            {"code": "OUT_OF_DATE", "details": "image 9", "is_active": False},
        ],
    }
    detail.update(overrides)
    return detail


def field_map(embed):
    return {f.name: f.value for f in embed.fields}


def test_rank_embed_public_view():
    embed = rank_embed(sample_detail(), staff_view=False)
    fields = field_map(embed)
    assert embed.title == "Rank summary for Player123"
    assert fields["Effective old rank"] == "IMMORTAL" and fields["Highest observed"] == "SCUFFED" and fields["New rank"] == "bandit"
    assert fields["All observed ranks"] == "HERO, SCUFFED"
    assert fields["Staff prefixes"] == "MOD" and fields["Custom prefixes"] == "ItsMob"
    assert "SCUFFED 1/2 sources (counts as IMMORTAL for now)" in fields["Provisional (needs more sources)"]
    assert "independent submitters: 1" in fields["Evidence"]
    assert "Export eligibility" not in fields
    notes = fields["Notes"]
    assert "Needs a second independent screenshot submitter for SCUFFED 1/2 sources" in notes
    assert "VERIFICATION_INSUFFICIENT_SOURCES" not in notes  # friendly text only
    assert "OUT_OF_DATE" not in notes  # resolved flag hidden
    assert embed.footer.text.startswith("computed 2026-11-05")


def test_rank_embed_staff_view_and_stale_footer():
    detail = sample_detail()
    detail["summary"]["stale"] = True
    embed = rank_embed(detail, staff_view=True)
    fields = field_map(embed)
    assert fields["Export eligibility"].startswith("excluded: blocked by flags")
    assert "VERIFICATION_INSUFFICIENT_SOURCES: SCUFFED 1/2 sources" in fields["Active flags"]
    assert embed.footer.text == "Rank summary is stale – a recompute is pending"


def test_rank_embed_truncates_long_fields_and_limits_field_count():
    detail = sample_detail()
    detail["summary"]["observed_ranks"] = [f"RANK{i:04d}" for i in range(400)]
    detail["flags"] = [{"code": f"FLAG_{i}", "details": "x" * 50, "is_active": True} for i in range(100)]
    embed = rank_embed(detail, staff_view=True)
    assert len(embed.fields) <= 25
    for f in embed.fields:
        assert len(f.value) <= FIELD_LIMIT
    assert field_map(embed)["All observed ranks"].endswith("…")
    assert truncate("abc", 3) == "abc" and truncate("abcd", 3) == "ab…"


def test_friendly_flag_fallback():
    assert friendly_flag({"code": "SOMETHING_NEW", "details": "d"}) == "SOMETHING_NEW (d)"
    assert friendly_flag({"code": "BEDROCK_PLAYER"}).startswith("Bedrock player")


def test_submission_summary_and_job_status():
    result = {
        "outcomes": [
            {"filename": "a.png", "image_id": 1, "created": True, "ai_status": "no_ai_indicator_found", "date_status": "unknown", "warnings": []},
            {"filename": "b.png", "image_id": 1, "created": False, "duplicate_of": 1, "ai_status": "ai_indicator_found", "date_status": "ok", "warnings": ["identical image already imported under another submitter; it counts as one source"]},
            {"filename": "c.txt", "error": "cannot decode c.txt"},
        ],
        "job_id": 42,
    }
    text = submission_summary(result, submitter="alice")
    assert "Recorded submitter: **alice**" in text
    assert "imported 1 · already known 1 · errors 1" in text
    assert "a.png: imported (no reliable date)" in text
    assert "duplicate of image 1 (counts once) (AI indicator found; identical image" in text
    assert "c.txt: error – cannot decode c.txt" in text and "Extraction job: #42" in text
    assert "No extraction job" in submission_summary({"outcomes": [], "job_id": None}, submitter=None)

    job = {"id": 42, "kind": "extract_images", "status": "running", "total": 10, "done": 4, "failed": 1, "progress_message": "4/10 images"}
    assert job_status_text(job) == "Job #42 (extract_images): **running** · 4/10 done, 1 failed · 4/10 images"
    done = {"id": 42, "kind": "extract_images", "status": "completed", "total": 10, "done": 10, "failed": 0, "result": {"images": 10, "failed": 0, "players_recomputed": 6}}
    assert "result: images 10, failed 0, players_recomputed 6" in job_status_text(done)
    assert "error: boom" in job_status_text({"id": 1, "kind": "x", "status": "failed", "error": "boom"})


# --------------------------------------------------------------------------- api client


def make_client(handler):
    return RankApiClient("http://server.test", "rr_testkey", transport=httpx.MockTransport(handler))


async def test_player_by_name_and_404():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("x-api-key")
        if request.url.path.endswith("/players/by-name/Player123"):
            return httpx.Response(200, json={"player": {"display_name": "Player123"}})
        return httpx.Response(404, json={"detail": "player not found"})

    client = make_client(handler)
    try:
        assert (await client.player_by_name("Player123"))["player"]["display_name"] == "Player123"
        assert await client.player_by_name("Nobody") is None
        assert seen["auth"] == "rr_testkey"
    finally:
        await client.aclose()


async def test_submit_flow_creates_batch_then_uploads():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("content-type", ""), request.content))
        if request.url.path == "/api/v1/batches":
            assert b"source" in request.content and b"bot" in request.content and b"alice" in request.content
            return httpx.Response(200, json={"batch_id": 7})
        if request.url.path == "/api/v1/images":
            body = request.content
            assert b'name="batch_id"' in body and b"\r\n7\r\n" in body
            assert b'name="submitter"' in body and b"alice" in body
            assert b'name="discord_user_id"' in body and b"123" in body
            assert b'name="files"; filename="shot.png"' in body and b"image/png" in body
            return httpx.Response(200, json={"outcomes": [{"filename": "shot.png", "image_id": 1, "created": True}], "job_id": 9})
        return httpx.Response(500, text="unexpected")

    client = make_client(handler)
    try:
        result = await client.submit_images([("shot.png", b"\x89PNG fake")], submitter="alice", discord_user_id="123", batch_label="test")
    finally:
        await client.aclose()
    assert [c[1] for c in calls] == ["/api/v1/batches", "/api/v1/images"]
    assert calls[1][2].startswith("multipart/form-data")
    assert result["job_id"] == 9 and result["batch_id"] == 7


async def test_errors_are_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            raise httpx.ConnectError("connection refused")
        return httpx.Response(403, json={"detail": "requires role staff"})

    client = make_client(handler)
    try:
        with pytest.raises(ApiError) as exc:
            await client.health()
        assert exc.value.status == 0 and "ConnectError" in exc.value.message
        with pytest.raises(ApiError) as exc:
            await client.dashboard()
        assert exc.value.status == 403 and exc.value.message == "requires role staff"
    finally:
        await client.aclose()


async def test_link_and_unlink():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/discord/link":
            body = json.loads(request.content)
            return httpx.Response(200, json={**body, "player_id": None})
        if request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "link not found"})

    client = make_client(handler)
    try:
        link = await client.link("1", "user#1", "Player123", verified=True)
        assert link["username"] == "Player123" and link["verified"] is True
        assert await client.get_link("1") is None
        assert await client.unlink("1") is True
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- bot wiring


@dataclass
class FakeSettings:
    staff_role_ids: list[int] = field(default_factory=list)
    admin_role_ids: list[int] = field(default_factory=list)
    guild_ids: list[int] = field(default_factory=list)
    max_attachments_per_submit: int = 10
    max_attachment_mb: int = 25
    lookup_cooldown_seconds: float = 5.0
    public_lookup: bool = True
    ephemeral_replies: bool = False


class FakeApi:
    async def aclose(self) -> None:
        return None


async def test_every_command_fits_discord_limits():
    """Discord rejects the whole sync when one description is over 100 characters (seen live on /rr lookup)."""
    from discord import app_commands

    from rankrecovery.bot.main import create_bot

    bot = await create_bot(FakeSettings(), FakeApi())
    try:
        problems: list[str] = []

        def check(command, path: str) -> None:
            if isinstance(command, app_commands.Group):
                for sub in command.commands:
                    check(sub, f"{path} {sub.name}")
                return
            if isinstance(command, app_commands.ContextMenu):
                if len(command.name) > 32:
                    problems.append(f"{path}: name too long")
                return
            if not 1 <= len(command.description) <= 100:
                problems.append(f"{path}: description {len(command.description)} chars")
            if len(command.name) > 32:
                problems.append(f"{path}: name too long")
            for param in getattr(command, "parameters", []):
                if not 1 <= len(param.description) <= 100:
                    problems.append(f"{path} <{param.name}>: description {len(param.description)} chars")

        for command in bot.tree.get_commands():
            check(command, f"/{command.name}")
        assert problems == [], problems
    finally:
        await bot.close()


async def test_create_bot_registers_commands():
    from discord import AppCommandType

    from rankrecovery.bot.main import create_bot

    bot = await create_bot(FakeSettings(), FakeApi())
    try:
        names = {c.name for c in bot.tree.get_commands()}
        assert {"rank", "myrank", "link", "unlink", "claim", "submit", "rr"} <= names
        rr = next(c for c in bot.tree.get_commands() if c.name == "rr")
        assert {c.name for c in rr.commands} == {"status", "job", "recompute", "find", "lookup", "ingest", "drive", "uuidranks", "claims", "approve", "reject", "link-old"}
        assert {c.name for c in bot.tree.get_commands(type=AppCommandType.user)} == {"Rank lookup"}
        assert {c.name for c in bot.tree.get_commands(type=AppCommandType.message)} == {"Submit screenshots"}
        submit = next(c for c in bot.tree.get_commands() if c.name == "submit")
        assert [p.name for p in submit.parameters][:5] == ["screenshot1", "screenshot2", "screenshot3", "screenshot4", "screenshot5"]
        assert submit.parameters[0].required and not submit.parameters[1].required
    finally:
        await bot.close()


def test_staff_check_logic():
    from types import SimpleNamespace

    from rankrecovery.bot.cogs import is_staff_member

    settings = FakeSettings(staff_role_ids=[10])
    admin = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True), roles=[])
    staff = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[SimpleNamespace(id=10)])
    member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[SimpleNamespace(id=99)])
    assert is_staff_member(admin, settings) and is_staff_member(staff, settings) and not is_staff_member(member, settings)
    assert not is_staff_member(None, settings)


def test_settings_parse_lists(monkeypatch):
    from rankrecovery.bot.config import BotSettings

    monkeypatch.setenv("RANKRECOVERY_BOT_DISCORD_TOKEN", "tok-secret-value")
    monkeypatch.setenv("RANKRECOVERY_BOT_API_KEY", "rr_secret")
    monkeypatch.setenv("RANKRECOVERY_BOT_STAFF_ROLE_IDS", "1, 2,3")
    monkeypatch.setenv("RANKRECOVERY_BOT_GUILD_IDS", "[42]")
    settings = BotSettings(_env_file=None)
    assert settings.staff_role_ids == [1, 2, 3] and settings.guild_ids == [42] and settings.admin_role_ids == []
    assert "tok-secret-value" not in repr(settings) and "rr_secret" not in str(settings)
    assert settings.secrets() == ["tok-secret-value", "rr_secret"]
