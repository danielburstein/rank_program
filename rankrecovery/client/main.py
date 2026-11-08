"""Desktop client entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from rankrecovery.client.api_client import ClientConfig, ServerClient, ServerError
from rankrecovery.client.state import AppState
from rankrecovery.client.theme import apply_theme
from rankrecovery.client.workers import run_async

PAGES: list[tuple[str, str, str]] = [
    ("dashboard", "Dashboard", "rankrecovery.client.pages.dashboard:DashboardPage"),
    ("submit", "Submit screenshots", "rankrecovery.client.pages.submit:SubmitPage"),
    ("players", "Mode 1 · Players", "rankrecovery.client.pages.players:PlayersPage"),
    ("review", "Review queue", "rankrecovery.client.pages.review:ReviewPage"),
    ("images", "Screenshots", "rankrecovery.client.pages.images:ImagesPage"),
    ("csv", "Trusted CSV", "rankrecovery.client.pages.csv_page:CsvPage"),
    ("conversion", "Mode 2 · Conversion", "rankrecovery.client.pages.conversion:ConversionPage"),
    ("identity", "Mode 3 · Identities", "rankrecovery.client.pages.identity:IdentityPage"),
    ("export", "Mode 4 · Export", "rankrecovery.client.pages.export:ExportPage"),
    ("search", "Mode 5 · Search", "rankrecovery.client.pages.search:SearchPage"),
    ("rescan", "Mode 6 · Rescan", "rankrecovery.client.pages.rescan:RescanPage"),
    ("settings", "Settings", "rankrecovery.client.pages.settings:SettingsPage"),
    ("logs", "Jobs & logs", "rankrecovery.client.pages.logs:LogsPage"),
]


def _load(path: str):  # noqa: ANN202
    module_name, cls_name = path.split(":")
    module = __import__(module_name, fromlist=[cls_name])
    return getattr(module, cls_name)


class ConnectionDialog(QDialog):
    def __init__(self, config: ClientConfig, parent: QWidget | None = None, *, client_factory=ServerClient):  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("Connect to Rank Recovery server")
        self.config = config
        self.client_factory = client_factory
        self.client: ServerClient | None = None
        form = QFormLayout(self)
        self.url = QLineEdit(config.base_url)
        self.key = QLineEdit(config.api_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        form.addRow("Server URL", self.url)
        form.addRow("API key", self.key)
        form.addRow("", self.status)
        buttons = QDialogButtonBox()
        self.test_btn = buttons.addButton("Test connection", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.test_btn.clicked.connect(self.test)
        buttons.accepted.connect(self.connect_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        form.addRow("", QLabel("The API key is saved in ~/.rankrecovery/client.json on this computer."))

    def _make(self) -> ServerClient:
        return self.client_factory(self.url.text().strip(), self.key.text().strip(), self.config.timeout)

    def test(self) -> None:
        self.status.setText("testing…")
        client = self._make()
        run_async(client.dashboard, on_result=lambda d: self.status.setText(f"OK: {d.get('players')} players, {d.get('images')} screenshots"), on_error=lambda m: self.status.setText(f"failed: {m}"))

    def connect_and_accept(self) -> None:
        client = self._make()
        try:
            client.health()
        except ServerError as exc:
            if exc.status == 401:
                self.status.setText("invalid API key")
                return
            if QMessageBox.question(self, "Server unreachable", f"{exc.message}\n\nKeep these settings anyway?") != QMessageBox.StandardButton.Yes:
                self.status.setText(f"failed: {exc.message}")
                return
        self.config.base_url = self.url.text().strip()
        self.config.api_key = self.key.text().strip()
        self.client = client
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle("Rank Recovery")
        self.resize(1480, 920)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        for key, label, path in PAGES:
            page = _load(path)(state)
            self.pages[key] = page
            self.stack.addWidget(page)
            self.nav.addItem(QListWidgetItem(label))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._page_shown)
        self.nav.setCurrentRow(0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.nav)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.health_label = QLabel("not connected")
        self.jobs_label = QLabel("")
        self.status_bar.addPermanentWidget(self.jobs_label)
        self.status_bar.addPermanentWidget(self.health_label)
        state.message.connect(lambda text: self.status_bar.showMessage(text, 8000))
        state.open_player.connect(lambda _pid: self.show_page("players"))
        state.open_image.connect(lambda _iid: self.show_page("images"))
        state.open_page.connect(self.show_page)
        state.client_changed.connect(self._bootstrap)
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(10_000)
        self.health_timer.timeout.connect(self._poll_health)
        if state.client is not None:
            self._bootstrap()

    def show_page(self, key: str) -> None:
        index = [k for k, _l, _p in PAGES].index(key)
        self.nav.setCurrentRow(index)

    def _page_shown(self, index: int) -> None:
        """Load a page's data the first time it is selected (independent of window visibility)."""
        page = self.stack.widget(index)
        if page is not None and self.state.client is not None and not getattr(page, "_loaded", True):
            page._loaded = True  # noqa: SLF001
            page.refresh()

    def _bootstrap(self) -> None:
        if self.state.client is None:
            return
        run_async(self.state.client.flag_catalogue, on_result=self._set_catalogue, on_error=lambda m: self.state.message.emit(m))
        run_async(self.state.client.policy, on_result=self._set_policy, on_error=lambda m: self.state.message.emit(m))
        self.health_timer.start()
        self._poll_health()
        current = self.stack.currentWidget()
        if hasattr(current, "refresh") and getattr(current, "_loaded", False) is False:
            current._loaded = True  # noqa: SLF001
            current.refresh()

    def _set_catalogue(self, catalogue: list[dict]) -> None:
        self.state.flag_catalogue = catalogue

    def _set_policy(self, policy: dict) -> None:
        self.state.policy = policy

    def _poll_health(self) -> None:
        if self.state.client is None:
            return
        run_async(self.state.client.health, on_result=lambda h: self.health_label.setText(f"{self.state.config.base_url} · v{h.get('version')} · {'vision' if h.get('vision_configured') else 'OCR only'}"), on_error=lambda m: self.health_label.setText(f"server unreachable: {m[:60]}"))
        run_async(self.state.client.jobs, 20, "running", on_result=lambda jobs: self.jobs_label.setText(f"{len(jobs)} running job(s)" if jobs else ""), on_error=lambda _m: None)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        self.state.config.window = {"w": self.width(), "h": self.height()}
        try:
            self.state.config.save()
        except OSError:
            pass
        self.health_timer.stop()
        super().closeEvent(event)


def create_main_window(config: ClientConfig | None = None, client: ServerClient | None = None) -> MainWindow:
    config = config or ClientConfig.load()
    state = AppState(config, client)
    return MainWindow(state)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Rank Recovery")
    apply_theme(app)
    config = ClientConfig.load()
    window = create_main_window(config)
    while True:
        dialog = ConnectionDialog(config)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.client is None:
            if QMessageBox.question(None, "Rank Recovery", "No server connection. Retry?") == QMessageBox.StandardButton.Yes:
                continue
            return 0
        config.save()
        window.state.set_client(dialog.client)
        break
    size = config.window or {}
    if size.get("w") and size.get("h"):
        window.resize(int(size["w"]), int(size["h"]))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
