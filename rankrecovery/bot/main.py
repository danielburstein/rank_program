"""Entry point: ``rankrecovery-bot``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import discord
from discord.ext import commands

from rankrecovery.bot.api_client import RankApiClient

log = logging.getLogger(__name__)

COG_MODULES = ("rankrecovery.bot.cogs.rank", "rankrecovery.bot.cogs.submit", "rankrecovery.bot.cogs.admin")


class RedactingFilter(logging.Filter):
    """Replaces secret values in log output."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        redacted = message
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[redacted]")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str, secrets: list[str]) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    redactor = RedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class RankBot(commands.Bot):
    def __init__(self, settings: Any, api: RankApiClient):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.rr_settings = settings
        self.rr_api = api

    async def setup_hook(self) -> None:
        for module in COG_MODULES:
            await self.load_extension(module)
        guild_ids = list(getattr(self.rr_settings, "guild_ids", []) or [])
        if guild_ids:
            for gid in guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("synced %d commands to guild %s", len(synced), gid)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global commands (may take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.rr_api.aclose()


async def create_bot(settings: Any, api: RankApiClient, *, load_cogs: bool = True) -> RankBot:
    """Build the bot without connecting. Tests use this with a fake API client."""
    bot = RankBot(settings, api)
    if load_cogs:
        for module in COG_MODULES:
            await bot.load_extension(module)
    return bot


async def run(settings: Any) -> None:
    api = RankApiClient(settings.api_base_url, settings.api_key.get_secret_value(), timeout=settings.api_timeout_seconds, upload_timeout=settings.upload_timeout_seconds)
    bot = RankBot(settings, api)
    loop = asyncio.get_running_loop()

    def _stop(*_: Any) -> None:
        loop.create_task(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    async with bot:
        await bot.start(settings.discord_token.get_secret_value())


def main(argv: list[str] | None = None) -> int:
    from rankrecovery.bot.config import BotSettings

    try:
        settings = BotSettings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic validation error: missing token/key
        print(f"configuration error: {exc}", file=sys.stderr)
        print("set RANKRECOVERY_BOT_DISCORD_TOKEN and RANKRECOVERY_BOT_API_KEY (see docs/BOT.md)", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.secrets())
    log.info("starting bot: %s", settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
