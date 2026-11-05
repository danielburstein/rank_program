"""Pure formatting helpers: API dicts -> Discord embeds and text."""

from __future__ import annotations

from typing import Any

import discord

FIELD_LIMIT = 1024
MAX_FIELDS = 25
ELLIPSIS = "…"

COLOUR_CONFIRMED = 0x2ECC71
COLOUR_PROVISIONAL = 0xF1C40F
COLOUR_NONE = 0x95A5A6
COLOUR_ERROR = 0xE74C3C

_FRIENDLY_FLAGS: dict[str, str] = {
    "VERIFICATION_INSUFFICIENT_SOURCES": "Needs a second independent screenshot submitter for {details}",
    "AI_DETECTED_HIGHER_RANK": "A higher rank ({details}) only appears in a screenshot with an AI-generator indicator",
    "AI_DETECTED_FOR_ALL_RANK_IMAGES": "Every rank screenshot carries an AI-generator indicator",
    "OUT_OF_DATE": "A screenshot is dated after the cutoff",
    "DATE_UNKNOWN": "A screenshot has no reliable capture date",
    "LOW_OCR_CONFIDENCE": "Some text was read with low confidence",
    "AMBIGUOUS_PREFIX": "A rank prefix could not be matched with certainty",
    "PROBABLE_MISREAD": "A rank prefix may have been misread and awaits review",
    "MANUAL_REVIEW_REQUIRED": "Staff review is pending",
    "ONLY_STAFF_RANK_DETECTED": "Only a staff prefix was found, no player rank",
    "STAFF_AND_PLAYER_RANK_FOUND": "Staff prefix and player rank both found",
    "SUBMITTER_UNKNOWN": "A screenshot has no recorded submitter",
    "BEDROCK_PLAYER": "Bedrock player (needs a manual or Floodgate UUID)",
    "PARTIAL_USERNAME": "A username was cut off at the image edge",
    "UNREADABLE_USERNAME": "A username could not be read",
    "CONFLICTING_RANK_EVIDENCE": "Contradictory rank readings in one screenshot",
    "CSV_CONFLICT": "A purchase record conflicts with other identity data",
    "CSV_IDENTITY_CONFLICT": "Purchase records assert conflicting identities",
    "IDENTITY_CONFLICT": "UUID lookup conflicts with a trusted identity",
    "IDENTITY_AMBIGUOUS": "Identity could not be resolved to one account",
    "MANUAL_OVERRIDE": "A manual override is active",
    "NAME_READ_RANK_NOT_FOUND": "The name was seen but no rank prefix was read",
    "VERIFICATION_WAIVED": "The two-submitter requirement was waived by staff",
    "NO_RANK_EVIDENCE": "No eligible rank evidence yet",
}


