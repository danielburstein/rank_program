"""Trusted purchase CSV import: preview, validate, commit."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from rankrecovery.core.enums import Credibility, IdentityStatus, SourceType
from rankrecovery.core.flags import FlagInstance
from rankrecovery.core.identity import normalise_uuid
from rankrecovery.core.usernames import normalise_username_key, validate_username
from rankrecovery.db.models import (
    CsvBatch,
    CsvRow,
    IdentityResolution,
    Player,
    PlayerSummary,
    RankObservation,
)
from rankrecovery.services.audit import audit
from rankrecovery.services.context import AppContext
from rankrecovery.services.flags import upsert_flags
from rankrecovery.services.players import (
    add_alias,
    find_player_by_username,
    find_player_by_uuid,
    get_or_create_player,
    resolve_survivor,
)

log = logging.getLogger(__name__)

FIELD_CANDIDATES: dict[str, list[str]] = {
    "username": ["username", "name", "player", "player_name", "playername", "ign", "minecraft_name", "mc_name", "user"],
    "uuid": ["uuid", "player_uuid", "minecraft_uuid", "mc_uuid", "id", "unique_id", "uniqueid"],
    "rank": ["rank", "old_rank", "oldrank", "package", "packages", "package_name", "product", "products", "group", "purchased_rank", "item", "items"],
    "purchase_date": ["purchase_date", "date", "purchased_at", "created_at", "time", "timestamp", "order_date"],
    "transaction_id": ["transaction_id", "transaction", "txn", "order_id", "orderid", "order", "payment_id", "id_transaction"],
    "status": ["status", "payment_status", "order_status", "state"],
}

DEFAULT_ACCEPTED_STATUSES = ["complete", "completed", "paid", "success", "successful", "captured"]

_PACKAGE_SPLIT_RE = re.compile(r"""['"]\s*,\s*['"]|;|\|""")
_RANK_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z+]*")


