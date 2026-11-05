from __future__ import annotations

import os

from rankrecovery.stack import _resolve_bind, load_dotenv


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('A_KEY="x"\n# comment\nB_KEY=y\nBROKEN\n', encoding="utf-8")
    monkeypatch.setenv("A_KEY", "already")
    monkeypatch.delenv("B_KEY", raising=False)
    loaded = load_dotenv(env)
    assert loaded == ["B_KEY"] and os.environ["A_KEY"] == "already" and os.environ["B_KEY"] == "y"


def test_panel_port_wins(monkeypatch):
    monkeypatch.delenv("RANKRECOVERY_HOST", raising=False)
    monkeypatch.setenv("SERVER_PORT", "25570")
    assert _resolve_bind() == ("0.0.0.0", 25570)
    monkeypatch.delenv("SERVER_PORT")
    monkeypatch.setenv("RANKRECOVERY_PORT", "9000")
    assert _resolve_bind() == ("127.0.0.1", 9000)
