"""Set UUIDs by hand: names the lookups could not resolve, a few at a time, with a NameMC link per name."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from rankrecovery.client.pages.base import Page
from rankrecovery.client.state import AppState

SKIP_REASON = "could not find the account by hand"


class NameCard(QGroupBox):
    """One unresolved name: what we know, a NameMC button, a UUID field and set/skip buttons."""

    def __init__(self, item: dict[str, Any], parent: QWidget | None = None):
        super().__init__(str(item["name"]), parent)
        self.item = item
        self.player_id = int(item["player_id"])
        lay = QVBoxLayout(self)
        rank = item.get("effective_rank") or item.get("highest_observed") or "no rank yet"
        others = [a for a in item.get("aliases") or [] if a != item["name"]]
        info = QLabel(f"rank {rank} · {item.get('images', 0)} screenshot(s) · status {item.get('identity_status')}" + (f" · also seen as {', '.join(others[:6])}" if others else ""))
        info.setWordWrap(True)
        lay.addWidget(info)
        if item.get("why"):
            why = QLabel(str(item["why"]))
            why.setWordWrap(True)
            why.setObjectName("dim")
            lay.addWidget(why)
        for cand in item.get("candidates") or []:
            cand_label = QLabel(f"candidate {cand.get('uuid')} (now {cand.get('current_name') or '?'})")
            cand_label.setTextInteractionFlags(cand_label.textInteractionFlags() | __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(cand_label)
        row = QHBoxLayout()
        self.namemc_btn = QPushButton("Open on NameMC")
        self.namemc_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(str(item.get("namemc_url") or ""))))
        self.uuid = QLineEdit()
        self.uuid.setPlaceholderText("paste the UUID from NameMC")
        self.note = QLineEdit()
        self.note.setPlaceholderText("note (optional)")
        self.set_btn = QPushButton("Set UUID")
        self.set_btn.setObjectName("accept")
        self.skip_btn = QPushButton("Skip: can't find it")
        for widget, stretch in ((self.namemc_btn, 0), (self.uuid, 2), (self.note, 1), (self.set_btn, 0), (self.skip_btn, 0)):
            row.addWidget(widget, stretch)
        lay.addLayout(row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self.uuid.returnPressed.connect(self.set_btn.click)

    @property
    def finished(self) -> bool:
        return not self.set_btn.isEnabled() and not self.skip_btn.isEnabled()

    def finish(self, text: str, *, ok: bool = True) -> None:
        self.result.setText(text)
        self.result.setObjectName("accept" if ok else "warning")
        for widget in (self.uuid, self.note, self.set_btn, self.skip_btn):
            widget.setEnabled(False)


class IdentityReviewPage(Page):
    title = "Mode 3 · Set UUIDs by hand"

    def __init__(self, state: AppState, parent: QWidget | None = None):
        super().__init__(state, parent)
        self.cards: list[NameCard] = []
        self.done = 0

        intro = QLabel("Names the automatic lookups could not resolve, highest rank first. Look each one up on NameMC, paste the UUID and press Set; Mojang confirms the account's current name. Skip names that are plainly misreads so they stop coming back.")
        intro.setWordWrap(True)
        intro.setObjectName("dim")
        self.root.addWidget(intro)

        controls = QHBoxLayout()
        self.batch = QSpinBox()
        self.batch.setRange(1, 25)
        self.batch.setValue(5)
        self.include_skipped = QCheckBox("include skipped")
        self.search = QLineEdit()
        self.search.setPlaceholderText("only names containing… (optional)")
        self.load_btn = QPushButton("Load names")
        self.load_btn.clicked.connect(self.load)
        self.counter = QLabel("")
        self.counter.setObjectName("dim")
        controls.addWidget(QLabel("Batch"))
        controls.addWidget(self.batch)
        controls.addWidget(self.include_skipped)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.load_btn)
        controls.addWidget(self.counter, 1)
        self.root.addLayout(controls)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.addStretch(1)
        scroll.setWidget(self.container)
        self.root.addWidget(scroll, 1)
        self.search.returnPressed.connect(self.load)

    # --------------------------------------------------------------- loading
    def refresh(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.load()

    def load(self) -> None:
        self.call(self.client.review_queue, limit=self.batch.value(), include_skipped=self.include_skipped.isChecked(), search=self.search.text().strip(), on_result=self._show)

    def _show(self, data: dict[str, Any]) -> None:
        for card in self.cards:
            self.container_layout.removeWidget(card)
            card.deleteLater()
        self.cards = []
        items = list(data.get("items") or [])
        for item in items:
            card = NameCard(item)
            card.set_btn.clicked.connect(lambda _=False, c=card: self._set_uuid(c))
            card.skip_btn.clicked.connect(lambda _=False, c=card: self._skip(c))
            self.container_layout.insertWidget(self.container_layout.count() - 1, card)
            self.cards.append(card)
        total = int(data.get("total") or 0)
        self.counter.setText(f"{len(items)} shown · {total} waiting · {self.done} done this session" if items else f"nothing left to review · {self.done} done this session")

    # --------------------------------------------------------------- actions
    def _set_uuid(self, card: NameCard) -> None:
        uuid = card.uuid.text().strip()
        if not uuid:
            card.result.setText("Paste a UUID first.")
            return
        card.set_btn.setEnabled(False)
        card.result.setText("checking with Mojang…")
        self.call(self.client.review_set_uuid, card.player_id, uuid, card.note.text().strip(), on_result=lambda r, c=card: self._set_done(c, r), on_error=lambda m, c=card: self._set_failed(c, m))

    def _set_done(self, card: NameCard, result: dict[str, Any]) -> None:
        now = result.get("current_name")
        text = f"✓ {result.get('uuid')} · " + (f"account now called {now}" if now else "Mojang could not confirm a current name") + f" · rank {result.get('effective_rank') or 'none yet'}" + (f" → {result['new_rank']}" if result.get("new_rank") else "") + (f" · {result['message']}" if result.get("message") else "")
        card.finish(text)
        self.done += 1
        self._maybe_reload()

    def _set_failed(self, card: NameCard, message: str) -> None:
        card.result.setText(message)
        card.set_btn.setEnabled(True)

    def _skip(self, card: NameCard) -> None:
        card.skip_btn.setEnabled(False)
        self.call(self.client.review_skip, card.player_id, SKIP_REASON, on_result=lambda _r, c=card: self._skip_done(c), on_error=lambda m, c=card: (c.result.setText(m), c.skip_btn.setEnabled(True)))

    def _skip_done(self, card: NameCard) -> None:
        card.finish("skipped; it stays out of the queue until un-skipped", ok=False)
        self._maybe_reload()

    def _maybe_reload(self) -> None:
        if self.cards and all(c.finished for c in self.cards):
            self.load()
