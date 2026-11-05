"""In-memory ring buffer of recent log records for the GUI error log."""

from __future__ import annotations

import collections
import logging
import threading
from datetime import UTC, datetime

_SECRET_MARKERS = ("sk-ant-", "rr_", "api_key", "apikey", "password", "x-api-key")


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.records: collections.deque[dict] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover
            message = str(record.msg)
        lowered = message.lower()
        if any(m in lowered for m in _SECRET_MARKERS):
            message = "[redacted: message contained a secret marker]"
        entry = {"time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(), "level": record.levelname, "logger": record.name, "message": message}
        if record.exc_info and record.exc_text:
            entry["exception"] = record.exc_text[-2000:]
        with self._lock:
            self.records.append(entry)

    def tail(self, limit: int = 200, level: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self.records)
        if level:
            threshold = logging.getLevelName(level.upper())
            if isinstance(threshold, int):
                items = [i for i in items if logging.getLevelName(i["level"]) >= threshold]
        return items[-limit:]


def install(capacity: int = 2000) -> RingBufferHandler:
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingBufferHandler):
            return h
    handler = RingBufferHandler(capacity)
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler
