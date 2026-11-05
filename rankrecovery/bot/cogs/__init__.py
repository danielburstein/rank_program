"""Bot cogs."""

from __future__ import annotations

import discord
from discord import app_commands


def settings_of(interaction: discord.Interaction):
    return getattr(interaction.client, "rr_settings", None)


def api_of(interaction: discord.Interaction):
    return getattr(interaction.client, "rr_api", None)


def is_staff_member(member: discord.abc.User | discord.Member | None, settings) -> bool:
    if member is None or settings is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    role_ids = {r.id for r in getattr(member, "roles", []) or []}
    allowed = set(getattr(settings, "staff_role_ids", []) or []) | set(getattr(settings, "admin_role_ids", []) or [])
    return bool(role_ids & allowed)


def is_staff_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return is_staff_member(interaction.user, settings_of(interaction))


class NotStaff(app_commands.CheckFailure):
    pass


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_staff_interaction(interaction):
            return True
        raise NotStaff("This command is limited to staff members.")

    return app_commands.check(predicate)


def ephemeral_for(interaction: discord.Interaction) -> bool:
    settings = settings_of(interaction)
    return bool(getattr(settings, "ephemeral_replies", False))
