"""Rank lookup, /myrank and Discord-account linking."""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from rankrecovery.bot.api_client import ApiError
from rankrecovery.bot.cogs import NotStaff, api_of, ephemeral_for, is_staff_interaction, settings_of
from rankrecovery.bot.formatting import not_found_text, rank_embed

log = logging.getLogger(__name__)


class RankCog(commands.Cog, name="Rank"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_lookup: dict[int, float] = {}
        self._lookup_menu = app_commands.ContextMenu(name="Rank lookup", callback=self._context_lookup)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._lookup_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._lookup_menu.name, type=self._lookup_menu.type)

    # ----------------------------------------------------------------- helpers
    def _cooldown_remaining(self, interaction: discord.Interaction) -> float:
        settings = settings_of(interaction)
        seconds = float(getattr(settings, "lookup_cooldown_seconds", 5.0) or 0)
        if seconds <= 0 or is_staff_interaction(interaction):
            return 0.0
        now = time.monotonic()
        last = self._last_lookup.get(interaction.user.id)
        if last is not None and now - last < seconds:
            return seconds - (now - last)
        self._last_lookup[interaction.user.id] = now
        return 0.0

    async def _send_rank(self, interaction: discord.Interaction, username: str) -> None:
        settings = settings_of(interaction)
        if not getattr(settings, "public_lookup", True) and not is_staff_interaction(interaction):
            await interaction.response.send_message("Rank lookups are limited to staff on this server.", ephemeral=True)
            return
        remaining = self._cooldown_remaining(interaction)
        if remaining > 0:
            await interaction.response.send_message(f"Please wait {remaining:.0f}s before another lookup.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=ephemeral_for(interaction))
        try:
            detail = await api_of(interaction).player_by_name(username)
        except ApiError as exc:
            log.warning("rank lookup failed for %r: %s", username, exc)
            await interaction.followup.send("The rank server could not be reached right now. Please try again later.")
            return
        if detail is None:
            await interaction.followup.send(not_found_text(username))
            return
        await interaction.followup.send(embed=rank_embed(detail, staff_view=is_staff_interaction(interaction)))

    async def _linked_username(self, interaction: discord.Interaction, user: discord.abc.User) -> str | None:
        try:
            link = await api_of(interaction).get_link(str(user.id))
        except ApiError as exc:
            log.warning("link lookup failed for %s: %s", user.id, exc)
            return None
        return (link or {}).get("username")

    # ---------------------------------------------------------------- commands
    @app_commands.command(name="rank", description="Show the recovered rank for a Minecraft username")
    @app_commands.describe(username="Minecraft username as it appeared in the old screenshots")
    async def rank(self, interaction: discord.Interaction, username: str) -> None:
        username = username.strip()
        if not username or len(username) > 20:
            await interaction.response.send_message("Please give a valid Minecraft username.", ephemeral=True)
            return
        await self._send_rank(interaction, username)

    @app_commands.command(name="myrank", description="Show the recovered rank for your linked Minecraft account")
    async def myrank(self, interaction: discord.Interaction) -> None:
        username = await self._linked_username(interaction, interaction.user)
        if not username:
            await interaction.response.send_message("You have not linked a Minecraft username yet. Use `/link <username>` first.", ephemeral=True)
            return
        await self._send_rank(interaction, username)

    @app_commands.command(name="link", description="Link a Discord account to a Minecraft username")
    @app_commands.describe(username="Minecraft username", member="(staff only) link another member and mark the link as verified")
    async def link(self, interaction: discord.Interaction, username: str, member: discord.Member | None = None) -> None:
        username = username.strip()
        if not username or len(username) > 20:
            await interaction.response.send_message("Please give a valid Minecraft username.", ephemeral=True)
            return
        target = interaction.user
        verified = False
        if member is not None and member.id != interaction.user.id:
            if not is_staff_interaction(interaction):
                await interaction.response.send_message("Only staff can link other members.", ephemeral=True)
                return
            target = member
            verified = True
        try:
            link = await api_of(interaction).link(str(target.id), str(target), username, verified=verified)
        except ApiError as exc:
            log.warning("link failed for %s: %s", target.id, exc)
            await interaction.response.send_message("Could not store the link right now. Please try again later.", ephemeral=True)
            return
        who = "You are" if target.id == interaction.user.id else f"{target.mention} is"
        status = "verified by staff" if link.get("verified") else "unverified"
        await interaction.response.send_message(f"{who} now linked to **{discord.utils.escape_markdown(username)}** ({status}).", ephemeral=True)

    @app_commands.command(name="unlink", description="Remove your Minecraft username link")
    async def unlink(self, interaction: discord.Interaction) -> None:
        try:
            removed = await api_of(interaction).unlink(str(interaction.user.id))
        except ApiError as exc:
            log.warning("unlink failed for %s: %s", interaction.user.id, exc)
            await interaction.response.send_message("Could not remove the link right now.", ephemeral=True)
            return
        await interaction.response.send_message("Link removed." if removed else "You had no link.", ephemeral=True)

    async def _context_lookup(self, interaction: discord.Interaction, member: discord.Member) -> None:
        username = await self._linked_username(interaction, member)
        if not username:
            await interaction.response.send_message(f"{member.display_name} has not linked a Minecraft username.", ephemeral=True)
            return
        await self._send_rank(interaction, username)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, NotStaff):
            await _reply(interaction, str(error))
            return
        log.exception("command error in %s", getattr(interaction.command, "name", "?"), exc_info=error)
        await _reply(interaction, "Something went wrong handling that command.")


async def _reply(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:  # pragma: no cover
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RankCog(bot))
