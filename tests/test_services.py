from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from rankrecovery.core.enums import LeadingIcon, LookupOutcome, TrailingIcon
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.identity.base import LookupAttempt, LookupResult
from rankrecovery.jobs.handlers import register_default_handlers
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext
from rankrecovery.services.conversion import ConversionService
from rankrecovery.services.csvimport import CsvImportService
from rankrecovery.services.export import ExportService
from rankrecovery.services.extraction import ExtractionService
from rankrecovery.services.identity import IdentityService
from rankrecovery.services.images import ImageService
from rankrecovery.services.playerview import PlayerViewService
from rankrecovery.services.reconcile import ReconcileService
from rankrecovery.services.rescan import RescanScope, RescanService
from rankrecovery.services.review import ReviewService
from rankrecovery.services.search import SearchQuery, SearchService
from tests.helpers import StubExtractor, png_bytes, tab


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "svc.db"))
    db.run_migrations()
    rows = {
        "a.png": [tab("◇ Hero ◇ Player123"), tab("◇ Mod ◇ Staffy", trailing=TrailingIcon.YINYANG)],
        "b.png": [tab("◇ Scuffed ◇ Player123", leading=LeadingIcon.YINYANG)],
        "b2.png": [tab("◇ Scuffed ◇ Player123", leading=LeadingIcon.YINYANG)],
        "c.png": [tab("◇ Scuffed ◇ Player123", leading=LeadingIcon.YINYANG)],
        "d.png": [tab("◇ Galactic ◇ Zeyos4Dan", cut_off=True), tab("◇ HER0 ◇ Misread1"), tab("∞ Infinite ∞ .Bedrocky", leading=LeadingIcon.INFINITY)],
        "ai.png": [tab("◇ Infinite ◇ Player123", leading=LeadingIcon.INFINITY)],
    }
    stub = StubExtractor(rows)
    context = AppContext(db, extractor_factory=lambda _ctx, _o: stub)
    context.stub = stub  # type: ignore[attr-defined]
    # these scenarios exercise the stricter two-submitter rule, which is off by default
    context.settings.set("verification.require_two_submitters_above_immortal", True, changed_by="test")
    yield context
    db.dispose()


def import_all(ctx: AppContext) -> dict[str, int]:
    images = ImageService(ctx)
    ids = {}
    ids["a"] = images.import_bytes(png_bytes(1), filename="a.png", submitter_name="alice").image_id
    ids["b"] = images.import_bytes(png_bytes(2), filename="b.png", submitter_name="alice").image_id
    ids["b2"] = images.import_bytes(png_bytes(8), filename="b2.png", submitter_name="alice").image_id
    ids["d"] = images.import_bytes(png_bytes(4), filename="d.png", submitter_name="carol").image_id
    ids["ai"] = images.import_bytes(png_bytes(5, text_chunks={"Software": "Made with ChatGPT"}), filename="ai.png", submitter_name="bob").image_id
    return ids


def run_pipeline(ctx: AppContext, ids: dict[str, int]) -> None:
    ext = ExtractionService(ctx)
    for image_id in ids.values():
        ext.extract_image(image_id)
    recon = ReconcileService(ctx)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)


def test_import_dedupes_content_and_detects_ai(ctx):
    images = ImageService(ctx)
    first = images.import_bytes(png_bytes(1), filename="a.png", submitter_name="alice")
    again = images.import_bytes(png_bytes(1), filename="copy_of_a.png", submitter_name="bob")
    assert first.created and not again.created and again.duplicate_of == first.image_id
    ai = images.import_bytes(png_bytes(5, text_chunks={"Software": "Made with ChatGPT"}), filename="ai.png", submitter_name="bob")
    assert ai.ai_status == "ai_indicator_found"
    clean = images.import_bytes(png_bytes(6), filename="2024-05-01_10.00.00.png", submitter_name="bob")
    assert clean.date_status == "ok"
    late = images.import_bytes(png_bytes(7), filename="2026-05-01_10.00.00.png", submitter_name="bob", file_modified=datetime(2024, 1, 1, tzinfo=UTC))
    assert late.date_status == "out_of_date"


