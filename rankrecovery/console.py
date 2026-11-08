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
            "convert": (self.cmd_convert, "run Mode 2 conversion"),
            "lookup": (self.cmd_lookup, "lookup [--retry-failed] [--all] [--force]: queue UUID lookups (--all re-checks resolved players too, --force ignores the cache)"),
            "players": (self.cmd_players, "players [search]: list players"),
            "lookups": (self.cmd_lookups, "lookups <name> [n]: recent UUID lookup attempts for a player, with status, failure code and the raw response excerpt"),
            "player": (self.cmd_player, "player <name>: full summary for one player"),
            "images": (self.cmd_images, "images [n]: recent images"),
            "image": (self.cmd_image, "image <id>: detections and runs for one image"),
            "vision": (self.cmd_vision, "vision test | vision model <id>: check the Anthropic key / change the model"),
            "settings": (self.cmd_settings, "settings get <key> | settings set <key> <json> | settings list [prefix]"),
            "keys": (self.cmd_keys, "keys: list API keys"),
            "key": (self.cmd_key, "key create <name> <admin|staff|bot|readonly> | key revoke <id>"),
            "export": (self.cmd_export, "export preview: command count and skip reasons"),
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

    def cmd_convert(self, args: list[str]) -> str:
        from rankrecovery.services.conversion import ConversionService

        return "conversion: " + json.dumps(ConversionService(self.ctx).recompute())

    def cmd_lookup(self, args: list[str]) -> str:
        job_id = self.runner.submit("lookup_uuids", {"retry_failed": "--retry-failed" in args, "force": "--force" in args, "all": "--all" in args}, label="console lookup", created_by="console")
        return f"queued job #{job_id}"

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
            from rankrecovery.api.routes.settings import RECOMPUTE_KEYS

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
