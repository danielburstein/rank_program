from __future__ import annotations

from rankrecovery.console import AdminConsole
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.jobs.handlers import register_default_handlers
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext
from rankrecovery.services.images import ImageService
from tests.helpers import StubExtractor, png_bytes, tab


def make(tmp_path):
    db = Database(sqlite_url(tmp_path / "c.db"))
    db.run_migrations()
    stub = StubExtractor({"a.png": [tab("◇ Hero ◇ Player123")]})
    ctx = AppContext(db, extractor_factory=lambda _c, _o: stub)
    runner = JobRunner(ctx, workers=1)
    register_default_handlers(runner)
    runner.start()
    return ctx, runner


def test_console_commands(tmp_path):
    ctx, runner = make(tmp_path)
    try:
        out = []
        console = AdminConsole(ctx, runner, out=out.append)
        assert "status" in console.run("help")
        assert "players 0" in console.run("status")
        assert "unknown command" in console.run("frobnicate")
        assert "(not set)" in console.run("env")
        ImageService(ctx).import_bytes(png_bytes(1), filename="a.png", submitter_name="alice")
        msg = console.run("extract")
        assert msg.startswith("queued job #")
        job_id = int(msg.split("#")[1].split()[0])
        assert runner.wait(job_id)["status"] == "completed"
        assert "Player123" in console.run("players")
        assert "HERO" in console.run("player player123")
        assert "det " in console.run("image 1")
        assert "completed" in console.run(f"job {job_id}")
        assert "created key" in console.run("key create test admin")
        assert "role=admin" in console.run("keys")
        assert console.run("settings get extraction.vision_model").strip('"').startswith("claude")
        assert "recompute queued" in console.run('settings set verification.require_two_submitters_above_immortal true')
        assert "set to claude-sonnet-5-5" in console.run("vision model claude-sonnet-5-5")
        assert "no Anthropic API key" in console.run("vision test")
        assert "command(s)" in console.run("export preview")
        assert "no lookup attempts" in console.run("lookups Player123")
        (tmp_path / "usercache.json").write_text('[{"name": "Player123", "uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5"}]')
        out_text = console.run(f'identity-import "{tmp_path / "usercache.json"}"')
        assert "matched 1" in out_text and "recompute queued" in out_text
        assert "file not found" in console.run("identity-import nope.json")
        assert console.run("names").startswith("queued job #")
        assert "GLOBAL" in console.run("alias list")
        assert "now maps to HERO" in console.run("alias add HERO NEBULA")
        assert "already an alias" in console.run("alias add HERO NEBULA")
        assert "refused" in console.run("alias add LEGEND NEBULA")  # ambiguous alias rejected
        assert "removed from HERO" in console.run("alias remove HERO NEBULA")
        assert "unknown rank" in console.run("alias add NOPE X")
        assert "now maps to MOD" in console.run("staff-alias add MOD MODERATORR")
        assert console.run("reparse").startswith("queued job #")
        assert "no batches" in console.run("csv-batches")
        assert "no pending claims" in console.run("claims")
        assert "no such claim" in console.run("claim approve 99")
        assert "usage" in console.run("link-old OnlyOne")
        assert "no such batch" in console.run("csv-retire 99")
        assert "-> HERO" in console.run('package-map add "Mystery Crate" HERO')
        assert "'mystery crate' -> HERO" in console.run("package-map list")
        assert "no such player" in console.run("lookups Nobody")
        ctx.settings.set("ingest.folder", str(tmp_path / "newuploads"), changed_by="test")
        assert "no files waiting" in console.run("ingest")
        (tmp_path / "newuploads" / "drop.png").write_bytes(png_bytes(7))
        ingest_text = console.run("ingest --submitter bob")
        assert "1 new image(s) submitted" in ingest_text and "extraction queued as job #" in ingest_text
        assert not (tmp_path / "newuploads" / "drop.png").exists()
        assert "usage" in console.run("drive")
        assert "not a Google Drive" in console.run("drive https://example.com/x")
        assert console.run("lookup").startswith("queued job #")
        assert "could not parse" in console.run('player "unterminated')
    finally:
        runner.stop()


def test_job_status_text_lists_item_errors():
    from rankrecovery.bot.formatting import job_status_text

    text = job_status_text({"id": 1, "kind": "extract_images", "status": "completed", "total": 1, "done": 1, "failed": 1, "errors": [{"item": "image:1", "message": "Anthropic API error 401"}]})
    assert "image:1: Anthropic API error 401" in text