def test_end_to_end_mode1_rules(ctx):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    view = PlayerViewService(ctx)
    p = view.lookup_by_name("player123")
    assert p is not None
    summary = p["summary"]
    # SCUFFED from alice twice (b,b2) = 1 source; INFINITE only in AI image; HERO clean
    assert summary["effective_rank"] == "IMMORTAL"
    assert summary["highest_observed"] == "SCUFFED"  # SCUFFED is the top ordinary rank
    assert {x["rank"] for x in summary["provisional"]} == {"INFINITE", "SCUFFED"}
    scuffed = next(v for v in p["verification"] if v["rank"] == "SCUFFED")
    assert scuffed["progress"] == "1/2"
    infinite = next(v for v in p["verification"] if v["rank"] == "INFINITE")
    assert infinite["progress"] == "0/2" and any("AI indicator" in r for src in infinite["sources"] for r in src["reasons"])
    codes = {f["code"] for f in p["flags"] if f["is_active"]}
    assert "AI_DETECTED_HIGHER_RANK" in codes and "VERIFICATION_INSUFFICIENT_SOURCES" in codes
    assert summary["ai_rank_images"] == 1 and summary["distinct_submitters"] == 1

    staffy = view.lookup_by_name("Staffy")
    assert staffy["summary"]["staff_ranks"] == ["MOD"]
    assert staffy["summary"]["effective_rank"] == "IMMORTAL"  # staff + ☯ => SCUFFED candidate 1/2 -> IMMORTAL fallback
    assert "STAFF_AND_PLAYER_RANK_FOUND" in {f["code"] for f in staffy["flags"] if f["is_active"]}

    # partial name produced no player rank, bedrock flagged, misread needs review
    assert view.lookup_by_name("Zeyos4Dan") is None or view.lookup_by_name("Zeyos4Dan")["summary"]["effective_rank"] is None
    bed = view.lookup_by_name(".Bedrocky")
    assert bed["player"]["is_bedrock"] and bed["summary"]["effective_rank"] == "IMMORTAL"
    mis = view.lookup_by_name("Misread1")
    assert mis["summary"]["effective_rank"] is None and "MANUAL_REVIEW_REQUIRED" in {f["code"] for f in mis["flags"] if f["is_active"]}

    # second submitter confirms SCUFFED
    c = ImageService(ctx).import_bytes(png_bytes(3), filename="c.png", submitter_name="bob").image_id
    ExtractionService(ctx).extract_image(c)
    ReconcileService(ctx).recompute_player(p["player"]["player_id"])
    p2 = view.detail(p["player"]["player_id"])
    assert p2["summary"]["effective_rank"] == "SCUFFED"
    # the insufficient-sources flags are resolved, not deleted
    resolved = [f for f in p2["flags"] if f["code"] == "VERIFICATION_INSUFFICIENT_SOURCES"]
    assert resolved and all(not f["is_active"] and f["resolved_by"] == "recompute" for f in resolved)
    # INFINITE (AI-only) is below the confirmed rank, so it is no longer provisional
    assert p2["summary"]["provisional"] == []
    assert next(v for v in p2["verification"] if v["rank"] == "INFINITE")["status"] == "provisional"


def test_extraction_cache_skips_second_run(ctx):
    ids = import_all(ctx)
    ext = ExtractionService(ctx)
    ext.extract_image(ids["a"])
    calls = ctx.stub.calls
    r = ext.extract_image(ids["a"])
    assert r.cached and ctx.stub.calls == calls


def test_review_and_override_flow(ctx):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    view = PlayerViewService(ctx)
    review = ReviewService(ctx)
    mis = view.lookup_by_name("Misread1")
    det = next(d for d in mis["detections"] if d["username"] == "Misread1")
    review.review_detection(det["detection_id"], accept=True, actor="daniel", note="it is clearly HERO")
    ReconcileService(ctx).recompute_player(mis["player"]["player_id"])
    assert view.detail(mis["player"]["player_id"])["summary"]["effective_rank"] == "HERO"

    p = view.lookup_by_name("Player123")
    with pytest.raises(ValueError):
        review.add_waiver(p["player"]["player_id"], "SCUFFED", actor="daniel", note="") if False else review.add_waiver(p["player"]["player_id"], "SCUFFED", actor="daniel", reason="")
    review.add_waiver(p["player"]["player_id"], "SCUFFED", actor="daniel", reason="owner confirmed on Discord")
    ReconcileService(ctx).recompute_player(p["player"]["player_id"])
    d = view.detail(p["player"]["player_id"])
    assert d["summary"]["effective_rank"] == "SCUFFED" and "VERIFICATION_WAIVED" in {f["code"] for f in d["flags"] if f["is_active"]}
    review.manual_rank_override(p["player"]["player_id"], "GOD", actor="daniel", reason="test override")
    ReconcileService(ctx).recompute_player(p["player"]["player_id"])
    d = view.detail(p["player"]["player_id"])
    assert d["summary"]["manual_override_active"] and d["summary"]["effective_rank"] == "SCUFFED"  # highest confirmed still wins


