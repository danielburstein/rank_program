"""Folder ingest (newuploads) and Google Drive downloads."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.db.models import ImportBatch, SourceImage, Submitter
from rankrecovery.services.context import AppContext
from rankrecovery.services.drive import DriveDownloader, parse_drive_link, safe_filename
from rankrecovery.services.ingest import FolderIngestService, unique_path
from tests.helpers import StubExtractor, png_bytes, tab


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def submit(self, kind: str, params: dict | None = None, **kwargs) -> int:
        self.submitted.append((kind, dict(params or {})))
        return len(self.submitted)


@pytest.fixture
def ctx(tmp_path):
    db = Database(sqlite_url(tmp_path / "ingest.db"))
    db.run_migrations()
    stub = StubExtractor({"a.png": [tab("◇ Hero ◇ Alpha")], "b.png": [tab("◇ Legend ◇ Beta")]})
    context = AppContext(db, extractor_factory=lambda _c, _o: stub)
    context.settings.set("ingest.folder", str(tmp_path / "newuploads"), changed_by="test")
    context.job_runner = FakeRunner()  # type: ignore[assignment]
    yield context
    db.dispose()


def test_folder_ingest_submits_removes_and_sorts_out_bad_files(ctx, tmp_path):
    svc = FolderIngestService(ctx)
    folder = svc.folder_path()
    assert folder == tmp_path / "newuploads" and folder.is_dir()
    (folder / "a.png").write_bytes(png_bytes(1))
    (folder / "Season 3").mkdir()
    (folder / "Season 3" / "b.png").write_bytes(png_bytes(2))
    (folder / "Season 3" / "deeper").mkdir()
    (folder / "Season 3" / "deeper" / "dup.png").write_bytes(png_bytes(1))
    (folder / "Season 3" / "deeper" / "bad.png").write_bytes(b"this is not a png")
    (folder / "notes.txt").write_text("hello")
    (folder / ".DS_Store").write_bytes(b"\x00")
    (folder / ".hidden").mkdir()
    (folder / ".hidden" / "c.png").write_bytes(png_bytes(5))
    (folder / "rejected").mkdir()
    (folder / "rejected" / "old.png").write_bytes(png_bytes(6))  # already rejected once: passed over
    (folder / "Season 3" / "Rejected").mkdir()
    (folder / "Season 3" / "Rejected" / "nested.png").write_bytes(png_bytes(7))  # the keyword is skipped at any depth, any case

    assert [p.relative_to(folder).as_posix() for p in svc.waiting()] == ["a.png", "notes.txt", "Season 3/b.png", "Season 3/deeper/bad.png", "Season 3/deeper/dup.png"]
    ticks: list[tuple[int, int]] = []
    summary = svc.ingest(submitter="alice", actor="test", progress=lambda done, total, msg: ticks.append((done, total)))
    assert (summary.seen, summary.imported, summary.duplicates, summary.rejected, summary.ignored, summary.folders_removed) == (5, 2, 1, 1, 1, 1)
    assert ticks == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
    assert not (folder / "a.png").exists() and not (folder / "Season 3" / "b.png").exists()
    assert not (folder / "Season 3" / "deeper").exists()  # emptied and removed
    assert (folder / "Season 3").exists()  # still holds the skipped Rejected folder
    assert (folder / "rejected" / "Season 3" / "deeper" / "bad.png").exists() and (folder / "rejected" / "old.png").exists()
    assert (folder / "notes.txt").exists() and (folder / ".hidden" / "c.png").exists() and (folder / "Season 3" / "Rejected" / "nested.png").exists()
    assert "Season 3/deeper/dup.png: identical" in summary.text() and "1 emptied sub-folder(s) removed" in summary.text()
    assert summary.job_id == 1 and ctx.job_runner.submitted == [("extract_images", {"image_ids": summary.image_ids})]
    assert "new image(s) submitted" in summary.text() and "identical to image" in summary.text()
    with ctx.db.read_session() as s:
        images = s.execute(select(SourceImage)).scalars().all()
        assert len(images) == 2 and all(img.import_batch_id == summary.batch_id for img in images)
        assert sorted(img.original_path for img in images) == ["newuploads/Season 3/b.png", "newuploads/a.png"]
        batch = s.get(ImportBatch, summary.batch_id)
        assert batch.source == "folder" and s.get(Submitter, batch.submitter_id).name == "alice"

    again = svc.ingest(actor="test")
    assert (again.seen, again.imported, again.job_id) == (1, 0, None)  # only notes.txt is left, nothing to extract
    assert svc.ingest(actor="test", submitter=None).seen == 1


def test_folder_ingest_default_submitter_and_empty_folder(ctx):
    svc = FolderIngestService(ctx)
    assert svc.ingest().seen == 0
    (svc.folder_path() / "a.png").write_bytes(png_bytes(3))
    summary = svc.ingest(extract=False)
    assert summary.imported == 1 and summary.job_id is None
    with ctx.db.read_session() as s:
        batch = s.get(ImportBatch, summary.batch_id)
        assert s.get(Submitter, batch.submitter_id).name == "folder-upload"


def test_unique_path_and_safe_filename(tmp_path):
    (tmp_path / "x.png").write_bytes(b"1")
    assert unique_path(tmp_path / "x.png") == tmp_path / "x (2).png"
    assert unique_path(tmp_path / "y.png") == tmp_path / "y.png"
    assert safe_filename('../evil\\name:*?.png') == "_evil_name___.png"  # separators replaced, leading dots dropped
    assert safe_filename("   ") == "file"


def test_parse_drive_link():
    assert parse_drive_link("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing") == ("folder", "1AbCdEfGhIjKlMnOp")
    assert parse_drive_link("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOp") == ("folder", "1AbCdEfGhIjKlMnOp")
    assert parse_drive_link("https://drive.google.com/file/d/1XyZ_fileid-0001/view?usp=drive_link") == ("file", "1XyZ_fileid-0001")
    assert parse_drive_link("https://drive.google.com/open?id=1XyZ_fileid-0001") == ("file", "1XyZ_fileid-0001")
    with pytest.raises(ValueError):
        parse_drive_link("https://example.com/not-drive")
    with pytest.raises(ValueError):
        parse_drive_link("")


def test_drive_api_downloads_images_recursively(tmp_path):
    media = {"img1": png_bytes(1), "img2": png_bytes(2), "img3": png_bytes(3)}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.params.get("key") == "KEY"
        if request.url.path.endswith("/files"):
            q = request.url.params["q"]
            if "'root1234567890'" in q:
                if request.url.params.get("pageToken") == "p2":
                    return httpx.Response(200, json={"files": [{"id": "sub1", "name": "more", "mimeType": "application/vnd.google-apps.folder"}]})
                return httpx.Response(200, json={"nextPageToken": "p2", "files": [{"id": "img1", "name": "shot one.png", "mimeType": "image/png"}, {"id": "doc", "name": "notes.gdoc", "mimeType": "application/vnd.google-apps.document"}, {"id": "img2", "name": "weird:name?.JPG", "mimeType": "application/octet-stream"}]})
            if "'sub1'" in q:
                return httpx.Response(200, json={"files": [{"id": "img3", "name": "shot one.png", "mimeType": "image/png"}]})
            return httpx.Response(200, json={"files": []})
        file_id = request.url.path.rsplit("/", 1)[-1]
        assert request.url.params.get("alt") == "media"
        return httpx.Response(200, content=media[file_id])

    seen: list[tuple[int, int | None, str]] = []
    dl = DriveDownloader(tmp_path / "drop", api_key="KEY", client=httpx.Client(transport=httpx.MockTransport(handler)), progress=lambda d, t, n: seen.append((d, t, n)))
    result = dl.download("https://drive.google.com/drive/folders/root1234567890?usp=sharing")
    assert result.method == "drive-api" and result.kind == "folder"
    assert sorted(result.downloaded) == ["shot one (2).png", "shot one.png", "weird_name_.JPG"]
    assert result.skipped == ["notes.gdoc: application/vnd.google-apps.document"] and not result.warnings
    assert (tmp_path / "drop" / "shot one.png").read_bytes() == png_bytes(1) and (tmp_path / "drop" / "shot one (2).png").read_bytes() == png_bytes(3)
    assert seen[-1][0] == 3 and seen[-1][1] == 3


def test_drive_api_errors_are_explained(tmp_path):
    dl = DriveDownloader(tmp_path, api_key="KEY", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "forbidden"}))))
    with pytest.raises(ValueError, match="Anyone with the link"):
        dl.download("https://drive.google.com/drive/folders/root1234567890")
    dl = DriveDownloader(tmp_path, api_key="KEY", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(ValueError, match="does not exist"):
        dl.download("https://drive.google.com/file/d/abcdefghijkl/view")


def test_gdown_fallback_moves_images_to_the_folder_and_warns_at_the_page_limit(tmp_path):
    class FakeGdown:
        def __init__(self, count: int) -> None:
            self.count = count
            self.calls: list[dict] = []

        def download_folder(self, **kwargs):
            self.calls.append(kwargs)
            out = Path(kwargs["output"]) / "Shared Folder"
            out.mkdir(parents=True)
            paths = []
            for i in range(self.count):
                p = out / f"pic{i}.png"
                p.write_bytes(png_bytes(i))
                paths.append(str(p))
            (out / "readme.txt").write_text("x")
            paths.append(str(out / "readme.txt"))
            return paths

        def download(self, **kwargs):
            self.calls.append(kwargs)
            p = Path(kwargs["output"]) / "single.png"
            p.write_bytes(png_bytes(9))
            return str(p)

    fake = FakeGdown(3)
    dl = DriveDownloader(tmp_path / "drop", gdown_module=fake)
    result = dl.download("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp")
    assert result.method == "gdown" and fake.calls[0]["id"] == "1AbCdEfGhIjKlMnOp" and fake.calls[0]["use_cookies"] is False
    assert sorted(result.downloaded) == ["pic0.png", "pic1.png", "pic2.png"] and result.skipped == ["readme.txt: not an image file"]
    assert sorted(p.name for p in (tmp_path / "drop").iterdir()) == ["pic0.png", "pic1.png", "pic2.png"]  # staging folder cleaned up
    assert not result.warnings

    result = DriveDownloader(tmp_path / "drop2", gdown_module=FakeGdown(50)).download("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp")
    assert len(result.downloaded) == 50 and any("50 files" in w for w in result.warnings)

    result = DriveDownloader(tmp_path / "drop3", gdown_module=FakeGdown(0)).download("https://drive.google.com/file/d/1XyZ_fileid-0001/view")
    assert result.downloaded == ["single.png"]


def test_gdown_calls_match_the_installed_gdown_signature(tmp_path):
    """gdown's keyword arguments change between releases (fuzzy= vanished in 6.x and broke a live download)."""
    import inspect

    gdown = pytest.importorskip("gdown")
    from rankrecovery.services.drive import gdown_file_kwargs, gdown_folder_kwargs

    inspect.signature(gdown.download_folder).bind(**gdown_folder_kwargs("1AbCdEfGhIjKlMnOp", tmp_path))
    inspect.signature(gdown.download).bind(**gdown_file_kwargs("1AbCdEfGhIjKlMnOp", tmp_path))


def test_drive_job_downloads_then_ingests(ctx, tmp_path, monkeypatch):
    from rankrecovery.jobs import handlers

    class FakeGdown:
        def download_folder(self, **kwargs):
            out = Path(kwargs["output"])
            (out / "a.png").write_bytes(png_bytes(1))
            return [str(out / "a.png")]

    monkeypatch.setattr("rankrecovery.services.drive.DriveDownloader._gdown_module", lambda self: FakeGdown())

    class Job:
        job_id = 1
        messages: list[str] = []
        errors: list[tuple[str, str]] = []

        def progress(self, done, total=None, message="", **kw):
            self.messages.append(message)

        def error(self, item, message, tb=""):
            self.errors.append((item, message))

        def check_cancelled(self):
            return None

    out = handlers.download_drive(ctx, Job(), {"url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", "submitter": "bob", "ingest": True})
    assert out["downloaded"] == ["a.png"] and out["ingest"]["imported"] == 1 and out["ingest"]["job_id"] == 1
    assert ctx.job_runner.submitted[0][0] == "extract_images"
    assert not (tmp_path / "newuploads" / "a.png").exists()
