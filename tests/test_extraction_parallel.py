"""Parallel extraction: crops of one image, several images per job, the shared call cap and 429 pauses."""

from __future__ import annotations

import threading
import time

import pytest

from rankrecovery.core.enums import ExtractorType, RegionKind
from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.extraction.base import DetectedRegion, ExtractionOutput, ExtractionSettings
from rankrecovery.extraction.vision import RATE_LIMIT_MARKER, AnthropicVisionExtractor
from rankrecovery.jobs import handlers
from rankrecovery.jobs.handlers import register_default_handlers
from rankrecovery.jobs.runner import JobRunner
from rankrecovery.services.context import AppContext
from rankrecovery.services.images import ImageService
from rankrecovery.services.playerview import PlayerViewService
from tests.helpers import StubExtractor, png_bytes, tab
from tests.test_extraction_vision import ROWS, FakeClient, png, settings


class ConcurrencyMeter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __enter__(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.active -= 1


class SlowFakeClient(FakeClient):
    """Every model call takes ``delay`` seconds and records how many ran at once."""

    def __init__(self, responses, *, delay: float = 0.05, meter: ConcurrencyMeter | None = None, fail_call: int | None = None):
        super().__init__(responses)
        self.meter = meter or ConcurrencyMeter()
        self.delay = delay
        self.fail_call = fail_call
        self.count = 0
        self.count_lock = threading.Lock()
        inner = self.messages.create

        def create(**kwargs):
            with self.count_lock:
                self.count += 1
                n = self.count
            with self.meter:
                time.sleep(self.delay)
                if self.fail_call == n:
                    raise RuntimeError(f"boom on call {n}")
                return inner(**kwargs)

        self.messages.create = create  # type: ignore[method-assign]


def three_regions() -> list[DetectedRegion]:
    return [DetectedRegion(kind=RegionKind.TAB, bbox=(10, 10, 150, 100), columns=[], confidence=0.9), DetectedRegion(kind=RegionKind.CHAT, bbox=(10, 120, 150, 60), columns=[], confidence=0.9), DetectedRegion(kind=RegionKind.NAMETAG, bbox=(200, 20, 150, 40), columns=[], confidence=0.9)]


def test_crops_of_one_image_are_read_in_parallel_and_kept_in_order():
    client = SlowFakeClient([ROWS, ROWS, ROWS])
    ext = AnthropicVisionExtractor(settings(), "key", client=client, concurrency=3)
    out = ext.extract(png(), regions=three_regions())
    assert out.ok and out.cost["api_calls"] == 3 and out.cost["input_tokens"] == 300
    assert client.meter.max_active >= 2
    assert len(out.rows) == 9  # three crops, three rows each; rows deduplicated by position/text only within identical boxes
    kinds = [part.split('"call": "')[1].split('"')[0] for part in out.raw_output.split("\n")]
    assert kinds == ["rows:tab", "rows:chat", "rows:nametag"]  # crop order, whatever finished first


def test_shared_call_cap_limits_calls_in_flight():
    client = SlowFakeClient([ROWS, ROWS, ROWS])
    ext = AnthropicVisionExtractor(settings(), "key", client=client, concurrency=3, max_concurrent_calls=1)
    out = ext.extract(png(), regions=three_regions())
    assert out.ok and client.meter.max_active == 1


def test_sequential_when_concurrency_is_one():
    client = SlowFakeClient([ROWS, ROWS, ROWS])
    ext = AnthropicVisionExtractor(settings(), "key", client=client, concurrency=1)
    out = ext.extract(png(), regions=three_regions())
    assert out.ok and client.meter.max_active == 1 and out.cost["api_calls"] == 3


def test_failure_in_one_parallel_crop_is_reported_not_raised():
    client = SlowFakeClient([ROWS, ROWS, ROWS], fail_call=2)
    ext = AnthropicVisionExtractor(settings(), "key", client=client, concurrency=3)
    out = ext.extract(png(), regions=three_regions())
    assert not out.ok and "boom" in out.error


class SlowStub(StubExtractor):
    """StubExtractor with a delay and a concurrency meter; optionally rate-limits the first call for an image."""

    def __init__(self, rows, *, delay: float = 0.1, rate_limit_first: set[str] | None = None):
        super().__init__(rows)
        self.meter = ConcurrencyMeter()
        self.delay = delay
        self.rate_limit_first = set(rate_limit_first or ())
        self.seen: dict[str, int] = {}
        self.lock = threading.Lock()

    def extract(self, image_bytes: bytes, *, filename: str = "", regions=None) -> ExtractionOutput:
        with self.lock:
            self.seen[filename] = self.seen.get(filename, 0) + 1
            first = self.seen[filename] == 1
        with self.meter:
            time.sleep(self.delay)
        if first and filename in self.rate_limit_first:
            return ExtractionOutput(rows=[], regions=[], extractor=ExtractorType.OCR, engine=self.settings.engine, model="stub-1", version="1", raw_output="", full_text="", settings_fingerprint=self.settings.fingerprint(), error=f"{RATE_LIMIT_MARKER} after retries")
        return super().extract(image_bytes, filename=filename, regions=regions)


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "parallel.db"))
    db.run_migrations()
    rows = {f"img{i}.png": [tab(f"◇ Hero ◇ Player{i}")] for i in range(6)}
    stub = SlowStub(rows, rate_limit_first={"img2.png"})
    context = AppContext(db, extractor_factory=lambda _c, _o: stub)
    context.stub = stub  # type: ignore[attr-defined]
    images = ImageService(context)
    for i in range(6):
        images.import_bytes(png_bytes(i + 1), filename=f"img{i}.png", submitter_name="staff")
    yield context
    db.dispose()