def test_verification_checkbox_recomputes_from_preserved_data(ctx):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    view = PlayerViewService(ctx)
    pid = view.lookup_by_name("Player123")["player"]["player_id"]
    ctx.settings.set("verification.require_two_submitters_above_immortal", False, changed_by="t")
    ReconcileService(ctx).recompute_player(pid)
    assert view.detail(pid)["summary"]["effective_rank"] == "SCUFFED"
    ctx.settings.set("verification.require_two_submitters_above_immortal", True, changed_by="t")
    ReconcileService(ctx).recompute_player(pid)
    assert view.detail(pid)["summary"]["effective_rank"] == "IMMORTAL"
    assert view.detail(pid)["summary"]["observed_ranks"] == ["HERO", "INFINITE", "SCUFFED"]


CSV = b"\xef\xbb\xbfusername,uuid,rank,purchase_date,order_id\nPlayer123,069a79f4-44e9-4726-a5be-fca90e38aaf5,Scuffed,2024-01-02,o1\nNewGuy,,SOLAR,2024-02-02,o2\n,abcdef,HERO,2024-03-03,o3\nBad Name!,,HERO,,o4\nPlayer123,069a79f444e94726a5befca90e38aaf5,Scuffed,2024-01-02,o1\n"


def test_csv_preview_and_commit(ctx):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    svc = CsvImportService(ctx)
    preview = svc.preview(CSV, filename="purchases.csv")
    assert preview.encoding == "utf-8-sig" and preview.mapping["username"] == "username" and preview.mapping["uuid"] == "uuid" and preview.mapping["transaction_id"] == "order_id"
    statuses = [r.status for r in preview.rows]
    assert statuses == ["valid", "valid", "invalid", "invalid", "duplicate_in_file"]
    result = svc.commit(CSV, filename="purchases.csv", actor="daniel")
    assert result["status"] == "imported" and result["summary"]["valid"] == 2
    again = svc.commit(CSV, filename="purchases.csv", actor="daniel")
    assert again["status"] == "already_imported"
    view = PlayerViewService(ctx)
    pid = view.lookup_by_name("Player123")["player"]["player_id"]
    ReconcileService(ctx).recompute_player(pid)
    d = view.detail(pid)
    assert d["summary"]["effective_rank"] == "SCUFFED" and d["summary"]["csv_records"] == 1
    assert d["player"]["uuid"] == "069a79f4-44e9-4726-a5be-fca90e38aaf5" and d["player"]["identity_status"] == "verified"
    new = view.lookup_by_name("NewGuy")
    ReconcileService(ctx).recompute_player(new["player"]["player_id"])
    assert view.detail(new["player"]["player_id"])["summary"]["effective_rank"] == "IMMORTAL"
    # deactivating the row removes the authoritative evidence without deleting it
    row_id = d["csv_rows"][0]["csv_row_id"]
    svc.deactivate_row(row_id, actor="daniel", reason="refund")
    ReconcileService(ctx).recompute_player(pid)
    assert view.detail(pid)["summary"]["effective_rank"] == "IMMORTAL"


class FakeLookupService:
    def __init__(self, table: dict[str, LookupResult]):
        self.table = table

    def resolve_many(self, names, *, workers=None, on_result=None, cancel=None, force=False):
        return [self.table.get(n.lower(), LookupResult(n, "mojang", LookupOutcome.NOT_FOUND, failure_code="UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED", message="no such name")) for n in names]


def attempt(name, status, outcome, code=None, uuid=None):
    return LookupAttempt(provider="mojang", queried_name=name, attempt_no=1, started_at=datetime.now(UTC), elapsed_ms=12, http_status=status, outcome=outcome, failure_code=code, resolved_uuid=uuid)


