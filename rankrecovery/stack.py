"""Run the API server and the Discord bot in one process.

Made for single-process hosts such as Pterodactyl/Bloom Host panels, where a
server has exactly one startup command and the panel assigns the listening
port through the ``SERVER_PORT`` environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
from pathlib import Path

log = logging.getLogger("rankrecovery.stack")


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    p = Path(path)
    loaded: list[str] = []
    if not p.exists():
        return loaded
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _resolve_bind() -> tuple[str, int]:
    """Panel-provided SERVER_PORT wins over RANKRECOVERY_PORT; bind to all interfaces on panels."""
    panel_port = os.environ.get("SERVER_PORT")
    if panel_port:
        return os.environ.get("RANKRECOVERY_HOST", "0.0.0.0"), int(panel_port)
    return os.environ.get("RANKRECOVERY_HOST", "127.0.0.1"), int(os.environ.get("RANKRECOVERY_PORT", "8420"))


def _bot_configured() -> bool:
    return bool(os.environ.get("RANKRECOVERY_BOT_DISCORD_TOKEN")) and bool(os.environ.get("RANKRECOVERY_BOT_API_KEY"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Rank Recovery API server and Discord bot together")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-bot", action="store_true", help="run the API only")
    parser.add_argument("--no-console", action="store_true", help="do not read admin commands from stdin")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    loaded = load_dotenv(args.env_file)
    level = (args.log_level or os.environ.get("RANKRECOVERY_LOG_LEVEL", "info")).upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if loaded:
        log.info("loaded %d setting(s) from %s", len(loaded), args.env_file)

    data_dir = os.environ.setdefault("RANKRECOVERY_DATA_DIR", "data")
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    host, port = _resolve_bind()

    import uvicorn

    from rankrecovery.api.app import build_context, create_app

    ctx = build_context(os.environ.get("RANKRECOVERY_DATABASE_URL"))
    app = create_app(ctx)
    config = uvicorn.Config(app, host=host, port=port, log_level=level.lower())
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    log.info("API listening on %s:%s (data dir %s)", host, port, data_dir)

    stop = threading.Event()
    bot_thread: threading.Thread | None = None
    bot_loop: asyncio.AbstractEventLoop | None = None
    if not args.no_bot and _bot_configured():
        os.environ.setdefault("RANKRECOVERY_BOT_API_BASE_URL", f"http://127.0.0.1:{port}")

        def run_bot() -> None:
            nonlocal bot_loop
            from rankrecovery.bot.config import BotSettings
            from rankrecovery.bot.main import run

            bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bot_loop)
            try:
                bot_loop.run_until_complete(run(BotSettings()))
            except Exception:  # pragma: no cover - logged, API keeps running
                log.exception("Discord bot stopped with an error; the API keeps running")
            finally:
                bot_loop.close()

        bot_thread = threading.Thread(target=run_bot, name="discord-bot", daemon=True)
        bot_thread.start()
        log.info("Discord bot starting")
    elif not args.no_bot:
        log.warning("Discord bot not started: set RANKRECOVERY_BOT_DISCORD_TOKEN and RANKRECOVERY_BOT_API_KEY in %s", args.env_file)

    def handle_signal(signum, _frame) -> None:  # type: ignore[no-untyped-def]
        log.info("signal %s received; shutting down", signum)
        stop.set()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except ValueError:  # pragma: no cover - not main thread
            pass

    if not args.no_console:
        from rankrecovery.console import AdminConsole

        def request_stop() -> None:
            stop.set()
            server.should_exit = True

        AdminConsole(ctx, app.state.runner, stop=request_stop).start()

    server.run()
    if bot_thread is not None and bot_loop is not None and bot_thread.is_alive():
        try:
            from rankrecovery.bot import main as bot_main

            shutdown = getattr(bot_main, "request_shutdown", None)
            if shutdown is not None:
                bot_loop.call_soon_threadsafe(shutdown)
        except Exception:  # pragma: no cover
            pass
        bot_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
