"""Discord buttons and forms for the manual UUID review (/rr unresolved)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import discord

from rankrecovery.bot.api_client import ApiError
from rankrecovery.bot.cogs import api_of, is_staff_interaction

log = logging.getLogger(__name__)
MAX_ITEMS = 5  # one row of buttons per name; Discord allows five rows


def namemc_link(name: str) -> str:
    return f"https://namemc.com/search?q={quote(name, safe='')}"


def unresolved_text(items: list[dict[str, Any]]) -> str:
    lines = []
    for i, item in enumerate(items, start=1):
        rank = item.get("effective_rank") or item.get("highest_observed") or "no rank yet"
        name = discord.utils.escape_markdown(str(item["name"]))
        line = f"**{i}. {name}** · {rank} · {item.get('images', 0)} screenshot(s) · <{namemc_link(str(item['name']))}>"
        others = [a for a in item.get("aliases") or [] if a != item["name"]]
        if others:
            line += f"\n   also seen as {discord.utils.escape_markdown(', '.join(others[:5]))}"
        if item.get("why"):
            line += f"\n   {discord.utils.escape_markdown(str(item['why'])[:160])}"
        for cand in (item.get("candidates") or [])[:4]:
            line += f"\n   candidate `{cand.get('uuid')}` (now {discord.utils.escape_markdown(str(cand.get('current_name') or '?'))})"
        lines.append(line)
    return "\n".join(lines)


def result_text(name: str, result: dict[str, Any]) -> str:
    safe = discord.utils.escape_markdown(name)
    now = result.get("current_name")
    text = f"✓ **{safe}** → `{result.get('uuid')}`"
    text += f", the account is now called **{discord.utils.escape_markdown(str(now))}**" if now else ", Mojang could not confirm a current name for that UUID"
    rank = result.get("effective_rank")
    text += f". Rank: **{rank}**" + (f" → {result['new_rank']}" if result.get("new_rank") else "") + "." if rank else ". No rank yet."
    if result.get("message"):
        text += f" ({result['message']})"
    return text


class UuidModal(discord.ui.Modal):
    uuid: discord.ui.TextInput = discord.ui.TextInput(label="UUID from NameMC", placeholder="069a79f4-44e9-4726-a5be-fca90e38aaf5", min_length=32, max_length=36)
    note: discord.ui.TextInput = discord.ui.TextInput(label="Note (optional)", required=False, max_length=100)

    def __init__(self, item: dict[str, Any]):
        super().__init__(title=f"Set UUID for {item['name']}"[:45])
        self.item = item

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not is_staff_interaction(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = str(self.note.value or "").strip() or f"set via Discord by {interaction.user}"
        try:
            result = await api_of(interaction).review_set_uuid(int(self.item["player_id"]), str(self.uuid.value).strip(), note=note)
        except ApiError as exc:
            await interaction.followup.send(f"Could not set the UUID for {discord.utils.escape_markdown(str(self.item['name']))}: {exc.message}", ephemeral=True)
            return
        await interaction.followup.send(result_text(str(self.item["name"]), result), ephemeral=True)


class SetUuidButton(discord.ui.Button):
    def __init__(self, item: dict[str, Any], row: int):
        super().__init__(label=f"Set UUID: {item['name']}"[:80], style=discord.ButtonStyle.primary, row=row, custom_id=f"rr:setuuid:{item['player_id']}")
        self.item = item

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not is_staff_interaction(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_modal(UuidModal(self.item))


class SkipButton(discord.ui.Button):
    def __init__(self, item: dict[str, Any], row: int):
        super().__init__(label=f"Skip: {item['name']}"[:80], style=discord.ButtonStyle.secondary, row=row, custom_id=f"rr:skipname:{item['player_id']}")
        self.item = item

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if not is_staff_interaction(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await api_of(interaction).review_skip(int(self.item["player_id"]), reason=f"skipped via Discord by {interaction.user}")
        except ApiError as exc:
            await interaction.followup.send(f"Could not skip {discord.utils.escape_markdown(str(self.item['name']))}: {exc.message}", ephemeral=True)
            return
        self.disabled = True
        try:
            await interaction.edit_original_response(view=self.view)
        except discord.HTTPException:  # pragma: no cover - the message may be gone
            pass
        await interaction.followup.send(f"Skipped **{discord.utils.escape_markdown(str(self.item['name']))}**; it stays out of the queue until someone un-skips it (`skip-name <name> --undo` in the console).", ephemeral=True)


class UnresolvedView(discord.ui.View):
    """One row per name: a button that opens the UUID form, and a skip button."""

    def __init__(self, items: list[dict[str, Any]], *, timeout: float | None = 900):
        super().__init__(timeout=timeout)
        self.items = items[:MAX_ITEMS]
        for row, item in enumerate(self.items):
            self.add_item(SetUuidButton(item, row))
            self.add_item(SkipButton(item, row))