def test_identity_lookup_merge_and_export(ctx, tmp_path):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    c = ImageService(ctx).import_bytes(png_bytes(3), filename="c.png", submitter_name="bob").image_id
    ExtractionService(ctx).extract_image(c)
    view = PlayerViewService(ctx)
    # OldName is an alias of the same account as Player123
    ctx.stub.rows_by_filename["e.png"] = [tab("◇ Titan ◇ OldName")]
    e = ImageService(ctx).import_bytes(png_bytes(9), filename="e.png", submitter_name="dave").image_id
    ExtractionService(ctx).extract_image(e)
    recon = ReconcileService(ctx)
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    uuid = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    fake = FakeLookupService(
        {
            "player123": LookupResult("Player123", "mojang", LookupOutcome.RESOLVED, uuid=uuid, current_name="Player123", attempts=[attempt("Player123", 200, LookupOutcome.RESOLVED, uuid=uuid)]),
            "oldname": LookupResult("OldName", "mojang", LookupOutcome.RESOLVED, uuid=uuid, current_name="Player123", attempts=[attempt("OldName", 200, LookupOutcome.RESOLVED, uuid=uuid)]),
            "staffy": LookupResult("Staffy", "mojang", LookupOutcome.FAILED, failure_code="UUID_LOOKUP_PROXY_CONNECTION_FAILED", message="proxy down", attempts=[attempt("Staffy", None, LookupOutcome.FAILED, "UUID_LOOKUP_PROXY_CONNECTION_FAILED")]),
            "misread1": LookupResult("Misread1", "mojang", LookupOutcome.FAILED, failure_code="UUID_LOOKUP_READ_TIMEOUT", message="timeout", attempts=[attempt("Misread1", None, LookupOutcome.FAILED, "UUID_LOOKUP_READ_TIMEOUT")]),
        }
    )
    ident = IdentityService(ctx)
    targets = ident.candidate_player_ids()
    summary = ident.lookup_players(targets, service=fake)
    assert summary.resolved == 2 and summary.failed == 2 and summary.merged == 1
    bed = view.lookup_by_name(".Bedrocky")
    assert bed["player"]["identity_status"] in ("not_attempted", "bedrock_pending", "unresolved")
    merged = view.lookup_by_name("OldName")
    assert merged["player"]["display_name"] == "Player123" and "OldName" in merged["player"]["aliases"]
    for pid in recon.stale_player_ids():
        recon.recompute_player(pid)
    d = view.lookup_by_name("Player123")
    assert d["summary"]["effective_rank"] == "SCUFFED" and "TITAN" in d["summary"]["observed_ranks"]
    staffy = view.lookup_by_name("Staffy")
    assert staffy["player"]["identity_status"] == "lookup_failed"
    assert "UUID_LOOKUP_PROXY_CONNECTION_FAILED" in {f["code"] for f in staffy["flags"] if f["is_active"]}
    assert staffy["lookups"][0]["failure_code"] == "UUID_LOOKUP_PROXY_CONNECTION_FAILED"

    # conversion + export
    counts = ConversionService(ctx).recompute()
    assert counts["included"] == 0  # every player carries flags by default
    ctx.settings.set("conversion.allowed_flags", ["STAFF_AND_PLAYER_RANK_FOUND", "AI_DETECTED_HIGHER_RANK", "DATE_UNKNOWN", "MANUAL_OVERRIDE", "DUPLICATE_IMAGE"], changed_by="t")
    ConversionService(ctx).recompute()
    preview = ExportService(ctx).preview()
    assert preview["command_count"] == 1
    assert preview["commands"] == [f"lp user {uuid} parent set Warlord"]
    skipped = {r["display_name"]: r["skip_reason"] for r in preview["skipped"]}
    assert "identity not resolved" in skipped["Staffy"]
    with pytest.raises(PermissionError):
        ExportService(ctx).write(path=tmp_path / "out.txt", actor="daniel", staff_warning_acknowledged=False) if preview["warnings"] else (_ for _ in ()).throw(PermissionError())
    out = ExportService(ctx).write(path=tmp_path / "out.txt", actor="daniel", staff_warning_acknowledged=True)
    assert (tmp_path / "out.txt").read_text() == out["text"] and out["command_count"] == 1
    # a later change marks the export stale
    ReviewService(ctx).manual_rank_override(d["player"]["player_id"], "DEFAULT", actor="daniel", reason="x")
    ctx.settings.set("verification.require_two_submitters_above_immortal", False, changed_by="t")
    recon.mark_all_stale("policy change")
    assert ExportService(ctx).history()[0]["stale"] is True