def truncate(text: str, limit: int = FIELD_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + ELLIPSIS


def _join(items: list[str], *, sep: str = ", ", empty: str = "none") -> str:
    return truncate(sep.join(i for i in items if i)) if items else empty


def _provisional_lines(summary: dict[str, Any]) -> list[str]:
    lines = []
    for item in summary.get("provisional") or []:
        rank = item.get("rank", "?")
        counted = item.get("counted", 0)
        required = item.get("required", "?")
        fallback = item.get("fallback")
        line = f"{rank} {counted}/{required} sources"
        if fallback:
            line += f" (counts as {fallback} for now)"
        lines.append(line)
    return lines


def friendly_flag(flag: dict[str, Any]) -> str:
    code = str(flag.get("code", ""))
    details = str(flag.get("details", "") or "")
    template = _FRIENDLY_FLAGS.get(code)
    if template is None:
        return f"{code}" + (f" ({details})" if details else "")
    if "{details}" in template:
        return template.format(details=details or "a higher rank")
    return template


def rank_embed(detail: dict[str, Any], *, staff_view: bool) -> discord.Embed:
    player = detail.get("player") or {}
    summary = detail.get("summary") or {}
    conversion = detail.get("conversion") or None
    name = player.get("display_name") or "unknown player"
    effective = summary.get("effective_rank")
    provisional = _provisional_lines(summary)
    colour = COLOUR_CONFIRMED if effective and not provisional else (COLOUR_PROVISIONAL if provisional or effective else COLOUR_NONE)
    embed = discord.Embed(title=truncate(f"Rank summary for {name}", 256), colour=colour)

    fields: list[tuple[str, str, bool]] = []
    fields.append(("Effective old rank", effective or "none confirmed", True))
    fields.append(("Highest observed", summary.get("highest_observed") or "none", True))
    new_rank = conversion.get("new_rank") if conversion else None
    fields.append(("New rank", new_rank or "not mapped", True))
    fields.append(("All observed ranks", _join(list(summary.get("observed_ranks") or [])), False))
    staff_ranks = list(summary.get("staff_ranks") or [])
    if staff_ranks:
        fields.append(("Staff prefixes", _join(staff_ranks), True))
    custom = list(summary.get("custom_prefixes") or [])
    if custom:
        fields.append(("Custom prefixes", _join(custom), True))
    if provisional:
        fields.append(("Provisional (needs more sources)", _join(provisional, sep="\n"), False))
    evidence = (
        f"clean rank images: {summary.get('clean_rank_images', 0)}\n"
        f"independent submitters: {summary.get('distinct_submitters', 0)}\n"
        f"AI-flagged rank images: {summary.get('ai_rank_images', 0)}\n"
        f"trusted purchase records: {summary.get('csv_records', 0)}"
    )
    fields.append(("Evidence", evidence, False))
    uuid = player.get("uuid")
    identity = uuid or f"not resolved ({player.get('identity_status', 'unknown')})"
    if player.get("is_bedrock"):
        identity += " · Bedrock"
    fields.append(("Identity", truncate(identity), False))
    aliases = [a for a in (player.get("aliases") or []) if a and a != name]
    if aliases:
        fields.append(("Also known as", _join(aliases), False))
    if staff_view and conversion is not None:
        if conversion.get("included"):
            export_text = f"included as {new_rank or '?'}"
        else:
            reasons = list(conversion.get("reasons") or []) or ["not evaluated"]
            export_text = "excluded: " + "; ".join(reasons)
        fields.append(("Export eligibility", truncate(export_text), False))
    elif staff_view:
        fields.append(("Export eligibility", "conversion not computed yet", False))
    active_flags = [f for f in (detail.get("flags") or []) if f.get("is_active")]
    if active_flags:
        if staff_view:
            lines = [f"{f.get('code')}" + (f": {f.get('details')}" if f.get("details") else "") for f in active_flags]
        else:
            lines = sorted({friendly_flag(f) for f in active_flags})
        fields.append(("Active flags" if staff_view else "Notes", _join(lines, sep="\n"), False))
    for fname, value, inline in fields[:MAX_FIELDS]:
        embed.add_field(name=truncate(fname, 256), value=truncate(value or "-"), inline=inline)
    if summary.get("stale"):
        embed.set_footer(text="Rank summary is stale – a recompute is pending")
    elif summary.get("computed_at"):
        embed.set_footer(text=f"computed {str(summary['computed_at'])[:19].replace('T', ' ')} UTC")
    return embed


def not_found_text(username: str) -> str:
    return f"No evidence has been recorded for **{discord.utils.escape_markdown(username)}** yet. Staff can submit screenshots with `/submit`."


def submission_summary(result: dict[str, Any], *, submitter: str | None) -> str:
    outcomes = list(result.get("outcomes") or [])
    lines = [f"Recorded submitter: **{discord.utils.escape_markdown(submitter) if submitter else 'unknown'}**"]
    imported = sum(1 for o in outcomes if o.get("created"))
    duplicates = sum(1 for o in outcomes if o.get("image_id") and not o.get("created"))
    errors = sum(1 for o in outcomes if o.get("error"))
    lines.append(f"Files: {len(outcomes)} · imported {imported} · already known {duplicates} · errors {errors}")
    for o in outcomes[:15]:
        fname = discord.utils.escape_markdown(str(o.get("filename") or "file"))
        if o.get("error"):
            lines.append(f"• {fname}: error – {o['error']}")
            continue
        status = "imported" if o.get("created") else f"duplicate of image {o.get('duplicate_of')} (counts once)"
        notes = []
        if o.get("ai_status") in ("ai_indicator_found", "manually_flagged"):
            notes.append("AI indicator found")
        elif o.get("ai_status") == "ai_filename_indicator":
            notes.append("AI-like filename")
        if o.get("date_status") == "out_of_date":
            notes.append("dated after cutoff")
        elif o.get("date_status") == "unknown":
            notes.append("no reliable date")
        if o.get("near_duplicates"):
            notes.append("similar to an existing image")
        for w in o.get("warnings") or []:
            notes.append(str(w))
        lines.append(f"• {fname}: {status}" + (f" ({'; '.join(notes)})" if notes else ""))
    if len(outcomes) > 15:
        lines.append(f"… and {len(outcomes) - 15} more")
    job_id = result.get("job_id")
    lines.append(f"Extraction job: #{job_id}" if job_id else "No extraction job started (nothing new to process).")
    return truncate("\n".join(lines), 1900)


def job_status_text(job: dict[str, Any]) -> str:
    status = str(job.get("status", "?"))
    total = job.get("total") or 0
    done = job.get("done") or 0
    failed = job.get("failed") or 0
    parts = [f"Job #{job.get('id', '?')} ({job.get('kind', '?')}): **{status}**"]
    if total:
        parts.append(f"{done}/{total} done" + (f", {failed} failed" if failed else ""))
    elif done:
        parts.append(f"{done} done" + (f", {failed} failed" if failed else ""))
    if job.get("progress_message"):
        parts.append(str(job["progress_message"]))
    result = job.get("result") or {}
    if status == "completed" and result:
        summary = ", ".join(f"{k} {v}" for k, v in result.items() if isinstance(v, int | float | str))
        if summary:
            parts.append(f"result: {summary}")
    if job.get("error"):
        parts.append(f"error: {job['error']}")
    return truncate(" · ".join(parts), 1900)


def dashboard_text(dashboard: dict[str, Any], health: dict[str, Any] | None) -> str:
    lines = []
    if health:
        lines.append(f"API {health.get('status', '?')} · version {health.get('version', '?')} · vision {'configured' if health.get('vision_configured') else 'NOT configured (OCR fallback)'}")
    keys = [
        ("players", "players"),
        ("images", "images"),
        ("confirmed_players", "confirmed"),
        ("provisional_players", "provisional"),
        ("staff_only_players", "staff only"),
        ("bedrock_players", "bedrock"),
        ("unresolved_identities", "unresolved identities"),
        ("detections_needing_review", "detections to review"),
        ("exportable_players", "exportable"),
        ("trusted_csv_rows", "trusted purchase rows"),
        ("stale_summaries", "stale summaries"),
    ]
    lines.append(" · ".join(f"{label} {dashboard.get(key, 0)}" for key, label in keys))
    flag_counts = dashboard.get("flag_counts") or {}
    if flag_counts:
        top = sorted(flag_counts.items(), key=lambda kv: -kv[1])[:8]
        lines.append("flags: " + ", ".join(f"{code} {n}" for code, n in top))
    if dashboard.get("database"):
        lines.append(str(dashboard["database"]))
    return truncate("\n".join(lines), 1900)


def search_results_text(players: list[dict[str, Any]]) -> str:
    if not players:
        return "No players matched."
    lines = []
    for p in players[:10]:
        prov = ", ".join(f"{x.get('rank')} {x.get('counted')}/{x.get('required')}" for x in (p.get("provisional") or []))
        line = f"• **{discord.utils.escape_markdown(str(p.get('display_name')))}** – effective {p.get('effective_rank') or 'none'}, highest {p.get('highest_observed') or 'none'}"
        if prov:
            line += f", provisional {prov}"
        if p.get("staff_ranks"):
            line += f", staff {', '.join(p['staff_ranks'])}"
        lines.append(line)
    return truncate("\n".join(lines), 1900)