def rank_from_package_text(text: str, policy: Any, package_mapping: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Derive the purchased rank from a rank name or a store package name.

    Handles Tebex-style package lists ("'Galactic to Infinite', 'Fly perk'"):
    an explicit mapping wins, then an exact rank/alias, then rank words inside
    the package name, where "X to Y" upgrades yield Y and several ranks yield
    the highest. Returns (canonical rank or None, note).
    """
    raw = (text or "").strip()
    if not raw:
        return None, "missing rank"
    mapping = {str(k).strip().strip("'\"").lower(): str(v) for k, v in (package_mapping or {}).items()}
    parts = [p.strip().strip("'\"").strip() for p in _PACKAGE_SPLIT_RE.split(raw.strip("'\""))]
    parts = [p for p in parts if p]
    found: list[str] = []
    unknown: list[str] = []
    for part in parts:
        key = part.lower()
        if key in mapping:
            canonical = policy.canonical_rank(mapping[key])
            if canonical:
                found.append(canonical)
                continue
        direct = policy.canonical_rank(part)
        if direct:
            found.append(direct)
            continue
        tokens = _RANK_TOKEN_RE.findall(part)
        ranks_in_order = [(i, policy.canonical_rank(tok)) for i, tok in enumerate(tokens)]
        ranks_in_order = [(i, r) for i, r in ranks_in_order if r]
        if not ranks_in_order:
            unknown.append(part)
            continue
        lowered = [t.lower() for t in tokens]
        if "to" in lowered and len(ranks_in_order) >= 2:
            found.append(ranks_in_order[-1][1])  # upgrade: the target rank
        else:
            found.append(policy.max_rank([r for _, r in ranks_in_order]))
    if not found:
        return None, f"unknown rank/package {raw!r}"
    best = policy.max_rank(found)
    note = "" if not unknown else f"ignored non-rank package(s): {', '.join(unknown)}"
    return best, note


@dataclass(slots=True)
class PreviewRow:
    row_number: int
    raw: dict[str, Any]
    username: str | None
    uuid: str | None
    rank_raw: str
    rank_canonical: str | None
    purchase_date: str | None
    transaction_id: str | None
    row_hash: str
    status: str  # valid | invalid | duplicate_in_file | duplicate_existing
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CsvPreview:
    filename: str
    sha256: str
    encoding: str
    delimiter: str
    headers: list[str]
    mapping: dict[str, str | None]
    rows: list[PreviewRow]
    already_imported_batch_id: int | None = None

    @property
    def summary(self) -> dict[str, int]:
        counts = {"total": len(self.rows), "valid": 0, "invalid": 0, "duplicate_in_file": 0, "duplicate_existing": 0, "unknown_rank": 0}
        for r in self.rows:
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.rank_raw and r.rank_canonical is None:
                counts["unknown_rank"] += 1
        return counts


class CsvImportService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ---------------------------------------------------------------- parse
    @staticmethod
    def decode(content: bytes) -> tuple[str, str]:
        if content.startswith(b"\xef\xbb\xbf"):
            return content[3:].decode("utf-8", errors="replace"), "utf-8-sig"
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return content.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace"), "utf-8"

    @staticmethod
    def sniff_delimiter(text: str) -> str:
        sample = text[:4096]
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","

    @staticmethod
    def auto_map(headers: list[str], sample_rows: list[dict[str, Any]] | None = None) -> dict[str, str | None]:
        """Map fields to headers by name, then disambiguate by looking at the values.

        Store exports often have several candidate columns (a numeric package
        id next to the package names, an order id next to the UUID); the
        values decide: the rank column should contain words, the UUID column
        UUID-shaped strings, the username column no e-mail addresses.
        """
        rows = sample_rows or []
        lowered = {h.strip().lower().replace(" ", "_"): h for h in headers}
        mapping: dict[str, str | None] = {k: None for k in FIELD_CANDIDATES}
        taken: set[str] = set()

        def values(header: str) -> list[str]:
            return [str(r.get(header) or "").strip() for r in rows if str(r.get(header) or "").strip()]

        def fraction(header: str, test: Any) -> float:
            vals = values(header)
            return sum(1 for v in vals if test(v)) / len(vals) if vals else 0.0

        hints = {"rank": ("package", "product", "rank", "item"), "uuid": ("uuid",), "username": ("username", "player", "ign"), "purchase_date": ("date",), "transaction_id": ("transaction", "order"), "status": ("status",)}
        for field_name, candidates in FIELD_CANDIDATES.items():
            options: list[str] = []
            for cand in candidates:
                if cand in lowered and lowered[cand] not in options:
                    options.append(lowered[cand])
            for key, header in lowered.items():
                if any(h in key for h in hints[field_name]) and header not in options:
                    options.append(header)
            options = [o for o in options if o not in taken]
            if not options:
                continue
            best = options[0]
            if rows:
                def score(header: str, field_name: str = field_name) -> float:
                    key = header.strip().lower()
                    if field_name == "rank":
                        return fraction(header, lambda v: any(c.isalpha() for c in v)) + (0.2 if "name" in key else 0.0)
                    if field_name == "uuid":
                        return fraction(header, lambda v: normalise_uuid(v) is not None)
                    if field_name == "username":
                        return fraction(header, lambda v: "@" not in v and not v.replace(".", "").isdigit() and " " not in v)
                    if field_name == "purchase_date":
                        return fraction(header, lambda v: bool(re.match(r"\d{4}-\d{2}-\d{2}", v)))
                    if field_name == "transaction_id":
                        return fraction(header, lambda v: not v.isdigit()) + (0.5 if "transaction" in key else 0.0)
                    return 1.0 if key == "status" else 0.5

                scored = sorted(options, key=score, reverse=True)
                best = scored[0]
                if field_name in ("rank", "uuid") and score(best) < 0.3:
                    best = options[0] if field_name == "rank" else best
            mapping[field_name] = best
            taken.add(best)
        return mapping

    def preview(self, content: bytes, *, filename: str, mapping: dict[str, str | None] | None = None, delimiter: str | None = None) -> CsvPreview:
        text, encoding = self.decode(content)
        delimiter = delimiter or self.sniff_delimiter(text)
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headers = [h for h in (reader.fieldnames or []) if h is not None]
        raw_rows = [{k: (v or "").strip() for k, v in r.items() if k is not None} for r in reader]
        mapping = mapping or self.auto_map(headers, raw_rows[:200])
        policy = self.ctx.policy()
        sha = hashlib.sha256(content).hexdigest()
        with self.ctx.db.read_session() as s:
            existing_batch = s.execute(select(CsvBatch).where(CsvBatch.sha256 == sha)).scalars().first()
            existing_hashes = {row.row_hash: row.id for row in s.execute(select(CsvRow).where(CsvRow.is_active.is_(True))).scalars()}
        rows: list[PreviewRow] = []
        seen_in_file: set[str] = set()
        for number, raw in enumerate(raw_rows, start=2):
            pr = self._validate_row(number, raw, mapping, policy)
            if pr.status == "valid":
                if pr.row_hash in seen_in_file:
                    pr.status = "duplicate_in_file"
                elif pr.row_hash in existing_hashes:
                    pr.status = "duplicate_existing"
                    pr.warnings.append(f"identical purchase row already imported (csv row {existing_hashes[pr.row_hash]})")
                seen_in_file.add(pr.row_hash)
            rows.append(pr)
        self._flag_identity_conflicts(rows)
        return CsvPreview(filename=filename, sha256=sha, encoding=encoding, delimiter=delimiter, headers=headers, mapping=mapping, rows=rows, already_imported_batch_id=existing_batch.id if existing_batch else None)

    def _validate_row(self, number: int, raw: dict[str, Any], mapping: dict[str, str | None], policy: Any) -> PreviewRow:
        def get(field_name: str) -> str:
            col = mapping.get(field_name)
            return str(raw.get(col, "")).strip() if col else ""

        errors: list[str] = []
        warnings: list[str] = []
        username = get("username") or None
        uuid_raw = get("uuid") or None
        uuid = normalise_uuid(uuid_raw) if uuid_raw else None
        if uuid_raw and uuid is None:
            errors.append(f"invalid UUID {uuid_raw!r}")
        if username:
            check = validate_username(username, bedrock_prefix=str(self.ctx.settings.get("lookup.bedrock_prefix", ".")))
            if not check.valid:
                errors.append(f"invalid username {username!r} ({check.reason})")
            username = check.normalised or username
        if not username and not uuid:
            errors.append("row has neither username nor UUID")
        status_raw = get("status")
        if status_raw:
            accepted = [str(x).lower() for x in (self.ctx.settings.get("csv.accepted_statuses") or DEFAULT_ACCEPTED_STATUSES)]
            if status_raw.lower() not in accepted:
                errors.append(f"payment status {status_raw!r} is not a completed payment")
        rank_raw = get("rank")
        rank_canonical, rank_note = rank_from_package_text(rank_raw, policy, self.ctx.settings.get("csv.package_rank_mapping") or {}) if rank_raw else (None, "missing rank")
        if rank_canonical is None:
            errors.append(rank_note or f"unknown rank {rank_raw!r}")
        elif rank_note:
            warnings.append(rank_note)
        purchase_date = get("purchase_date") or None
        transaction_id = get("transaction_id") or None
        payload = json.dumps({"u": (username or "").lower(), "id": uuid or "", "r": rank_canonical or rank_raw.upper(), "d": purchase_date or "", "t": transaction_id or ""}, sort_keys=True)
        row_hash = hashlib.sha256(payload.encode()).hexdigest()
        return PreviewRow(number, raw, username, uuid, rank_raw, rank_canonical, purchase_date, transaction_id, row_hash, "invalid" if errors else "valid", errors, warnings)

    @staticmethod
    def _flag_identity_conflicts(rows: list[PreviewRow]) -> None:
        by_name: dict[str, set[str]] = {}
        by_uuid: dict[str, set[str]] = {}
        for r in rows:
            if r.username and r.uuid:
                by_name.setdefault(r.username.lower(), set()).add(r.uuid)
                by_uuid.setdefault(r.uuid, set()).add(r.username.lower())
        for r in rows:
            if r.username and r.uuid:
                if len(by_name[r.username.lower()]) > 1:
                    r.warnings.append(f"username {r.username} maps to several UUIDs in this file")
                if len(by_uuid[r.uuid]) > 1:
                    r.warnings.append(f"UUID {r.uuid} carries several usernames in this file (name changes are possible)")

    # --------------------------------------------------------------- commit
    def commit(self, content: bytes, *, filename: str, mapping: dict[str, str | None] | None = None, delimiter: str | None = None, reject_invalid: bool = True, actor: str = "", notes: str = "", source: str = "file") -> dict[str, Any]:
        preview = self.preview(content, filename=filename, mapping=mapping, delimiter=delimiter)
        if preview.already_imported_batch_id is not None:
            return {"batch_id": preview.already_imported_batch_id, "status": "already_imported", "summary": preview.summary, "hint": "retire that batch first (console: csv-retire <id>) to import this file again"}
        if not reject_invalid and any(r.status == "invalid" for r in preview.rows):
            return {"batch_id": None, "status": "rejected", "summary": preview.summary, "errors": [{"row": r.row_number, "errors": r.errors} for r in preview.rows if r.status == "invalid"]}
        policy = self.ctx.policy()
        touched: set[int] = set()
        with self.ctx.db.write_session() as s:
            batch = CsvBatch(filename=filename, sha256=preview.sha256, delimiter=preview.delimiter, encoding=preview.encoding, header_mapping=preview.mapping, row_count=len(preview.rows), imported_by=actor, notes=notes, source=source)
            s.add(batch)
            s.flush()
            existing_hashes = {row.row_hash: row.id for row in s.execute(select(CsvRow).where(CsvRow.is_active.is_(True))).scalars()}
            for pr in preview.rows:
                status = "valid"
                dup_of = None
                if pr.status == "invalid":
                    status = "invalid"
                elif pr.status in ("duplicate_in_file", "duplicate_existing"):
                    status = "duplicate"
                    dup_of = existing_hashes.get(pr.row_hash)
                row = CsvRow(
                    batch_id=batch.id,
                    row_number=pr.row_number,
                    raw_row=pr.raw,
                    row_hash=pr.row_hash,
                    username=pr.username,
                    username_key=normalise_username_key(pr.username) if pr.username else None,
                    uuid=pr.uuid,
                    rank_raw=pr.rank_raw,
                    rank_canonical=pr.rank_canonical,
                    purchase_date=pr.purchase_date,
                    transaction_id=pr.transaction_id,
                    validation_status=status,
                    validation_errors=pr.errors + pr.warnings,
                    duplicate_of_id=dup_of,
                    is_active=status == "valid",
                    deactivated_reason=None if status == "valid" else status,
                )
                s.add(row)
                s.flush()
                if status == "valid":
                    existing_hashes[pr.row_hash] = row.id
                    batch.valid_count += 1
                    player = self._link_row(s, row, policy, actor)
                    if player is not None:
                        touched.add(player.id)
                elif status == "invalid":
                    batch.rejected_count += 1
                else:
                    batch.duplicate_count += 1
            for pid in touched:
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if summary is not None:
                    summary.stale = True
            audit(s, actor=actor, action="csv.import", entity_type="csv_batch", entity_id=batch.id, after={"filename": filename, "valid": batch.valid_count, "rejected": batch.rejected_count, "duplicates": batch.duplicate_count})
            return {"batch_id": batch.id, "status": "imported", "summary": preview.summary, "players_touched": sorted(touched)}

    def _link_row(self, s: Any, row: CsvRow, policy: Any, actor: str) -> Player | None:
        """Attach a valid row to a player and create its authoritative observation."""
        player: Player | None = None
        conflict: str | None = None
        if row.uuid:
            by_uuid = find_player_by_uuid(s, row.uuid)
            by_name = find_player_by_username(s, row.username) if row.username else None
            if by_uuid is not None and by_name is not None and by_uuid.id != by_name.id:
                if by_name.canonical_uuid and by_name.canonical_uuid != row.uuid:
                    conflict = f"username {row.username} is linked to UUID {by_name.canonical_uuid} but CSV asserts {row.uuid}"
                    player = by_uuid
                else:
                    # the name is new to this UUID: record alias on the UUID's player, keep the name's player for review
                    player = by_uuid
                    conflict = f"username {row.username} already exists as a separate player ({by_name.id}); CSV links it to UUID {row.uuid}"
            elif by_uuid is not None:
                player = by_uuid
            elif by_name is not None:
                if by_name.canonical_uuid and by_name.canonical_uuid != row.uuid:
                    conflict = f"CSV UUID {row.uuid} differs from verified UUID {by_name.canonical_uuid} for {row.username}"
                player = by_name
            else:
                player, _ = get_or_create_player(s, row.username or row.uuid, source="csv")
            if row.username:
                add_alias(s, player, row.username, source="csv")
            if conflict is None and player.canonical_uuid in (None, row.uuid):
                if player.canonical_uuid is None:
                    player.canonical_uuid = row.uuid
                    player.identity_status = IdentityStatus.VERIFIED.value
                    player.identity_source = "csv"
                    for old in s.execute(select(IdentityResolution).where(IdentityResolution.player_id == player.id, IdentityResolution.is_active.is_(True))).scalars():
                        old.is_active = False
                    s.add(IdentityResolution(player_id=player.id, uuid=row.uuid, status=IdentityStatus.VERIFIED.value, source="csv", queried_name=row.username, csv_row_id=row.id, note="trusted purchase CSV"))
        elif row.username:
            player, _ = get_or_create_player(s, row.username, source="csv")
        if player is None:
            return None
        player = resolve_survivor(s, player)
        row.player_id = player.id
        if conflict:
            upsert_flags(s, "player", player.id, [FlagInstance("CSV_IDENTITY_CONFLICT", conflict, evidence_ref=f"csv_row:{row.id}")], actor=actor, source="manual")
            upsert_flags(s, "csv_row", row.id, [FlagInstance("CSV_IDENTITY_CONFLICT", conflict)], actor=actor, source="manual")
        s.add(
            RankObservation(
                player_id=player.id,
                csv_row_id=row.id,
                source_type=SourceType.TRUSTED_CSV.value,
                basis="csv",
                raw_text=row.rank_raw[:128],
                rank_canonical=row.rank_canonical or "",
                rank_priority=policy.priority(row.rank_canonical),
                credibility=Credibility.AUTHORITATIVE.value,
                confidence=1.0,
                review_status="not_required",
                created_by=actor,
            )
        )
        return player

    # ------------------------------------------------------------- manual row
    def add_manual_row(self, *, username: str | None, uuid: str | None, rank: str, purchase_date: str | None, transaction_id: str | None, actor: str, reason: str) -> dict[str, Any]:
        raw = {"username": username or "", "uuid": uuid or "", "rank": rank, "purchase_date": purchase_date or "", "transaction_id": transaction_id or "", "manual_reason": reason}
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(raw))
        writer.writeheader()
        writer.writerow(raw)
        content = buffer.getvalue().encode()
        # make the batch hash unique per manual entry so re-adding is explicit
        content += f"\n# manual {actor} {hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()}\n".encode()
        return self.commit(content, filename=f"manual-{actor}.csv", actor=actor, notes=reason, source="manual")

    def retire_batch(self, batch_id: int, *, actor: str, reason: str) -> dict[str, int]:
        """Retire a whole import batch so its file can be imported again (rows and evidence kept, inactive)."""
        with self.ctx.db.write_session() as s:
            batch = s.get(CsvBatch, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            rows = 0
            touched: set[int] = set()
            for row in batch.rows:
                if row.is_active:
                    row.is_active = False
                    row.deactivated_reason = f"batch retired: {reason}"
                    rows += 1
                for obs in s.execute(select(RankObservation).where(RankObservation.csv_row_id == row.id, RankObservation.is_active.is_(True))).scalars():
                    obs.is_active = False
                    obs.deactivated_reason = f"batch retired: {reason}"
                if row.player_id:
                    touched.add(row.player_id)
            batch.is_active = False
            if not batch.sha256.startswith("retired-"):
                batch.sha256 = f"retired-{batch.id}-{batch.sha256}"[:64]
            for pid in touched:
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == pid)).scalars().first()
                if summary:
                    summary.stale = True
            audit(s, actor=actor, action="csv.batch_retired", entity_type="csv_batch", entity_id=batch_id, note=reason, after={"rows": rows})
            return {"rows_retired": rows, "players_touched": len(touched)}

    def deactivate_row(self, row_id: int, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            row = s.get(CsvRow, row_id)
            if row is None:
                raise KeyError(row_id)
            row.is_active = False
            row.deactivated_reason = reason
            for obs in s.execute(select(RankObservation).where(RankObservation.csv_row_id == row_id)).scalars():
                obs.is_active = False
                obs.deactivated_reason = reason
            if row.player_id:
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == row.player_id)).scalars().first()
                if summary:
                    summary.stale = True
            audit(s, actor=actor, action="csv_row.deactivate", entity_type="csv_row", entity_id=row_id, note=reason)

    def reactivate_row(self, row_id: int, *, actor: str, reason: str) -> None:
        with self.ctx.db.write_session() as s:
            row = s.get(CsvRow, row_id)
            if row is None:
                raise KeyError(row_id)
            if row.validation_status != "valid":
                raise ValueError("only valid rows can be reactivated")
            row.is_active = True
            row.deactivated_reason = None
            for obs in s.execute(select(RankObservation).where(RankObservation.csv_row_id == row_id)).scalars():
                obs.is_active = True
                obs.deactivated_reason = None
            if row.player_id:
                summary = s.execute(select(PlayerSummary).where(PlayerSummary.player_id == row.player_id)).scalars().first()
                if summary:
                    summary.stale = True
            audit(s, actor=actor, action="csv_row.reactivate", entity_type="csv_row", entity_id=row_id, note=reason)
