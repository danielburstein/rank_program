"""Review queue: pending detections one at a time, zoomed to the row in the screenshot."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QVBoxLayout, QWidget

from rankrecovery.client.pages.base import Page
from rankrecovery.client.state import AppState
from rankrecovery.client.widgets.forms import KeyValueForm
from rankrecovery.client.widgets.image_viewer import ImageViewer

DEFAULT_NOTE = "reviewed in desktop client"


class ReviewPage(Page):
    title = "Review queue"

    def __init__(self, state: AppState, parent: QWidget | None = None):
        super().__init__(state, parent)
        self.items: list[dict[str, Any]] = []
        self.index = 0
        self.accepted = 0
        self.rejected = 0
        self._busy = False
        self._image_cache: dict[int, dict[str, Any]] = {}

        controls = QHBoxLayout()
        self.username = QLineEdit()
        self.username.setPlaceholderText("only this player (optional)")
        self.load_btn = QPushButton("Load queue")
        self.load_btn.clicked.connect(self.load_queue)
        self.counter = QLabel("")
        self.counter.setObjectName("dim")
        controls.addWidget(self.username, 1)
        controls.addWidget(self.load_btn)
        controls.addWidget(self.counter, 1)
        self.root.addLayout(controls)

        actions = QHBoxLayout()
        self.note = QLineEdit(DEFAULT_NOTE)
        self.note.setPlaceholderText("note recorded with the decision")
        self.accept_btn = QPushButton("Accept  (Ctrl+Enter)")
        self.reject_btn = QPushButton("Reject  (Ctrl+Backspace)")
        self.skip_btn = QPushButton("Skip  (Ctrl+Right)")
        self.accept_image_btn = QPushButton("Accept all remaining on this screenshot")
        self.accept_btn.setObjectName("accept")
        self.reject_btn.setObjectName("reject")
        self.accept_btn.clicked.connect(lambda: self.decide(True))
        self.reject_btn.clicked.connect(lambda: self.decide(False))
        self.skip_btn.clicked.connect(self.skip)
        self.accept_image_btn.clicked.connect(self.accept_rest_of_image)
        actions.addWidget(QLabel("Note"))
        actions.addWidget(self.note, 2)
        for b in (self.accept_btn, self.reject_btn, self.skip_btn, self.accept_image_btn):
            actions.addWidget(b)
        self.root.addLayout(actions)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=lambda: self.decide(True))
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=lambda: self.decide(True))
        QShortcut(QKeySequence("Ctrl+Backspace"), self, activated=lambda: self.decide(False))
        QShortcut(QKeySequence("Ctrl+Right"), self, activated=self.skip)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.viewer = ImageViewer()
        self.viewer.detection_selected.connect(self._jump_to_detection)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.details = KeyValueForm("Detection")
        self.why = QLabel("")
        self.why.setWordWrap(True)
        self.why.setObjectName("warning")
        self.open_player_btn = QPushButton("Open player")
        self.open_player_btn.clicked.connect(self._open_player)
        side_layout.addWidget(self.details)
        side_layout.addWidget(QLabel("Why it needs review"))
        side_layout.addWidget(self.why)
        side_layout.addWidget(self.open_player_btn)
        side_layout.addStretch(1)
        split.addWidget(self.viewer)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.root.addWidget(split, 1)
        self._set_enabled(False)

    # ------------------------------------------------------------------ queue
    def refresh(self) -> None:
        self.load_queue()

    def load_queue(self) -> None:
        name = self.username.text().strip() or None
        self.counter.setText("loading…")
        self.call(self.client.pending_detections, name, 200, on_result=self._queue_loaded)

    def _queue_loaded(self, items: list[dict[str, Any]]) -> None:
        self.items = list(items)
        self.index = 0
        self.accepted = 0
        self.rejected = 0
        self._image_cache.clear()
        self.show_current()

    def current(self) -> dict[str, Any] | None:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def show_current(self) -> None:
        item = self.current()
        self._update_counter()
        if item is None:
            self._set_enabled(False)
            self.details.set_values({"status": "queue empty" if not self.items else "all reviewed"})
            self.why.setText("")
            return
        self._set_enabled(True)
        mapped = item.get("rank") or ""
        if item.get("staff"):
            mapped = (mapped + " " if mapped else "") + f"staff {item['staff']}"
        if item.get("trailing_icon_rank"):
            mapped += f" + {item['trailing_icon_rank']} (icon after name)"
        self.details.set_values(
            {
                "player": item.get("player_display_name") or item.get("username"),
                "username read": item.get("raw_username"),
                "prefix read": item.get("raw_prefix"),
                "maps to": mapped or "nothing",
                "custom prefix": item.get("custom_prefix") or "",
                "misread of": item.get("misread_of") or "",
                "region": item.get("region"),
                "confidence": f"{float(item.get('confidence') or 0):.2f}",
                "screenshot": f"#{item.get('image_id')} {item.get('image_filename') or ''}",
                "submitter": item.get("submitter"),
                "detection id": item.get("detection_id"),
            },
            order=["player", "username read", "prefix read", "maps to", "custom prefix", "misread of", "region", "confidence", "screenshot", "submitter", "detection id"],
        )
        flags = item.get("flags") or []
        notes = item.get("notes") or []
        self.why.setText("\n".join([*flags, *notes]) or "low confidence or ambiguous prefix")
        self._load_image(item)

    def _update_counter(self) -> None:
        total = len(self.items)
        pos = min(self.index + 1, total)
        self.counter.setText(f"{pos} of {total} pending · accepted {self.accepted} · rejected {self.rejected}" if total else "nothing pending")

    def _set_enabled(self, enabled: bool) -> None:
        for b in (self.accept_btn, self.reject_btn, self.skip_btn, self.accept_image_btn, self.open_player_btn):
            b.setEnabled(enabled)

    # ------------------------------------------------------------------ image
    def _load_image(self, item: dict[str, Any]) -> None:
        image_id = int(item["image_id"])
        cached = self._image_cache.get(image_id)
        if cached is not None:
            self._show_image(item, cached)
            return

        def got_bytes(data: bytes) -> None:
            cache = {"bytes": data, "detections": [i for i in self.items if i.get("image_id") == image_id]}
            self._image_cache[image_id] = cache
            self._show_image(item, cache)  # show at once; the full detection list follows
            self.call(self.client.image_context, image_id, on_result=lambda ctx: self._got_context(image_id, ctx), on_error=lambda _m: None)

        self.call(self.client.image_content, image_id, on_result=got_bytes)

    def _got_context(self, image_id: int, ctx: dict[str, Any]) -> None:
        cache = self._image_cache.get(image_id)
        if cache is None:
            return
        cache["detections"] = ctx.get("detections") or cache["detections"]
        current = self.current()
        if current is not None and current.get("image_id") == image_id:
            self.viewer.set_detections(cache["detections"])  # keeps the current zoom

    def _show_image(self, item: dict[str, Any], cache: dict[str, Any]) -> None:
        if self.current() is not item:
            return
        self.viewer.load_bytes(cache["bytes"], detections=cache["detections"], info=f"#{item.get('image_id')} {item.get('image_filename') or ''} · submitter {item.get('submitter')}")
        self.viewer.highlight_detection(item.get("detection_id"))
        if item.get("bbox"):
            self.viewer.zoom_to(item["bbox"])

    # -------------------------------------------------------------- decisions
    def _note(self) -> str:
        return self.note.text().strip() or DEFAULT_NOTE

    def decide(self, accept: bool) -> None:
        item = self.current()
        if item is None or self._busy:
            return
        self._busy = True
        self._set_enabled(False)
        detection_id = int(item["detection_id"])

        def done(_result: Any) -> None:
            self._busy = False
            if accept:
                self.accepted += 1
            else:
                self.rejected += 1
            self.state.message.emit(f"detection {detection_id} {'accepted' if accept else 'rejected'}")
            self.items.pop(self.index)
            self.show_current()

        def failed(message: str) -> None:
            self._busy = False
            self._set_enabled(True)
            self.state.message.emit(f"review failed: {message}")

        self.call(self.client.review_detection, detection_id, accept, self._note(), on_result=done, on_error=failed)

    def skip(self) -> None:
        if self.current() is None or self._busy:
            return
        self.index += 1
        if self.index >= len(self.items):
            self.index = 0 if self.items else 0
        self.show_current()

    def accept_rest_of_image(self) -> None:
        item = self.current()
        if item is None or self._busy:
            return
        image_id = item.get("image_id")
        targets = [i for i in self.items if i.get("image_id") == image_id]
        ids = [int(i["detection_id"]) for i in targets]
        players = sorted({int(i["player_id"]) for i in targets if i.get("player_id")})
        self._busy = True
        self._set_enabled(False)

        def done(_result: Any) -> None:
            for pid in players:
                self.call(self.client.recompute_player, pid, on_result=lambda _r: None, on_error=lambda _m: None)
            self._busy = False
            self.accepted += len(ids)
            self.items = [i for i in self.items if i.get("image_id") != image_id]
            self.index = min(self.index, max(len(self.items) - 1, 0))
            self.show_current()

        def failed(message: str) -> None:
            self._busy = False
            self._set_enabled(True)
            self.state.message.emit(f"bulk review failed: {message}")

        self.call(self.client.bulk_review, ids, True, self._note(), on_result=done, on_error=failed)

    # ----------------------------------------------------------------- misc
    def _jump_to_detection(self, detection_id: int) -> None:
        for n, item in enumerate(self.items):
            if item.get("detection_id") == detection_id:
                self.index = n
                self.show_current()
                return

    def _open_player(self) -> None:
        item = self.current()
        if item and item.get("player_id"):
            self.state.open_player.emit(int(item["player_id"]))