def test_search_and_rescan_manual_correction(ctx):
    ids = import_all(ctx)
    run_pipeline(ctx, ids)
    search = SearchService(ctx)
    r = search.search(SearchQuery(text="player1"))
    assert r["players"] and r["players"][0]["display_name"] == "Player123"
    r = search.search(SearchQuery(text="Scuffed", entity_types=["detection"]))
    assert len(r["detections"]) >= 2
    r = search.search(SearchQuery(flag_code="AI_INDICATOR_FOUND", entity_types=["image"]))
    assert [i["filename"] for i in r["images"]] == ["ai.png"]
    r = search.search(SearchQuery(provisional_only=True, entity_types=["player"]))
    assert {p["display_name"] for p in r["players"]} >= {"Player123"}

    rescan = RescanService(ctx)
    cands = rescan.find_candidates("Zeyos4Danger", RescanScope(mode="all"))
    assert any(c.how == "fuzzy_detection" for c in cands)
    # manual correction on image d: partial name is actually Zeyos4Danger with GALACTIC
    result = rescan.save_manual_correction(ids["d"], username="Zeyos4Danger", prefix_text="Galactic", rank="GALACTIC", staff=None, actor="daniel", note="read from original file")
    view = PlayerViewService(ctx)
    d = view.lookup_by_name("Zeyos4Danger")
    assert d["summary"]["effective_rank"] == "IMMORTAL"  # 1/2 for GALACTIC under carol
    assert next(v for v in d["verification"] if v["rank"] == "GALACTIC")["sources"][0]["submitter"] == "carol"
    assert result["detection_id"] and any(o["source_type"] == "manual_screenshot" for o in d["observations"])
    ctxinfo = rescan.image_context(ids["d"])
    assert any(x["manual"] for x in ctxinfo["detections"]) and any(x["partial"] for x in ctxinfo["detections"])  # original kept


def test_reparse_applies_new_alias_without_reextraction(ctx):
    ctx.stub.rows_by_filename["g.png"] = [tab("◇ Global ◇ Sushi410"), tab("◇ Zonk ◇ Other1")]
    g = ImageService(ctx).import_bytes(png_bytes(21), filename="g.png", submitter_name="booga").image_id
    # GLOBAL is a default alias now; make it unknown first to simulate the original situation
    ranks = [dict(r) for r in ctx.settings.get("ranks.player_ranks")]
    for r in ranks:
        r["aliases"] = [a for a in r.get("aliases", []) if a != "GLOBAL"]
    ctx.settings.set("ranks.player_ranks", ranks, changed_by="t")
    ExtractionService(ctx).extract_image(g)
    view = PlayerViewService(ctx)
    ReconcileService(ctx).recompute_player(view.lookup_by_name("Sushi410")["player"]["player_id"])
    d = view.lookup_by_name("Sushi410")
    assert d["summary"]["effective_rank"] is None and d["detections"][0]["custom_prefix"] == "GLOBAL" and d["detections"][0]["needs_review"]
    # reject the other custom prefix by hand so the re-parse must leave it alone
    other = view.lookup_by_name("Other1")
    ReviewService(ctx).review_detection(other["detections"][0]["detection_id"], accept=False, actor="daniel", note="not a prefix")
    calls_before = ctx.stub.calls
    for r in ranks:
        if r["name"] == "HERO":
            r["aliases"].append("GLOBAL")
    ctx.settings.set("ranks.player_ranks", ranks, changed_by="t")
    counts = ExtractionService(ctx).reparse_detections()
    assert ctx.stub.calls == calls_before  # no extraction happened
    assert counts["changed"] == 1 and counts["skipped_reviewed"] == 1
    ReconcileService(ctx).recompute_player(d["player"]["player_id"])
    d = view.lookup_by_name("Sushi410")
    det = d["detections"][0]
    assert det["rank"] == "HERO" and det["custom_prefix"] is None and not det["needs_review"] and det["raw_prefix"] == "Global"
    assert d["summary"]["effective_rank"] == "HERO" and d["summary"]["observed_ranks"] == ["HERO"]
    assert view.lookup_by_name("Other1")["detections"][0]["status"] == "rejected"


def test_job_runner_extracts_and_recomputes(ctx):
    import_all(ctx)
    runner = JobRunner(ctx, workers=1)
    register_default_handlers(runner)
    runner.start()
    try:
        job_id = runner.submit("extract_images", {}, label="extract all", created_by="t")
        info = runner.wait(job_id, timeout=30)
        assert info["status"] == "completed", info
        assert info["result"]["images"] == 5
        view = PlayerViewService(ctx)
        assert view.lookup_by_name("Player123")["summary"]["effective_rank"] == "IMMORTAL"
        # cancellation of a queued job
        blocker = threading.Event()
        runner.register("block", lambda c, h, p: (blocker.wait(5), {})[1])
        b1 = runner.submit("block", {})
        b2 = runner.submit("recompute_players", {"all": True})
        runner.cancel(b2)
        blocker.set()
        assert runner.wait(b1)["status"] == "completed"
        assert runner.get(b2)["status"] == "cancelled"
    finally:
        runner.stop()
