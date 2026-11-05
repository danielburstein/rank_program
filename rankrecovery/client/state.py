"""Shared client state and cross-page navigation signals."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from rankrecovery.client.api_client import ClientConfig, ServerClient


class AppState(QObject):
    """Holds the live server client and broadcasts navigation requests."""

    open_player = Signal(int)
    open_image = Signal(int)
    open_page = Signal(str)
    job_started = Signal(int, str)
    message = Signal(str)  # status-bar text
    client_changed = Signal()

    def __init__(self, config: ClientConfig, client: ServerClient | None = None):
        super().__init__()
        self.config = config
        self.client = client
        self.flag_catalogue: list[dict] = []
        self.policy: dict = {}

    def set_client(self, client: ServerClient) -> None:
        if self.client is not None and self.client is not client:
            try:
                self.client.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self.client = client
        self.client_changed.emit()

    def require(self) -> ServerClient:
        if self.client is None:
            raise RuntimeError("not connected to a server")
        return self.client