def test_extraction_job_runs_images_in_parallel_and_pauses_on_rate_limit(ctx, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(handlers, "SLEEP", lambda s: sleeps.append(s))
    ctx.settings.set("extraction.ocr_workers", 3, changed_by="test")  # the stub is not the vision engine
    runner = JobRunner(ctx, workers=1)
    register_default_handlers(runner)
    runner.start()
    try:
        job_id = runner.submit("extract_images", {}, label="extract all", created_by="t")
        info = runner.wait(job_id, timeout=30)
        assert info["status"] == "completed", info
        assert info["result"]["images"] == 6 and info["result"]["failed"] == 0 and info["result"]["workers"] == 3
        assert ctx.stub.meter.max_active >= 2  # several images at once
        assert sleeps == [15.0] and ctx.stub.seen["img2.png"] == 2  # rate-limited once, paused, retried, succeeded
        view = PlayerViewService(ctx)
        assert all(view.lookup_by_name(f"Player{i}")["summary"]["effective_rank"] == "HERO" for i in range(6))
        assert "3 at a time" in (info.get("progress_message") or "") or info["done"] == 6
    finally:
        runner.stop()


def test_extraction_job_honours_explicit_worker_count(ctx):
    runner = JobRunner(ctx, workers=1)
    register_default_handlers(runner)
    runner.start()
    try:
        job_id = runner.submit("extract_images", {"workers": 1}, label="extract all", created_by="t")
        info = runner.wait(job_id, timeout=30)
        assert info["status"] == "completed" and info["result"]["workers"] == 1 and ctx.stub.meter.max_active == 1
    finally:
        runner.stop()


def test_concurrency_settings_do_not_change_the_fingerprint():
    base = ExtractionSettings(extractor=ExtractorType.VISION, engine="anthropic", model="m")
    a = AnthropicVisionExtractor(base, "key", client=FakeClient([]), concurrency=1)
    b = AnthropicVisionExtractor(base, "key", client=FakeClient([]), concurrency=8, max_concurrent_calls=2)
    assert a.settings.fingerprint() == b.settings.fingerprint()  # already-read images never look stale after tuning
