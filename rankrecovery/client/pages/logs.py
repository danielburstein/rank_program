from __future__ import annotations

from rankrecovery.client.pages.base import Page
from rankrecovery.client.widgets.job_panel import JobPanel
from rankrecovery.client.widgets.log_view import LogView


class LogsPage(Page):
    title = "Jobs & logs"

    def __init__(self, state, parent=None):  # noqa: ANN001
        super().__init__(state, parent)
        self.jobs = JobPanel(state)
        self.logs = LogView(state)
        self.root.addWidget(self.jobs, 1)
        self.root.addWidget(self.logs, 1)

    def refresh(self) -> None:
        self.jobs.refresh()
        self.logs.refresh()
