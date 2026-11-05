"""Bot configuration from environment variables / .env (prefix RANKRECOVERY_BOT_)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_int_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            import json

            return [int(v) for v in json.loads(text)]
        return [int(part.strip()) for part in text.replace(";", ",").split(",") if part.strip()]
    return [int(v) for v in value]


class BotSettings(BaseSettings):
    """All settings are read from ``RANKRECOVERY_BOT_*`` environment variables or a ``.env`` file."""

    model_config = SettingsConfigDict(env_prefix="RANKRECOVERY_BOT_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: SecretStr
    api_key: SecretStr
    api_base_url: str = "http://127.0.0.1:8420"
    staff_role_ids: Annotated[list[int], NoDecode] = []
    admin_role_ids: Annotated[list[int], NoDecode] = []
    guild_ids: Annotated[list[int], NoDecode] = []
    max_attachments_per_submit: int = 10
    max_attachment_mb: int = 25
    lookup_cooldown_seconds: float = 5.0
    public_lookup: bool = True
    ephemeral_replies: bool = False
    log_level: str = "INFO"
    api_timeout_seconds: float = 30.0
    upload_timeout_seconds: float = 300.0
    job_poll_interval_seconds: float = 3.0
    job_poll_max_seconds: float = 120.0

    @field_validator("staff_role_ids", "admin_role_ids", "guild_ids", mode="before")
    @classmethod
    def _int_lists(cls, value: Any) -> list[int]:
        return _parse_int_list(value)

    def secrets(self) -> list[str]:
        """Secret values the log filter must redact."""
        return [s for s in (self.discord_token.get_secret_value(), self.api_key.get_secret_value()) if s]

    def __repr__(self) -> str:  # never expose secrets
        return f"BotSettings(api_base_url={self.api_base_url!r}, guild_ids={self.guild_ids}, staff_role_ids={self.staff_role_ids}, public_lookup={self.public_lookup})"

    __str__ = __repr__
