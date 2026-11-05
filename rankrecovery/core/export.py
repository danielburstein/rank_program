"""LuckPerms command generation."""

from __future__ import annotations

from dataclasses import dataclass, field

from rankrecovery.core.identity import normalise_uuid

DEFAULT_COMMAND_TEMPLATE = "lp user {uuid} parent set {group}"


@dataclass(frozen=True, slots=True)
class ExportCandidate:
    player_id: int
    uuid: str | None
    old_rank: str | None
    new_rank: str | None
    identity_status: str
    conversion_included: bool
    conversion_reasons: list[str] = field(default_factory=list)
    has_staff_evidence: bool = False
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ExportRow:
    player_id: int
    uuid: str | None
    old_rank: str | None
    new_rank: str | None
    command: str | None
    included: bool
    skip_reason: str = ""
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ExportResult:
    rows: list[ExportRow]

    @property
    def commands(self) -> list[str]:
        return [r.command for r in self.rows if r.included and r.command]

    @property
    def included(self) -> list[ExportRow]:
        return [r for r in self.rows if r.included]

    @property
    def skipped(self) -> list[ExportRow]:
        return [r for r in self.rows if not r.included]

    def text(self) -> str:
        return "\n".join(self.commands) + ("\n" if self.commands else "")


def render_command(template: str, uuid: str, group: str) -> str:
    command = template.format(uuid=uuid, group=group)
    # Defensive: the spec forbids quotes, placeholders and a leading slash.
    command = command.replace('"', "").replace("'", "").replace("%", "").lstrip("/")
    return " ".join(command.split())


def build_export(
    candidates: list[ExportCandidate],
    *,
    template: str = DEFAULT_COMMAND_TEMPLATE,
    verified_identity_statuses: frozenset[str] = frozenset({"verified", "manual"}),
) -> ExportResult:
    """Produce at most one command per canonical UUID.

    When two candidates share a UUID (should not happen after merging, but is
    guarded against), the one with the higher old-rank priority is kept; the
    caller passes candidates ordered by descending priority.
    """
    rows: list[ExportRow] = []
    seen: set[str] = set()
    for c in candidates:
        uuid = normalise_uuid(c.uuid)
        if c.identity_status not in verified_identity_statuses:
            rows.append(ExportRow(c.player_id, c.uuid, c.old_rank, c.new_rank, None, False, f"identity not resolved ({c.identity_status})", c.display_name))
            continue
        if uuid is None:
            rows.append(ExportRow(c.player_id, c.uuid, c.old_rank, c.new_rank, None, False, "invalid or missing UUID", c.display_name))
            continue
        if not c.conversion_included:
            reason = "; ".join(c.conversion_reasons) or "excluded by conversion policy"
            rows.append(ExportRow(c.player_id, uuid, c.old_rank, c.new_rank, None, False, reason, c.display_name))
            continue
        if not c.new_rank:
            rows.append(ExportRow(c.player_id, uuid, c.old_rank, None, None, False, "no mapped new rank", c.display_name))
            continue
        if uuid in seen:
            rows.append(ExportRow(c.player_id, uuid, c.old_rank, c.new_rank, None, False, "duplicate UUID already exported with a higher or equal rank", c.display_name))
            continue
        seen.add(uuid)
        rows.append(ExportRow(c.player_id, uuid, c.old_rank, c.new_rank, render_command(template, uuid, c.new_rank), True, "", c.display_name))
    return ExportResult(rows)
