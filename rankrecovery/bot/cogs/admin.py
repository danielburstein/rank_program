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

    @rr.command(name="lookup", description="Find UUIDs for players missing one: Mojang first, then the last account that held the name")
    @app_commands.describe(retry_failed="Only names whose last lookup failed", force="Ignore cached answers")
    @staff_only()
    async def lookup(self, interaction: discord.Interaction, retry_failed: bool = False, force: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            job = await api_of(interaction).start_lookup(retry_failed=retry_failed, force=force)
        except ApiError as exc:
            await interaction.followup.send(f"Could not start the lookup: {exc.message}")
            return
        job_id = job.get("job_id")
        await interaction.followup.send(f"Queued UUID lookup as job #{job_id}. Players who already have a UUID are skipped; each remaining old name is checked against Mojang first, and names nobody holds today go to the most recent account that used them. Follow it with `/rr job {job_id}`.")

    @rr.command(name="ingest", description="Submit every screenshot dropped into the server's newuploads folder")
    @app_commands.describe(submitter="Who took these screenshots (recorded as the submitter)")
    @staff_only()
    async def ingest(self, interaction: discord.Interaction, submitter: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            queued = await api_of(interaction).ingest_folder(submitter=submitter)
        except ApiError as exc:
            await interaction.followup.send(f"Ingest failed: {exc.message}")
            return
        if not queued.get("found"):
            await interaction.followup.send(f"Nothing waiting in `{queued.get('folder')}`. Drop screenshots there with the panel's File Manager and run this again.")
            return
        job_id = queued.get("job_id")
        await interaction.followup.send(f"Found {queued['found']} file(s) in `{queued.get('folder')}`; submitting them as job #{job_id}. Processed files disappear from the folder as they go, extraction is queued at the end. Follow it with `/rr job {job_id}`.")

    @rr.command(name="drive", description="Download the photos behind a shared Google Drive link into newuploads and submit them")
    @app_commands.describe(link="A Google Drive folder or file link shared with 'Anyone with the link'", submitter="Who took these screenshots")
    @staff_only()
    async def drive(self, interaction: discord.Interaction, link: str, submitter: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            job = await api_of(interaction).drive_import(link.strip(), submitter=submitter)
        except ApiError as exc:
            await interaction.followup.send(f"Could not start the download: {exc.message}")
            return
        job_id = job.get("job_id")
        await interaction.followup.send(f"Queued job #{job_id}: downloading from Google Drive into the newuploads folder, then submitting the images. Follow it with `/rr job {job_id}`.")

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

    @rr.command(name="claims", description="List pending old-name claims")
    @staff_only()
    async def claims(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            claims = await api_of(interaction).list_claims("pending", limit=20)
        except ApiError as exc:
            await interaction.followup.send(f"Could not fetch claims: {exc.message}")
            return
        if not claims:
            await interaction.followup.send("No pending claims.")
            return
        lines = [
            f"#{c['claim_id']} **{discord.utils.escape_markdown(c.get('old_username', '?'))}** → **{discord.utils.escape_markdown(c.get('current_username', '?'))}** (`{c.get('uuid')}`) · rank on file: {c.get('effective_rank') or 'none'}"
            + (f" · by {c.get('discord_username')}" if c.get("discord_username") else "")
            + (" · ⚠️ conflicts with a verified UUID" if c.get("conflict_with") else "")
            for c in claims
        ]
        await interaction.followup.send("Pending claims (approve with `/rr approve <id>`, reject with `/rr reject <id> <reason>`):\n" + "\n".join(lines)[:1900])

    @rr.command(name="approve", description="Approve an old-name claim: the current account receives the old name's rank")
    @app_commands.describe(claim_id="Claim number from /rr claims", note="Optional note", force="Override an existing verified UUID (use with care)")
    @staff_only()
    async def approve(self, interaction: discord.Interaction, claim_id: int, note: str = "", force: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await api_of(interaction).decide_claim(claim_id, approve=True, note=note or f"approved by {interaction.user}", force=force)
        except ApiError as exc:
            await interaction.followup.send("No such claim." if exc.status == 404 else f"Could not approve: {exc.message}")
            return
        await interaction.followup.send(f"Approved claim #{claim_id}: **{discord.utils.escape_markdown(result.get('old_username', '?'))}** is now **{discord.utils.escape_markdown(result.get('current_username', '?'))}** (`{result.get('uuid')}`). The rank summary has been recomputed.")

    @rr.command(name="reject", description="Reject an old-name claim")
    @app_commands.describe(claim_id="Claim number from /rr claims", reason="Why it is rejected")
    @staff_only()
    async def reject(self, interaction: discord.Interaction, claim_id: int, reason: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await api_of(interaction).decide_claim(claim_id, approve=False, note=reason)
        except ApiError as exc:
            await interaction.followup.send("No such claim." if exc.status == 404 else f"Could not reject: {exc.message}")
            return
        await interaction.followup.send(f"Rejected claim #{claim_id}.")

    @rr.command(name="link-old", description="Staff: connect an old username to a player's current username right away")
    @app_commands.describe(old_name="Username in the old screenshots", current_name="The player's current Minecraft username", note="Optional note", force="Override an existing verified UUID (use with care)")
    @staff_only()
    async def link_old(self, interaction: discord.Interaction, old_name: str, current_name: str, note: str = "", force: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await api_of(interaction).link_names(old_name.strip(), current_name.strip(), note=note or f"linked by {interaction.user}", force=force)
        except ApiError as exc:
            if exc.status == 404:
                await interaction.followup.send(f"No evidence recorded for **{discord.utils.escape_markdown(old_name)}**.")
            else:
                await interaction.followup.send(f"Could not link: {exc.message}")
            return
        await interaction.followup.send(f"**{discord.utils.escape_markdown(old_name)}** → **{discord.utils.escape_markdown(result.get('current_username', current_name))}** (`{result.get('uuid')}`) linked and recomputed. `/rank {discord.utils.escape_markdown(result.get('current_username', current_name))}` now shows the recovered rank.")

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
