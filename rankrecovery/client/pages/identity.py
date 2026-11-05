"""Mode 3: UUID lookup and identity reconciliation."""

from __future__ import annotations

from collections import Counter

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.data_table import DataTable
from rankrecovery.client.widgets.forms import error_box, info_box

ID_COLUMNS = [("display_name", "Player"), ("aliases", "Known names"), ("uuid", "UUID"), ("current_name", "Current name"), ("identity_status", "Status"), ("identity_source", "Source"), ("is_bedrock", "Bedrock"), ("effective_rank", "Effective rank"), ("last_lookup", "Last lookup"), ("player_id", "ID")]


class IdentityPage(Page):
    title = "Mode 3 · UUID lookup and identities"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        bar = QHBoxLayout()
        self.all_btn = QPushButton("Lookup all unresolved")
        self.failed_btn = QPushButton("Retry failed")
        self.proxy_btn = QPushButton("Retry proxy failures")
        self.selected_btn = QPushButton("Lookup selected")
        self.force_btn = QPushButton("Force re-lookup selected")
        self.refresh_btn = QPushButton("Refresh")
        self.all_btn.clicked.connect(lambda: self._lookup())
        self.failed_btn.clicked.connect(lambda: self._lookup(retry_failed=True))
        self.proxy_btn.clicked.connect(lambda: self._lookup(retry_failed=True, retry_proxy_failures=True))
        self.selected_btn.clicked.connect(lambda: self._lookup(selected=True))
        self.force_btn.clicked.connect(lambda: self._lookup(selected=True, force=True))
        self.refresh_btn.clicked.connect(self.refresh)
        for b in (self.all_btn, self.failed_btn, self.proxy_btn, self.selected_btn, self.force_btn, self.refresh_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.root.addLayout(bar)
        self.proxy_label = QLabel("")
        self.proxy_label.setObjectName("dim")
        self.proxy_label.setWordWrap(True)
        self.root.addWidget(self.proxy_label)
        self.failure_label = QLabel("")
        self.failure_label.setWordWrap(True)
        self.root.addWidget(self.failure_label)
        self.job_label = QLabel("")
        self.root.addWidget(self.job_label)
        self.table = DataTable(ID_COLUMNS, colour_key="identity_status")
        self.table.add_row_action("Open player", lambda r: self.state.open_player.emit(int(r["player_id"])))
        self.table.row_activated.connect(lambda r: self.state.open_player.emit(int(r["player_id"])))
        self.root.addWidget(self.table, 1)
        note = QLabel("Current-name lookups are not proof of historical ownership. Mojang removed the name-history endpoint in 2022; names that cannot be resolved stay unresolved and are excluded from export. Bedrock (dot) names go to the Floodgate/Geyser provider only.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        self.root.addWidget(note)

    def refresh(self) -> None:
        self.call(self.client.identity_table, 2000, 0, on_result=self._fill)
        self.call(self.client.proxy_status, on_result=lambda r: self.proxy_label.setText(f"proxies configured: {r.get('count', 0)} " + (", ".join(r.get('entries', [])[:10])) + (f" · errors: {r['errors']}" if r.get("errors") else "")), on_error=lambda _m: self.proxy_label.setText("proxy status unavailable (admin key required)"))

    def _fill(self, rows: list[dict]) -> None:
        for r in rows:
            last = r.get("last_lookup")
            r["last_lookup"] = f"{last['provider']} {last['outcome']} {last.get('failure_code') or ''} http={last.get('http_status')}" if last else ""
        self.table.set_rows(rows)
        statuses = Counter(r.get("identity_status") for r in rows)
        failures = Counter(x["last_lookup"].split()[2] for x in rows if x["last_lookup"] and len(x["last_lookup"].split()) > 2 and x["last_lookup"].split()[2].startswith("UUID_"))
        self.failure_label.setText("Statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())) + ("   Failures: " + ", ".join(f"{k}={v}" for k, v in failures.most_common()) if failures else ""))

    def _lookup(self, *, retry_failed: bool = False, retry_proxy_failures: bool = False, selected: bool = False, force: bool = False) -> None:
        body = {"retry_failed": retry_failed, "retry_proxy_failures": retry_proxy_failures, "force": force}
        if selected:
            rows = self.table.selected_rows()
            if not rows:
                error_box(self, "Select players first.")
                return
            body["player_ids"] = [int(r["player_id"]) for r in rows]
        self.call(self.client.lookup, on_result=lambda r: self.watch_job(int(r["job_id"]), label="lookup", on_progress=lambda j: self.job_label.setText(f"lookup job {j['id']}: {j['status']} {j.get('done')}/{j.get('total')} {j.get('progress_message', '')}"), on_finished=self._done), **body)

    def _done(self, job: dict) -> None:
        self.job_label.setText(f"lookup job {job['id']} {job['status']}: {job.get('result') or job.get('error')}")
        info_box(self, f"Lookup finished: {job.get('result') or job.get('error')}")
        self.refresh()
