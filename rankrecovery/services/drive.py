"""Pull the photos behind a shared Google Drive link into the newuploads folder.

Two routes, both for links shared with "Anyone with the link":

* With a Google API key (``drive.api_key``): the Drive API v3 lists the folder,
  including sub-folders, page by page, so folders of any size work.
* Without one: ``gdown`` reads the public folder page the way a browser does.
  Google shows at most 50 files per folder that way, so larger folders are
  reported as possibly incomplete.

Only image files are kept; documents, videos and archives are skipped and listed.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from rankrecovery.imaging.decode import SUPPORTED_EXTENSIONS
from rankrecovery.services.ingest import unique_path

log = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff", "image/avif"}
GDOWN_PAGE_LIMIT = 50
_ID = r"([A-Za-z0-9_-]{10,})"
_FOLDER_PATTERNS = [re.compile(p) for p in (rf"drive\.google\.com/drive/(?:u/\d+/)?(?:mobile/)?folders/{_ID}", rf"drive\.google\.com/embeddedfolderview\?id={_ID}", rf"drive\.google\.com/drive/folders/{_ID}")]
_FILE_PATTERNS = [re.compile(p) for p in (rf"drive\.google\.com/file/d/{_ID}", rf"drive\.google\.com/(?:open|uc)\?(?:.*&)?id={_ID}", rf"docs\.google\.com/(?:uc|open)\?(?:.*&)?id={_ID}", rf"drive\.usercontent\.google\.com/download\?(?:.*&)?id={_ID}")]
ProgressFn = Callable[[int, int | None, str], None]


def parse_drive_link(url: str) -> tuple[str, str]:
    """Return ``("folder" | "file", id)`` for a Google Drive link, or raise ValueError."""
    text = (url or "").strip()
    for pattern in _FOLDER_PATTERNS:
        m = pattern.search(text)
        if m:
            return "folder", m.group(1)
    for pattern in _FILE_PATTERNS:
        m = pattern.search(text)
        if m:
            return "file", m.group(1)
    raise ValueError("that is not a Google Drive folder or file link (expected drive.google.com/drive/folders/... or drive.google.com/file/d/...)")


def safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip()).strip(". ")
    return cleaned[:150] or fallback


@dataclass
class DriveResult:
    kind: str
    drive_id: str
    folder: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    method: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriveDownloader:
    def __init__(self, dest: Path, *, api_key: str | None = None, client: httpx.Client | None = None, gdown_module: Any | None = None, progress: ProgressFn | None = None, timeout: float = 60.0):
        self.dest = Path(dest)
        self.api_key = (api_key or "").strip() or None
        self._client = client
        self._gdown = gdown_module
        self.progress = progress
        self.timeout = timeout

    # ------------------------------------------------------------------ entry
    def download(self, url: str) -> DriveResult:
        kind, drive_id = parse_drive_link(url)
        self.dest.mkdir(parents=True, exist_ok=True)
        result = DriveResult(kind=kind, drive_id=drive_id, folder=str(self.dest))
        if self.api_key:
            result.method = "drive-api"
            self._via_api(kind, drive_id, result)
        else:
            result.method = "gdown"
            self._via_gdown(kind, drive_id, url, result)
        return result

    # --------------------------------------------------------------- Drive API
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "rankrecovery/0.1 (+https://github.com/danielburstein/rank_program)"})
        return self._client

    def _api_get(self, path: str, **params: Any) -> httpx.Response:
        params["key"] = self.api_key
        response = self._http().get(f"{DRIVE_API}/{path}", params=params)
        if response.status_code in (401, 403):
            raise ValueError(f"Google Drive refused the request (HTTP {response.status_code}): the link must be shared with 'Anyone with the link', and drive.api_key must be a valid Google API key with the Drive API enabled")
        if response.status_code == 404:
            raise ValueError("Google Drive says this folder or file does not exist (HTTP 404); check the link and that it is shared with 'Anyone with the link'")
        if response.status_code >= 400:
            raise ValueError(f"Google Drive answered HTTP {response.status_code}: {response.text[:200]}")
        return response

    def _list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"q": f"'{folder_id}' in parents and trashed = false", "fields": "nextPageToken,files(id,name,mimeType,size)", "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self._api_get("files", **params).json()
            files.extend(f for f in data.get("files", []) if isinstance(f, dict))
            token = data.get("nextPageToken")
            if not token:
                return files

    def _via_api(self, kind: str, drive_id: str, result: DriveResult) -> None:
        if kind == "file":
            meta = self._api_get(f"files/{drive_id}", fields="id,name,mimeType,size", supportsAllDrives="true").json()
            entries = [meta]
        else:
            entries = []
            queue = [drive_id]
            seen: set[str] = set()
            while queue:
                fid = queue.pop(0)
                if fid in seen:
                    continue
                seen.add(fid)
                for entry in self._list_folder(fid):
                    if entry.get("mimeType") == FOLDER_MIME:
                        queue.append(str(entry["id"]))
                    else:
                        entries.append(entry)
        images = [e for e in entries if self._is_image(e)]
        for entry in entries:
            if entry not in images:
                result.skipped.append(f"{entry.get('name')}: {entry.get('mimeType', 'unknown type')}")
        total = len(images)
        for n, entry in enumerate(images, start=1):
            target = unique_path(self.dest / safe_filename(str(entry.get("name") or entry["id"])))
            response = self._api_get(f"files/{entry['id']}", alt="media", supportsAllDrives="true")
            target.write_bytes(response.content)
            result.downloaded.append(target.name)
            if self.progress:
                self.progress(n, total, target.name)

    @staticmethod
    def _is_image(entry: dict[str, Any]) -> bool:
        mime = str(entry.get("mimeType") or "")
        name = str(entry.get("name") or "")
        return mime in IMAGE_MIMES or Path(name).suffix.lower() in SUPPORTED_EXTENSIONS

    # ------------------------------------------------------------------- gdown
    def _gdown_module(self) -> Any:
        if self._gdown is None:
            try:
                import gdown
            except ImportError as exc:  # pragma: no cover - dependency listed in requirements
                raise ValueError("the gdown package is not installed (pip install gdown), and no drive.api_key is configured") from exc
            self._gdown = gdown
        return self._gdown

    def _via_gdown(self, kind: str, drive_id: str, url: str, result: DriveResult) -> None:
        gdown = self._gdown_module()
        staging = self.dest / f".drive-{drive_id[:12]}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            if kind == "folder":
                paths = gdown.download_folder(id=drive_id, output=str(staging), quiet=True, use_cookies=False)
                if not paths:
                    raise ValueError("Google Drive returned nothing for that folder: it must be shared with 'Anyone with the link' (or set drive.api_key)")
                if len(paths) >= GDOWN_PAGE_LIMIT:
                    result.warnings.append(f"Google Drive lists at most {GDOWN_PAGE_LIMIT} files per folder without an API key; this folder may hold more. Set drive.api_key to fetch everything.")
            else:
                path = gdown.download(id=drive_id, output=str(staging) + "/", quiet=True, use_cookies=False, fuzzy=True)
                if not path:
                    raise ValueError("Google Drive returned nothing for that file: it must be shared with 'Anyone with the link'")
                paths = [path]
            files = [Path(p) for p in paths if isinstance(p, str) and Path(p).is_file()]
            files += [p for p in staging.rglob("*") if p.is_file() and p not in files]
            total = len(files)
            for n, path in enumerate(sorted(set(files)), start=1):
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    result.skipped.append(f"{path.name}: not an image file")
                    continue
                target = unique_path(self.dest / safe_filename(path.name))
                shutil.move(str(path), target)
                result.downloaded.append(target.name)
                if self.progress:
                    self.progress(n, total, target.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
