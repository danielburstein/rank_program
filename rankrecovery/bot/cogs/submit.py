"""Staff screenshot submissions."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from rankrecovery.bot.api_client import ApiError
from rankrecovery.bot.cogs import NotStaff, api_of, is_staff_interaction, settings_of, staff_only
from rankrecovery.bot.formatting import job_status_text, submission_summary

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".avif")
TERMINAL = {"completed", "failed", "cancelled"}


def is_image_attachment(attachment: discord.Attachment) -> bool:
    name = (attachment.filename or "").lower()
    ctype = (attachment.content_type or "").lower()
    return name.endswith(ALLOWED_EXTENSIONS) or ctype.startswith("image/")


class SubmitCog(commands.Cog, name="Submit"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._message_menu = app_commands.ContextMenu(name="Submit screenshots", callback=self._context_submit)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._message_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._message_menu.name, type=self._message_menu.type)

    # ----------------------------------------------------------------- helpers
    async def _default_submitter(self, interaction: discord.Interaction, user: discord.abc.User) -> str:
        try:
            link = await api_of(interaction).get_link(str(user.id))
        except ApiError:
            link = None
        if link and link.get("username"):
            return str(link["username"])
        return getattr(user, "display_name", None) or str(user)

    def _validate(self, interaction: discord.Interaction, attachments: list[discord.Attachment]) -> tuple[list[discord.Attachment], list[str]]:
        settings = settings_of(interaction)
        max_count = int(getattr(settings, "max_attachments_per_submit", 10))
        max_bytes = int(getattr(settings, "max_attachment_mb", 25)) * 1024 * 1024
        ok: list[discord.Attachment] = []
        problems: list[str] = []
        for a in attachments:
            if not is_image_attachment(a):
                problems.append(f"{a.filename}: not a supported image type")
            elif a.size and a.size > max_bytes:
                problems.append(f"{a.filename}: larger than {max_bytes // (1024 * 1024)} MB")
            else:
                ok.append(a)
        if len(ok) > max_count:
            problems.append(f"only the first {max_count} images were taken")
            ok = ok[:max_count]
        return ok, problems

    async def _run_submission(self, interaction: discord.Interaction, attachments: list[discord.Attachment], *, submitter: str, note: str, label: str) -> None:
        files: list[tuple[str, bytes]] = []
        read_errors: list[str] = []
        for a in attachments:
            try:
                files.append((a.filename, await a.read()))
            except discord.HTTPException as exc:
                read_errors.append(f"{a.filename}: download failed ({exc.status})")
        if not files:
            await interaction.followup.send("No usable screenshots were attached.\n" + "\n".join(read_errors))
            return
        try:
            result = await api_of(interaction).submit_images(files, submitter=submitter, discord_user_id=str(interaction.user.id), batch_label=label[:120])
        except ApiError as exc:
            log.warning("submission failed: %s", exc)
            await interaction.followup.send(f"Recorded submitter: **{discord.utils.escape_markdown(submitter)}**\nThe server rejected the upload: {exc.message if exc.status else 'server unreachable'}")
            return
        text = submission_summary(result, submitter=submitter)
        if note:
            text += f"\nNote: {discord.utils.escape_markdown(note)[:300]}"
        if read_errors:
            text += "\n" + "\n".join(read_errors)
        message = await interaction.followup.send(text, wait=True)
        job_id = result.get("job_id")
        if job_id:
            await self._poll_job(interaction, message, int(job_id), text)

    async def _poll_job(self, interaction: discord.Interaction, message: discord.Message, job_id: int, base_text: str) -> None:
        settings = settings_of(interaction)
        interval = float(getattr(settings, "job_poll_interval_seconds", 3.0))
        max_seconds = float(getattr(settings, "job_poll_max_seconds", 120.0))
        waited = 0.0
        last_status = ""
        while waited <= max_seconds:
            await asyncio.sleep(interval)
            waited += interval
            try:
                job = await api_of(interaction).job(job_id)
            except ApiError as exc:
                log.warning("job poll failed: %s", exc)
                break
            status_text = job_status_text(job)
            if status_text != last_status:
                last_status = status_text
                try:
                    await message.edit(content=f"{base_text}\n{status_text}")
                except discord.HTTPException:  # pragma: no cover
                    pass
            if job.get("status") in TERMINAL:
                return
        try:
            await message.edit(content=f"{base_text}\n{last_status or f'Job #{job_id} is still running'} · still processing; check `/rr job {job_id}` later.")
        except discord.HTTPException:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- commands
    @app_commands.command(name="submit", description="(staff) Submit rank screenshots for processing")
    @app_commands.describe(
        screenshot1="Screenshot (TAB list, chat or nametag)",
        screenshot2="Another screenshot",
        screenshot3="Another screenshot",
        screenshot4="Another screenshot",
        screenshot5="Another screenshot",
        submitter="Minecraft name of the player who PROVIDED these screenshots (defaults to your linked name)",
        note="Optional note stored with the batch",
    )
    @staff_only()
    async def submit(
        self,
        interaction: discord.Interaction,
        screenshot1: discord.Attachment,
        screenshot2: discord.Attachment | None = None,
        screenshot3: discord.Attachment | None = None,
        screenshot4: discord.Attachment | None = None,
        screenshot5: discord.Attachment | None = None,
        submitter: str | None = None,
        note: str | None = None,
    ) -> None:
        attachments = [a for a in (screenshot1, screenshot2, screenshot3, screenshot4, screenshot5) if a is not None]
        ok, problems = self._validate(interaction, attachments)
        if not ok:
            await interaction.response.send_message("No valid screenshots: " + "; ".join(problems), ephemeral=True)
            return
        await interaction.response.defer()
        submitter_name = (submitter or "").strip() or await self._default_submitter(interaction, interaction.user)
        label = f"discord /submit by {interaction.user} ({len(ok)} files)"
        if problems:
            await interaction.followup.send("Skipped: " + "; ".join(problems))
        await self._run_submission(interaction, ok, submitter=submitter_name, note=(note or "").strip(), label=label)

    async def _context_submit(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not is_staff_interaction(interaction):
            await interaction.response.send_message("Only staff can submit screenshots.", ephemeral=True)
            return
        attachments = [a for a in message.attachments if is_image_attachment(a)]
        ok, problems = self._validate(interaction, attachments)
        if not ok:
            await interaction.response.send_message("That message has no usable image attachments." + (" " + "; ".join(problems) if problems else ""), ephemeral=True)
            return
        await interaction.response.defer()
        submitter_name = await self._default_submitter(interaction, message.author)
        label = f"discord message {message.id} by {message.author} submitted by {interaction.user}"
        await self._run_submission(interaction, ok, submitter=submitter_name, note="", label=label)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        text = str(error) if isinstance(error, NotStaff) else "Something went wrong handling that command."
        if not isinstance(error, NotStaff):
            log.exception("submit command error", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:  # pragma: no cover
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SubmitCog(bot))
