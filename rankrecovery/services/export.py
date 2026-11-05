"""Mode 4: LuckPerms command export with preview, history and stale tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from rankrecovery.core.export import DEFAULT_COMMAND_TEMPLATE, ExportCandidate, ExportResult, build_export
from rankrecovery.db.models import Conversion, ExportBatch, ExportRow, Player, PlayerSummary, StaffObservation
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext


class ExportService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def _candidates(self) -> tuple[list[ExportCandidate], list[str]]:
        policy = self.ctx.policy()
        warnings: list[str] = []
        with self.ctx.db.read_session() as s:
            rows = s.execute(select(Player, Conversion).join(Conversion, Conversion.player_id == Player.id).where(Player.merged_into_id.is_(None))).all()
            staff_players = set(s.execute(select(StaffObservation.player_id).where(StaffObservation.is_active.is_(True))).scalars().all())
            stale_summaries = set(s.execute(select(PlayerSummary.player_id).where(PlayerSummary.stale.is_(True))).scalars().all())
            cands: list[ExportCandidate] = []
            for p, c in rows:
                reasons = list(c.reasons)
                included = c.included
                if c.stale:
                    included = False
                    reasons.append("conversion is stale; rerun Mode 2")
                if p.id in stale_summaries:
                    included = False
                    reasons.append("rank summary is stale; rerun Mode 1")
                cands.append(ExportCandidate(p.id, p.canonical_uuid, c.old_rank, c.new_rank, p.identity_status, included, reasons, has_staff_evidence=p.id in staff_players, display_name=p.display_name))
            cands.sort(key=lambda c: (-policy.priority(c.old_rank), c.player_id))
            staff_included = [c.display_name for c in cands if c.conversion_included and c.has_staff_evidence]
            if staff_included:
                warnings.append(
                    "LuckPerms 'parent set' REPLACES existing parent groups. These players have staff evidence and would lose staff groups if the commands run unchanged: "
                    + ", ".join(sorted(staff_included))
                )
        return cands, warnings

    def preview(self) -> dict[str, Any]:
        template = str(self.ctx.settings.get("export.command_template", DEFAULT_COMMAND_TEMPLATE))
        cands, warnings = self._candidates()
        result = build_export(cands, template=template)
        return {
            "command_count": len(result.commands),
            "skipped_count": len(result.skipped),
            "warnings": warnings,
            "commands": result.commands,
            "included": [self._row_dict(r) for r in result.included],
            "skipped": [self._row_dict(r) for r in result.skipped],
            "stale_exports": self.stale_export_ids(),
        }

    @staticmethod
    def _row_dict(r: Any) -> dict[str, Any]:
        return {"player_id": r.player_id, "display_name": r.display_name, "uuid": r.uuid, "old_rank": r.old_rank, "new_rank": r.new_rank, "command": r.command, "included": r.included, "skip_reason": r.skip_reason}

    def write(self, *, path: str | Path | None, actor: str, staff_warning_acknowledged: bool, options: dict[str, Any] | None = None) -> dict[str, Any]:
        template = str(self.ctx.settings.get("export.command_template", DEFAULT_COMMAND_TEMPLATE))
        cands, warnings = self._candidates()
        if warnings and not staff_warning_acknowledged:
            raise PermissionError("staff warning must be acknowledged before export: " + " | ".join(warnings))
        result: ExportResult = build_export(cands, template=template)
        text = result.text()
        filename = Path(path).name if path else str(self.ctx.settings.get("export.default_filename", "luckperms_commands.txt"))
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8", newline="\n")
        with self.ctx.db.write_session() as s:
            batch = ExportBatch(
                created_by=actor,
                filename=filename,
                file_path=str(Path(path).resolve()) if path else "",
                options={"template": template, **(options or {})},
                command_count=len(result.commands),
                skipped_count=len(result.skipped),
                policy_fingerprint=self.ctx.policy().fingerprint(),
                mapping_fingerprint=self.ctx.mapping().fingerprint(),
                staff_warning_acknowledged=staff_warning_acknowledged,
                content=text,
            )
            s.add(batch)
            s.flush()
            for r in result.rows:
                s.add(ExportRow(batch_id=batch.id, player_id=r.player_id, uuid=r.uuid, old_rank=r.old_rank, new_rank=r.new_rank, command=r.command, included=r.included, skip_reason=r.skip_reason))
            audit(s, actor=actor, action="export.write", entity_type="export_batch", entity_id=batch.id, after={"commands": len(result.commands), "skipped": len(result.skipped), "file": filename})
            batch_id = batch.id
        return {"batch_id": batch_id, "command_count": len(result.commands), "skipped_count": len(result.skipped), "warnings": warnings, "text": text, "filename": filename}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.ctx.db.read_session() as s:
            out = []
            for b in s.execute(select(ExportBatch).order_by(ExportBatch.id.desc()).limit(limit)).scalars():
                out.append({"id": b.id, "created_at": b.created_at.isoformat() if b.created_at else None, "created_by": b.created_by, "filename": b.filename, "file_path": b.file_path, "command_count": b.command_count, "skipped_count": b.skipped_count, "stale": b.stale, "stale_reason": b.stale_reason, "policy_fingerprint": b.policy_fingerprint, "mapping_fingerprint": b.mapping_fingerprint})
            return out

    def batch_detail(self, batch_id: int) -> dict[str, Any] | None:
        with self.ctx.db.read_session() as s:
            b = s.get(ExportBatch, batch_id)
            if b is None:
                return None
            return {"id": b.id, "content": b.content, "rows": [{"player_id": r.player_id, "uuid": r.uuid, "old_rank": r.old_rank, "new_rank": r.new_rank, "command": r.command, "included": r.included, "skip_reason": r.skip_reason} for r in b.rows], "stale": b.stale, "stale_reason": b.stale_reason, "options": b.options}

    def stale_export_ids(self) -> list[int]:
        with self.ctx.db.read_session() as s:
            return list(s.execute(select(ExportBatch.id).where(ExportBatch.stale.is_(True))).scalars().all())

    def mark_stale(self, reason: str) -> int:
        with self.ctx.db.write_session() as s:
            n = 0
            for b in s.execute(select(ExportBatch).where(ExportBatch.stale.is_(False))).scalars():
                b.stale = True
                b.stale_reason = reason
                b_at = datetime.now(UTC)
                _ = b_at
                n += 1
            return n
