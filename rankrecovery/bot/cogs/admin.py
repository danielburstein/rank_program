"""Staff utility commands under the /rr group."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from rankrecovery.bot.api_client import ApiError
from rankrecovery.bot.cogs import NotStaff, api_of, staff_only
from rankrecovery.bot.formatting import (
    dashboard_text,
    job_status_text,
    not_found_text,
    rank_embed,
    search_results_text,
)

log = logging.getLogger(__name__)


class AdminCog(commands.Cog, name="Admin"):
    rr = app_commands.Group(name="rr", description="Rank recovery staff tools")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @rr.command(name="status", description="Server health and dashboard counts")
    @staff_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        api = api_of(interaction)
        try:
            health = await api.health()
        except ApiError as exc:
            await interaction.followup.send(f"API unreachable: {exc.message}")
            return
        try:
            dashboard = await api.dashboard()
        except ApiError as exc:
            await interaction.followup.send(f"API {health.get('status')} but dashboard failed: {exc.message}")
            return
        await interaction.followup.send(dashboard_text(dashboard, health))

    @rr.command(name="job", description="Show a processing job")
    @app_commands.describe(job_id="Job number from a submission reply")
    @staff_only()
    async def job(self, interaction: discord.Interaction, job_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            job = await api_of(interaction).job(job_id)
        except ApiError as exc:
            await interaction.followup.send("No such job." if exc.status == 404 else f"Could not fetch the job: {exc.message}")
            return
        text = job_status_text(job)
        errors = job.get("errors") or []
        if errors:
            text += "\nErrors:\n" + "\n".join(f"• {e.get('item')}: {e.get('message')}" for e in errors[:10])
        await interaction.followup.send(text[:1900])

    @rr.command(name="recompute", description="Recompute a player's rank summary from stored evidence")
    @app_commands.describe(username="Minecraft username")
    @staff_only()
    async def recompute(self, interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer(ephemeral=True)
        api = api_of(interaction)
        try:
            detail = await api.player_by_name(username.strip())
            if detail is None:
                await interaction.followup.send(not_found_text(username))
                return
            detail = await api.recompute_player(int(detail["player"]["player_id"]))
        except ApiError as exc:
            await interaction.followup.send(f"Recompute failed: {exc.message}")
            return
        await interaction.followup.send(content="Recomputed from stored evidence:", embed=rank_embed(detail, staff_view=True))

    @rr.command(name="find", description="Search players by name, alias or UUID fragment")
    @app_commands.describe(text="Text to search for")
    @staff_only()
    async def find(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            players = await api_of(interaction).search_players(text.strip(), limit=10)
        except ApiError as exc:
            await interaction.followup.send(f"Search failed: {exc.message}")
            return
        await interaction.followup.send(search_results_text(players))

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        text = str(error) if isinstance(error, NotStaff) else "Something went wrong handling that command."
        if not isinstance(error, NotStaff):
            log.exception("admin command error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
