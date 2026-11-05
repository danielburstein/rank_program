from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.forms import error_box, info_box
from rankrecovery.client.widgets.job_panel import JobPanel

COUNTERS = [
    ("players", "Players"),
    ("images", "Screenshots"),
    ("confirmed_players", "Confirmed"),
    ("provisional_players", "Provisional"),
    ("stale_summaries", "Stale summaries"),
    ("staff_only_players", "Staff only"),
    ("bedrock_players", "Bedrock"),
    ("unresolved_identities", "Unresolved UUIDs"),
    ("detections_needing_review", "Detections to review"),
    ("exportable_players", "Exportable"),
    ("trusted_csv_rows", "Trusted CSV rows"),
]


class DashboardPage(Page):
    title = "Dashboard"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.health_label = QLabel("")
        self.health_label.setObjectName("dim")
        self.root.addWidget(self.health_label)
        grid_box = QGroupBox("Overview")
        self.grid = QGridLayout(grid_box)
        self.values: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(COUNTERS):
            value = QLabel("–")
            value.setObjectName("title")
            caption = QLabel(label)
            caption.setObjectName("dim")
            box = QVBoxLayout()
            box.addWidget(value)
            box.addWidget(caption)
            self.grid.addLayout(box, i // 6, i % 6)
            self.values[key] = value
        self.root.addWidget(grid_box)
        self.flags_label = QLabel("")
        self.flags_label.setWordWrap(True)
        self.root.addWidget(self.flags_label)
        actions = QHBoxLayout()
        self.extract_btn = QPushButton("Extract pending screenshots")
        self.recompute_btn = QPushButton("Recompute stale players")
        self.convert_btn = QPushButton("Run Mode 2 conversion")
        self.refresh_btn = QPushButton("Refresh")
        self.extract_btn.clicked.connect(self._extract)
        self.recompute_btn.clicked.connect(self._recompute)
        self.convert_btn.clicked.connect(self._convert)
        self.refresh_btn.clicked.connect(self.refresh)
        for b in (self.extract_btn, self.recompute_btn, self.convert_btn, self.refresh_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        self.root.addLayout(actions)
        self.root.addWidget(QLabel("Recent jobs"))
        self.jobs = JobPanel(state)
        self.root.addWidget(self.jobs, 1)

    def refresh(self) -> None:
        self.call(self.client.health, on_result=self._health, on_error=lambda m: self.health_label.setText(f"server unreachable: {m}"))
        self.call(self.client.dashboard, on_result=self._fill)
        self.jobs.refresh()

    def _health(self, data: dict) -> None:
        vision = "vision model configured" if data.get("vision_configured") else "no vision API key: local OCR fallback (low accuracy on Minecraft font)"
        self.health_label.setText(f"Server {data.get('version')} · {data.get('database')} · {vision}")

    def _fill(self, data: dict) -> None:
        for key, label in self.values.items():
            label.setText(str(data.get(key, "–")))
        counts = data.get("flag_counts") or {}
        if counts:
            self.flags_label.setText("Active player flags: " + ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items())))
        else:
            self.flags_label.setText("No active player flags.")

    def _extract(self) -> None:
        self.call(self.client.extract, on_result=lambda r: (self.watch_job(int(r["job_id"]), label="extract", on_finished=lambda _j: self.refresh()), info_box(self, f"Extraction job {r['job_id']} started for {r.get('pending', 0)} image(s).")))

    def _recompute(self) -> None:
        self.call(self.client.recompute, on_result=lambda r: self.watch_job(int(r["job_id"]), label="recompute", on_finished=lambda _j: self.refresh()))

    def _convert(self) -> None:
        self.call(self.client.run_conversion, on_result=lambda r: self.watch_job(int(r["job_id"]), label="conversion", on_finished=lambda _j: self.refresh()), on_error=lambda m: error_box(self, m))
