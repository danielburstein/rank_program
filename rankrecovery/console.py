"""Interactive admin console on stdin for panel hosts.

Pterodactyl-style panels forward whatever is typed in the console box to
the process's standard input. This module reads those lines and runs
admin commands directly against the application context, so the panel
console becomes a debugging tool without SSH or HTTP calls.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
import threading
from collections.abc import Callable
from typing import Any

from rankrecovery.services.context import AppContext

log = logging.getLogger("rankrecovery.console")

Handler = Callable[[list[str]], str]


def _mask(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "(not set)"
    return f"{text[:6]}…{text[-4:]} ({len(text)} chars)" if len(text) > 12 else "set"


class AdminConsole:
    def __init__(self, ctx: AppContext, runner: Any, *, stop: Callable[[], None] | None = None, out: Callable[[str], None] | None = None):
        self.ctx = ctx
        self.runner = runner
        self._stop = stop
        self._out = out or (lambda text: print(text, flush=True))
        self.commands: dict[str, tuple[Handler, str]] = {
            "help": (self.cmd_help, "list commands"),
            "status": (self.cmd_status, "dashboard counts and configuration state"),
            "env": (self.cmd_env, "show which secrets/settings are configured (masked)"),
            "jobs": (self.cmd_jobs, "jobs [n]: recent jobs"),
            "job": (self.cmd_job, "job <id>: one job with its errors"),
            "cancel": (self.cmd_cancel, "cancel <job id>"),
            "extract": (self.cmd_extract, "extract [--force] [vision|ocr]: queue extraction of pending (or all) images"),
            "recompute": (self.cmd_recompute, "recompute [all]: recompute stale (or all) player summaries"),
            "reparse": (self.cmd_reparse, "re-classify stored readings under the current rank list (after alias/rank changes), then recompute"),
            "alias": (self.cmd_alias, "alias add <RANK> <ALIAS> | alias remove <RANK> <ALIAS> | alias list: seasonal rank aliases (GLOBAL -> HERO)"),
            "staff-alias": (self.cmd_staff_alias, "staff-alias add <STAFF> <ALIAS> | staff-alias remove <STAFF> <ALIAS>"),
            "convert": (self.cmd_convert, "run Mode 2 conversion"),
            "lookup": (self.cmd_lookup, "lookup [--retry-failed] [--force]: queue UUID lookups for every player without a UUID (Mojang first, then the last holder of the name; --retry-failed: only names whose last lookup failed; --force ignores cached answers)"),
            "ingest": (self.cmd_ingest, "ingest [--submitter <name>] [--no-extract]: submit every image dropped into the newuploads folder, then remove it from the folder"),
            "drive": (self.cmd_drive, "drive <google drive link> [--submitter <name>] [--no-ingest]: download the photos behind a shared Drive folder/file link into newuploads and submit them"),
            "players": (self.cmd_players, "players [search]: list players"),
            "identity-import": (self.cmd_identity_import, "identity-import <file> [--create]: import username/UUID pairs from usercache.json or a CSV (old server records)"),
            "names": (self.cmd_names, "names [--force]: fetch current Mojang names for players with a known UUID"),
            "claims": (self.cmd_claims, "claims: pending old-name claims from Discord"),
            "claim": (self.cmd_claim, "claim approve <id> [note] | claim reject <id> <reason>"),
            "link-old": (self.cmd_link_old, "link-old <old name> <current name> [--force]: give the current account the old name's rank"),
            "lookups": (self.cmd_lookups, "lookups <name> [n]: recent UUID lookup attempts for a player, with status, failure code and the raw response excerpt"),
            "player": (self.cmd_player, "player <name>: full summary for one player"),
            "images": (self.cmd_images, "images [n]: recent images"),
            "image": (self.cmd_image, "image <id>: detections and runs for one image"),
            "vision": (self.cmd_vision, "vision test | vision model <id>: check the Anthropic key / change the model"),
            "settings": (self.cmd_settings, "settings get <key> | settings set <key> <json> | settings list [prefix]"),
            "keys": (self.cmd_keys, "keys: list API keys"),
            "key": (self.cmd_key, "key create <name> <admin|staff|bot|readonly> | key revoke <id>"),
            "export": (self.cmd_export, "export preview: command count and skip reasons"),
            "uuid-ranks": (self.cmd_uuid_ranks, "uuid-ranks [rebuild|list [n]|csv <file>]: the uuid_ranks table, one row per resolved UUID with its highest rank (rebuilt after every job)"),
            "csv-batches": (self.cmd_csv_batches, "list trusted-CSV import batches"),
            "csv-retire": (self.cmd_csv_retire, "csv-retire <batch id>: retire a batch so its file can be imported again"),
            "package-map": (self.cmd_package_map, "package-map add \"<package name>\" <RANK> | remove \"<package name>\" | list: store packages that do not name their rank"),
            "backup": (self.cmd_backup, "backup: write a database backup into backups/"),
            "logs": (self.cmd_logs, "logs [n] [level]: recent log records"),
            "stop": (self.cmd_stop, "stop the server"),
        }

    # ---------------------------------------------------------------- loop
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="admin-console", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:
        stream = sys.stdin
        if stream is None or stream.closed:
            return
        self._out("Admin console ready. Type 'help' for commands.")
        while True:
            try:
                line = stream.readline()
            except Exception:  # pragma: no cover - stdin went away
                return
            if line == "":
                return  # EOF: no console attached
            line = line.strip()
            if not line:
                continue
            self._out(self.run(line))

    # ------------------------------------------------------------ dispatch
    def run(self, line: str) -> str:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            return f"could not parse command: {exc}"
        if not parts:
            return ""
        name, args = parts[0].lower().lstrip("/"), parts[1:]
        entry = self.commands.get(name)
        if entry is None:
            return f"unknown command '{name}'. Type 'help'."
        try:
            return entry[0](args)
        except Exception as exc:
            log.exception("console command failed: %s", line)
            return f"error: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ commands
    def cmd_help(self, args: list[str]) -> str:
        width = max(len(k) for k in self.commands)
        return "\n".join(f"  {name.ljust(width)}  {desc}" for name, (_, desc) in self.commands.items())

    def cmd_status(self, args: list[str]) -> str:
        from rankrecovery.services.playerview import PlayerViewService

        d = PlayerViewService(self.ctx).dashboard()
        lines = [
            f"players {d['players']} · images {d['images']} · confirmed {d['confirmed_players']} · provisional {d['provisional_players']} · stale {d['stale_summaries']}",
            f"needs review {d['detections_needing_review']} · unresolved identities {d['unresolved_identities']} · exportable {d['exportable_players']} · csv rows {d['trusted_csv_rows']}",
            f"vision configured: {d['vision_configured']} · model {self.ctx.settings.get('extraction.vision_model')}",
        ]
        if d["flag_counts"]:
            lines.append("flags: " + ", ".join(f"{k} {v}" for k, v in sorted(d["flag_counts"].items())))
        running = [j for j in self.runner.list(limit=20) if j["status"] in ("running", "queued")]
        if running:
            lines.append("active jobs: " + ", ".join(f"#{j['id']} {j['kind']} {j['status']}" for j in running))
        return "\n".join(lines)

    def cmd_env(self, args: list[str]) -> str:
        import os

        s = self.ctx.settings
        lines = [
            f"ANTHROPIC_API_KEY (env): {_mask(os.environ.get('ANTHROPIC_API_KEY'))}",
            f"vision.anthropic_api_key (settings): {'set' if s.is_configured('vision.anthropic_api_key') else '(not set)'}",
            f"extraction.default_extractor: {s.get('extraction.default_extractor')} · vision model: {s.get('extraction.vision_model')}",
            f"RANKRECOVERY_BOT_DISCORD_TOKEN: {_mask(os.environ.get('RANKRECOVERY_BOT_DISCORD_TOKEN'))}",
            f"RANKRECOVERY_BOT_API_KEY: {_mask(os.environ.get('RANKRECOVERY_BOT_API_KEY'))}",
            f"RANKRECOVERY_BOT_GUILD_IDS: {os.environ.get('RANKRECOVERY_BOT_GUILD_IDS') or '(not set)'} · STAFF_ROLE_IDS: {os.environ.get('RANKRECOVERY_BOT_STAFF_ROLE_IDS') or '(not set)'}",
            f"RANKRECOVERY_ADMIN_KEY (bootstrap): {'SET - remove it once real keys exist' if os.environ.get('RANKRECOVERY_ADMIN_KEY') else '(not set)'}",
            f"database: {self.ctx.db.url}",
        ]
        return "\n".join(lines)

    def cmd_jobs(self, args: list[str]) -> str:
        n = int(args[0]) if args else 10
        jobs = self.runner.list(limit=n)
        if not jobs:
            return "no jobs yet"
        return "\n".join(f"#{j['id']} {j['kind']} {j['status']} {j['done']}/{j['total']} failed {j['failed']} {j.get('progress_message', '')}".rstrip() for j in jobs)

    def cmd_job(self, args: list[str]) -> str:
        if not args:
            return "usage: job <id>"
        j = self.runner.get(int(args[0]))
        lines = [f"#{j['id']} {j['kind']} {j['status']} {j['done']}/{j['total']} failed {j['failed']} · {j.get('progress_message', '')}".rstrip()]
        if j.get("result"):
            lines.append("result: " + json.dumps(j["result"]))
        if j.get("error"):
            lines.append("error: " + j["error"])
        for e in j.get("errors", [])[:20]:
            lines.append(f"  {e['item']}: {e['message']}")
        return "\n".join(lines)

    def cmd_cancel(self, args: list[str]) -> str:
        if not args:
            return "usage: cancel <job id>"
        return "cancel requested" if self.runner.cancel(int(args[0])) else "job not found"

    def cmd_extract(self, args: list[str]) -> str:
        force = "--force" in args
        extractor = next((a for a in args if a in ("vision", "ocr", "auto")), None)
        from rankrecovery.services.extraction import ExtractionService

        pending = ExtractionService(self.ctx).pending_images() if not force else None
        job_id = self.runner.submit("extract_images", {"force": force, "extractor": extractor}, label="console extract", created_by="console")
        return f"queued job #{job_id}" + (f" for {len(pending)} pending image(s)" if pending is not None else " for all images (force)")

    def cmd_recompute(self, args: list[str]) -> str:
        job_id = self.runner.submit("recompute_players", {"all": bool(args and args[0] == "all")}, label="console recompute", created_by="console")
        return f"queued job #{job_id}"

    def cmd_reparse(self, args: list[str]) -> str:
        job_id = self.runner.submit("reparse_detections", {"all": True}, label="console reparse", created_by="console")
        return f"queued job #{job_id} (re-parse stored readings, then recompute every player)"

    def cmd_alias(self, args: list[str]) -> str:
        from rankrecovery.core.ranks import PolicyError, RankPolicy
        from rankrecovery.core.text import normalise_prefix_text

        ranks = [dict(r) for r in self.ctx.settings.get("ranks.player_ranks")]
        if args and args[0] == "list":
            return "\n".join(f"{r['name']}: {', '.join(r.get('aliases') or []) or '-'}" for r in ranks)
        if len(args) != 3 or args[0] not in ("add", "remove"):
            return "usage: alias add <RANK> <ALIAS> | alias remove <RANK> <ALIAS> | alias list"
        rank_name, alias = normalise_prefix_text(args[1]), normalise_prefix_text(args[2])
        target = next((r for r in ranks if normalise_prefix_text(str(r["name"])) == rank_name), None)
        if target is None:
            return f"unknown rank {args[1]!r}; ranks: {', '.join(str(r['name']) for r in ranks)}"
        aliases = [normalise_prefix_text(str(a)) for a in target.get("aliases") or []]
        if args[0] == "add":
            if alias in aliases:
                return f"{alias} is already an alias of {rank_name}"
            aliases.append(alias)
        else:
            if alias not in aliases:
                return f"{alias} is not an alias of {rank_name}"
            aliases.remove(alias)
        target["aliases"] = aliases
        try:
            RankPolicy.from_config(ranks, self.ctx.settings.get("ranks.staff_prefixes"), scuffed_rank=str(self.ctx.settings.get("ranks.scuffed_rank", "SCUFFED")))
        except PolicyError as exc:
            return f"refused: {exc}"
        self.ctx.settings.set("ranks.player_ranks", ranks, changed_by="console", reason=f"alias {args[0]} {alias} -> {rank_name}")
        job_id = self.runner.submit("reparse_detections", {"all": True}, label=f"re-parse after alias {args[0]}", created_by="console")
        return f"{alias} {'now maps to' if args[0] == 'add' else 'removed from'} {rank_name}; re-parse and recompute queued as job #{job_id}"

    def cmd_staff_alias(self, args: list[str]) -> str:
        from rankrecovery.core.ranks import PolicyError, RankPolicy
        from rankrecovery.core.text import normalise_prefix_text

        staff = [dict(r) for r in self.ctx.settings.get("ranks.staff_prefixes")]
        if len(args) != 3 or args[0] not in ("add", "remove"):
            return "usage: staff-alias add <STAFF> <ALIAS> | staff-alias remove <STAFF> <ALIAS>"
        staff_name, alias = normalise_prefix_text(args[1]), normalise_prefix_text(args[2])
        target = next((r for r in staff if normalise_prefix_text(str(r["name"])) == staff_name), None)
        if target is None:
            return f"unknown staff prefix {args[1]!r}; known: {', '.join(str(r['name']) for r in staff)}"
        aliases = [normalise_prefix_text(str(a)) for a in target.get("aliases") or []]
        if args[0] == "add" and alias not in aliases:
            aliases.append(alias)
        elif args[0] == "remove" and alias in aliases:
            aliases.remove(alias)
        target["aliases"] = aliases
        try:
            RankPolicy.from_config(self.ctx.settings.get("ranks.player_ranks"), staff, scuffed_rank=str(self.ctx.settings.get("ranks.scuffed_rank", "SCUFFED")))
        except PolicyError as exc:
            return f"refused: {exc}"
        self.ctx.settings.set("ranks.staff_prefixes", staff, changed_by="console", reason=f"staff alias {args[0]} {alias} -> {staff_name}")
        job_id = self.runner.submit("reparse_detections", {"all": True}, label=f"re-parse after staff alias {args[0]}", created_by="console")
        return f"{alias} {'now maps to' if args[0] == 'add' else 'removed from'} {staff_name}; re-parse and recompute queued as job #{job_id}"

    def cmd_convert(self, args: list[str]) -> str:
        from rankrecovery.services.conversion import ConversionService

        return "conversion: " + json.dumps(ConversionService(self.ctx).recompute())

    def cmd_lookup(self, args: list[str]) -> str:
        job_id = self.runner.submit("lookup_uuids", {"retry_failed": "--retry-failed" in args, "force": "--force" in args, "all": "--all" in args}, label="console lookup", created_by="console")
        return f"queued job #{job_id}"

    @staticmethod
    def _option(args: list[str], name: str) -> str | None:
        if name in args:
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else None
        return None

    def cmd_ingest(self, args: list[str]) -> str:
        from rankrecovery.services.ingest import FolderIngestService

        svc = FolderIngestService(self.ctx)
        waiting = svc.waiting()
        if not waiting:
            return f"no files waiting in {svc.folder_path()}; drop screenshots there (File Manager) and run 'ingest' again"
        submitter = self._option(args, "--submitter")
        job_id = self.runner.submit("ingest_folder", {"submitter": submitter, "extract": "--no-extract" not in args, "actor": "console"}, label=f"ingest {len(waiting)} file(s) from {svc.folder_path().name}", created_by="console", total=len(waiting))
        return f"found {len(waiting)} file(s) in {svc.folder_path()}; queued job #{job_id} (watch it with 'job {job_id}'; processed files disappear from the folder as they go)"

    def cmd_drive(self, args: list[str]) -> str:
        if not args or args[0].startswith("--"):
            return "usage: drive <google drive link> [--submitter <name>] [--no-ingest]"
        from rankrecovery.services.drive import parse_drive_link

        try:
            kind, drive_id = parse_drive_link(args[0])
        except ValueError as exc:
            return str(exc)
        submitter = self._option(args, "--submitter")
        job_id = self.runner.submit("download_drive", {"url": args[0], "submitter": submitter, "ingest": "--no-ingest" not in args, "actor": "console"}, label=f"download Drive {kind} {drive_id}", created_by="console")
        return f"queued job #{job_id}: downloading Drive {kind} {drive_id} into the newuploads folder" + (" and submitting it" if "--no-ingest" not in args else "")

    def cmd_uuid_ranks(self, args: list[str]) -> str:
        from rankrecovery.services.uuidranks import UuidRankService

        svc = UuidRankService(self.ctx)
        action = args[0].lower() if args else "rebuild"
        if action == "rebuild":
            s = svc.rebuild(actor="console")
            return f"uuid_ranks rebuilt: {s['uuids']} uuid(s) · {s['records_merged']} duplicate record(s) collapsed · {s['resolved_without_rank']} resolved without a rank (left out) · {s['unresolved_left_as_is']} unresolved (left as is)"
        if action == "list":
            n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
            rows = svc.rows(limit=n)
            if not rows:
                return "uuid_ranks is empty; run 'uuid-ranks rebuild'"
            return "\n".join(f"{r['uuid']}  {r['highest_rank']:<10} -> {r['new_rank'] or '-':<10} {r['current_name'] or '?':<17} records {r['records']}  names {', '.join(r['usernames'])}"[:200] for r in rows) + (f"\n… {svc.count()} total" if svc.count() > n else "")
        if action == "csv":
            if len(args) < 2:
                return "usage: uuid-ranks csv <file>"
            from pathlib import Path

            path = Path(args[1])
            path.write_text(svc.csv_text(), encoding="utf-8")
            return f"wrote {svc.count()} row(s) to {path} (download it with the File Manager)"
        return "usage: uuid-ranks [rebuild|list [n]|csv <file>]"

    def cmd_players(self, args: list[str]) -> str:
        from rankrecovery.services.playerview import PlayerViewService

        table = PlayerViewService(self.ctx).table(search=" ".join(args), limit=30)
        if not table["rows"]:
            return "no players"
        return "\n".join(r["compact"][:200] for r in table["rows"]) + (f"\n… {table['total']} total" if table["total"] > 30 else "")

    def cmd_player(self, args: list[str]) -> str:
        if not args:
            return "usage: player <name>"
        from rankrecovery.services.playerview import PlayerViewService

        d = PlayerViewService(self.ctx).lookup_by_name(args[0])
        if d is None:
            return "no such player"
        s = d["summary"]
        lines = [d["summary"]["compact"] or d["player"]["display_name"], f"uuid {d['player']['uuid']} ({d['player']['identity_status']}) · aliases {', '.join(d['player']['aliases'])}"]
        for v in d["verification"]:
            lines.append(f"  {v['rank']}: {v['status']} {v['progress']} confirmed_by={v['confirmed_by']}")
            for src in v["sources"]:
                lines.append(f"     image {src['image_id']} by {src['submitter']}: {'counts' if src['counted'] else 'not counted'} {'; '.join(src['reasons'])}".rstrip())
        if d["conversion"]:
            lines.append(f"new rank {d['conversion']['new_rank']} included={d['conversion']['included']} {'; '.join(d['conversion']['reasons'])}".rstrip())
        lines.append(f"stale={s['stale']} · flags: {', '.join(f['code'] for f in d['flags'] if f['is_active']) or 'none'}")
        return "\n".join(lines)

    def cmd_identity_import(self, args: list[str]) -> str:
        if not args:
            return "usage: identity-import <file> [--create]"
        from pathlib import Path

        from rankrecovery.services.identity import IdentityService

        path = Path(args[0])
        if not path.exists():
            return f"file not found: {path} (upload it with the File Manager first)"
        summary = IdentityService(self.ctx).import_identity_records(path.read_bytes(), filename=path.name, actor="console", create_missing="--create" in args)
        lines = [f"records {summary['records']} · matched {summary['matched']} · already known {summary['already_known']} · conflicts {summary['conflicts']} · unmatched {summary['unmatched']} · created {summary['created']} · invalid {summary['invalid']} · merged {summary['merged']}"]
        lines += [f"  {e}" for e in summary["errors"][:10]]
        if summary["matched"] or summary["conflicts"]:
            job_id = self.runner.submit("recompute_players", {}, label="recompute after identity import", created_by="console")
            lines.append(f"recompute queued as job #{job_id}; run 'names' to fetch current Mojang names")
        return "\n".join(lines)

    def cmd_names(self, args: list[str]) -> str:
        job_id = self.runner.submit("refresh_names", {"force": "--force" in args}, label="refresh current names", created_by="console")
        return f"queued job #{job_id}"

    def cmd_claims(self, args: list[str]) -> str:
        from rankrecovery.services.identity import IdentityService

        claims = IdentityService(self.ctx).list_claims(status="pending")
        return "\n".join(f"#{c['claim_id']} {c.get('old_username')} -> {c.get('current_username')} ({c.get('uuid')}) rank {c.get('effective_rank') or 'none'} by {c.get('discord_username') or c.get('created_by')}{' CONFLICT' if c.get('conflict_with') else ''}" for c in claims) or "no pending claims"

    def cmd_claim(self, args: list[str]) -> str:
        if len(args) < 2 or args[0] not in ("approve", "reject") or not args[1].isdigit():
            return "usage: claim approve <id> [note] | claim reject <id> <reason>"
        from rankrecovery.services.identity import IdentityService

        note = " ".join(args[2:])
        try:
            result = IdentityService(self.ctx).decide_claim(int(args[1]), approve=args[0] == "approve", actor="console", note=note, force="--force" in args)
        except KeyError:
            return "no such claim"
        except ValueError as exc:
            return f"refused: {exc}"
        return f"claim #{args[1]} {result['status']}: {result.get('old_username')} -> {result.get('current_username')} ({result.get('uuid')})"

    def cmd_link_old(self, args: list[str]) -> str:
        names = [a for a in args if not a.startswith("--")]
        if len(names) < 2:
            return "usage: link-old <old name> <current name> [--force]"
        from rankrecovery.services.identity import IdentityService

        try:
            result = IdentityService(self.ctx).link_by_names(names[0], names[1], actor="console", note=" ".join(names[2:]), force="--force" in args)
        except KeyError as exc:
            return str(exc).strip("'")
        except ValueError as exc:
            return f"refused: {exc}"
        return f"{names[0]} -> {result['current_username']} ({result['uuid']}) linked; merged {result['merged']} duplicate record(s); recomputed"

    def cmd_lookups(self, args: list[str]) -> str:
        if not args:
            return "usage: lookups <name> [n]"
        from sqlalchemy import select

        from rankrecovery.db.models import LookupAttemptRow
        from rankrecovery.services.players import find_player_by_username

        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
        with self.ctx.db.read_session() as s:
            player = find_player_by_username(s, args[0])
            if player is None:
                return "no such player"
            rows = s.execute(select(LookupAttemptRow).where(LookupAttemptRow.player_id == player.id).order_by(LookupAttemptRow.id.desc()).limit(n)).scalars().all()
            if not rows:
                return "no lookup attempts recorded"
            lines = []
            for r in rows:
                lines.append(f"#{r.id} {r.started_at.strftime('%H:%M:%S') if r.started_at else '?'} {r.provider} {r.queried_name} -> HTTP {r.http_status} {r.outcome} {r.failure_code or ''} {r.error_message or ''}".rstrip())
                if r.raw_excerpt:
                    lines.append(f"     body: {r.raw_excerpt}")
                if r.resolved_uuid:
                    lines.append(f"     uuid: {r.resolved_uuid} name: {r.resolved_name}")
            return "\n".join(lines)

    def cmd_images(self, args: list[str]) -> str:
        from sqlalchemy import select

        from rankrecovery.db.models import SourceImage, Submitter

        n = int(args[0]) if args else 10
        with self.ctx.db.read_session() as s:
            subs = {x.id: x.name for x in s.execute(select(Submitter)).scalars()}
            rows = s.execute(select(SourceImage).order_by(SourceImage.id.desc()).limit(n)).scalars().all()
            if not rows:
                return "no images"
            return "\n".join(f"#{i.id} {i.original_filename} {i.width}x{i.height} submitter={subs.get(i.submitter_id, 'unknown')} ai={i.ai_status} date={i.date_status} last_run={i.last_extraction_run_id}" for i in rows)

    def cmd_image(self, args: list[str]) -> str:
        if not args:
            return "usage: image <id>"
        from rankrecovery.services.rescan import RescanService

        c = RescanService(self.ctx).image_context(int(args[0]))
        lines = [f"#{c['image']['image_id']} {c['image']['filename']} submitter={c['image']['submitter']} ai={c['image']['ai_status']} date={c['image']['date_status']}"]
        for r in c["runs"]:
            lines.append(f"  run {r['id']} {r['extractor']}/{r['engine']} {r['model']} {r['status']} {r['error'] or ''} {'; '.join(r['warnings'])}".rstrip())
        for d in c["detections"]:
            lines.append(f"  det {d['detection_id']} [{d['region']}] prefix={d['raw_prefix']!r} user={d['username']!r} rank={d['rank']} staff={d['staff']} status={d['status']} conf={d['confidence']:.2f}")
        for f in c["flags"]:
            if f["is_active"]:
                lines.append(f"  flag {f['code']}: {f['details']}")
        return "\n".join(lines)

    def cmd_vision(self, args: list[str]) -> str:
        if not args or args[0] not in ("test", "model"):
            return "usage: vision test | vision model <model id>"
        if args[0] == "model":
            if len(args) < 2:
                return "usage: vision model <model id>"
            self.ctx.settings.set("extraction.vision_model", args[1], changed_by="console")
            return f"vision model set to {args[1]}"
        key = self.ctx.anthropic_api_key()
        if not key:
            return "no Anthropic API key configured (ANTHROPIC_API_KEY in .env or vision.anthropic_api_key setting)"
        try:
            import anthropic
        except ImportError:
            return "the anthropic package is not installed"
        client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=30)
        model = str(self.ctx.settings.get("extraction.vision_model"))
        try:
            client.models.retrieve(model)
        except anthropic.AuthenticationError as exc:
            return f"key REJECTED (401): {getattr(exc, 'message', exc)}. Check the ANTHROPIC_API_KEY line in .env (no quotes, no spaces) and restart."
        except anthropic.NotFoundError:
            return f"key accepted, but model {model} is not available to this account. Try: vision model claude-sonnet-5-5"
        except anthropic.APIStatusError as exc:
            return f"API error {exc.status_code}: {getattr(exc, 'message', exc)}"
        except anthropic.APIConnectionError as exc:
            return f"cannot reach api.anthropic.com from this container: {exc}"
        return f"key accepted and model {model} is available"

    def cmd_settings(self, args: list[str]) -> str:
        if not args:
            return "usage: settings get <key> | settings set <key> <json> | settings list [prefix]"
        if args[0] == "list":
            prefix = args[1] if len(args) > 1 else ""
            values = self.ctx.settings.get_all()
            return "\n".join(f"{k} = {json.dumps(v)[:120]}" for k, v in sorted(values.items()) if k.startswith(prefix)) or "no such settings"
        if args[0] == "get" and len(args) == 2:
            return json.dumps(self.ctx.settings.get_all().get(args[1]))
        if args[0] == "set" and len(args) >= 3:
            raw = " ".join(args[2:])
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            changed = self.ctx.settings.set(args[1], value, changed_by="console")
            from rankrecovery.api.routes.settings import RECOMPUTE_KEYS, REPARSE_KEYS

            if changed and args[1] in REPARSE_KEYS:
                from rankrecovery.services.reconcile import ReconcileService

                ReconcileService(self.ctx).mark_all_stale(f"console changed {args[1]}")
                job_id = self.runner.submit("reparse_detections", {"all": True}, label="re-parse after console setting", created_by="console")
                return f"set {args[1]}; re-parse and recompute queued as job #{job_id}"
            if changed and args[1] in RECOMPUTE_KEYS:
                from rankrecovery.services.reconcile import ReconcileService

                ReconcileService(self.ctx).mark_all_stale(f"console changed {args[1]}")
                job_id = self.runner.submit("recompute_players", {"all": True}, label="recompute after console setting", created_by="console")
                return f"set {args[1]}; recompute queued as job #{job_id}"
            return f"set {args[1]}" if changed else f"{args[1]} unchanged"
        return "usage: settings get <key> | settings set <key> <json> | settings list [prefix]"

    def cmd_keys(self, args: list[str]) -> str:
        from rankrecovery.api.auth import list_api_keys

        keys = list_api_keys(self.ctx)
        return "\n".join(f"#{k['id']} {k['name']} role={k['role']} prefix={k['prefix']} {'REVOKED' if k['revoked'] else ''} last used {k['last_used_at'] or 'never'}" for k in keys) or "no keys"

    def cmd_key(self, args: list[str]) -> str:
        from rankrecovery.api.auth import create_api_key, revoke_api_key
        from rankrecovery.core.enums import ApiRole

        if len(args) == 3 and args[0] == "create":
            try:
                role = ApiRole(args[2])
            except ValueError:
                return "role must be admin, staff, bot or readonly"
            raw, key_id = create_api_key(self.ctx, name=args[1], role=role, created_by="console")
            return f"created key #{key_id} ({role.value}). Copy it now, it is not shown again:\n{raw}"
        if len(args) == 2 and args[0] == "revoke":
            return "revoked" if revoke_api_key(self.ctx, int(args[1])) else "key not found"
        return "usage: key create <name> <admin|staff|bot|readonly> | key revoke <id>"

    def cmd_export(self, args: list[str]) -> str:
        from rankrecovery.services.export import ExportService

        p = ExportService(self.ctx).preview()
        lines = [f"{p['command_count']} command(s), {p['skipped_count']} skipped"]
        lines += [f"  skip {r['display_name']}: {r['skip_reason']}" for r in p["skipped"][:15]]
        lines += [f"  {c}" for c in p["commands"][:10]]
        lines += [f"WARNING: {w}" for w in p["warnings"]]
        return "\n".join(lines)

    def cmd_csv_batches(self, args: list[str]) -> str:
        from sqlalchemy import select

        from rankrecovery.db.models import CsvBatch

        with self.ctx.db.read_session() as s:
            rows = s.execute(select(CsvBatch).order_by(CsvBatch.id.desc()).limit(20)).scalars().all()
            return "\n".join(f"#{b.id} {b.filename} valid {b.valid_count} rejected {b.rejected_count} duplicates {b.duplicate_count} {'ACTIVE' if b.is_active else 'retired'} by {b.imported_by}" for b in rows) or "no batches"

    def cmd_csv_retire(self, args: list[str]) -> str:
        if not args or not args[0].isdigit():
            return "usage: csv-retire <batch id>"
        from rankrecovery.services.csvimport import CsvImportService

        try:
            result = CsvImportService(self.ctx).retire_batch(int(args[0]), actor="console", reason=" ".join(args[1:]) or "retired from console")
        except KeyError:
            return "no such batch"
        job_id = self.runner.submit("recompute_players", {}, label="recompute after batch retire", created_by="console")
        return f"retired {result['rows_retired']} row(s); recompute queued as job #{job_id}. The file can now be imported again."

    def cmd_package_map(self, args: list[str]) -> str:
        mapping = {str(k): str(v) for k, v in (self.ctx.settings.get("csv.package_rank_mapping") or {}).items()}
        if args and args[0] == "list":
            return "\n".join(f"{k!r} -> {v}" for k, v in sorted(mapping.items())) or "no package mappings"
        if len(args) == 3 and args[0] == "add":
            canonical = self.ctx.policy().canonical_rank(args[2])
            if canonical is None:
                return f"unknown rank {args[2]!r}"
            mapping[args[1].strip().strip("'\"").lower()] = canonical
            self.ctx.settings.set("csv.package_rank_mapping", mapping, changed_by="console")
            return f"{args[1]!r} -> {canonical}; re-import (or retire and re-import) the CSV to apply"
        if len(args) == 2 and args[0] == "remove":
            mapping.pop(args[1].strip().strip("'\"").lower(), None)
            self.ctx.settings.set("csv.package_rank_mapping", mapping, changed_by="console")
            return "removed"
        return 'usage: package-map add "<package name>" <RANK> | remove "<package name>" | list'

    def cmd_backup(self, args: list[str]) -> str:
        from rankrecovery.db.backup import backup_sqlite

        return f"backup written: {backup_sqlite(self.ctx.db, str(self.ctx.settings.get('database.backup_dir', 'backups')))}"

    def cmd_logs(self, args: list[str]) -> str:
        from rankrecovery.api import logbuffer

        handler = logbuffer.install()
        n = int(args[0]) if args and args[0].isdigit() else 30
        level = args[1] if len(args) > 1 else (args[0] if args and not args[0].isdigit() else None)
        return "\n".join(f"{r['time'][11:19]} {r['level']} {r['logger']}: {r['message']}" for r in handler.tail(limit=n, level=level)) or "no log records"

    def cmd_stop(self, args: list[str]) -> str:
        if self._stop:
            self._stop()
            return "stopping"
        return "stop is not available"
