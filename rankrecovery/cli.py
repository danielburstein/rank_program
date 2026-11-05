"""Administrative CLI: ``rankrecovery-admin``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rankrecovery-admin", description="Rank Recovery administration")
    parser.add_argument("--database-url", default=None, help="SQLAlchemy URL (default: data/rankrecovery.db or RANKRECOVERY_DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database")
    k = sub.add_parser("create-key", help="create an API key (printed once)")
    k.add_argument("--name", required=True)
    k.add_argument("--role", default="readonly", choices=["admin", "staff", "bot", "readonly"])
    sub.add_parser("list-keys", help="list API keys")
    r = sub.add_parser("revoke-key", help="revoke an API key by id")
    r.add_argument("key_id", type=int)
    b = sub.add_parser("backup", help="back up the SQLite database")
    b.add_argument("--dir", default="backups")
    b.add_argument("--label", default="")
    rs = sub.add_parser("restore", help="restore the SQLite database from a backup file (stop the server first)")
    rs.add_argument("backup_file")
    rs.add_argument("--yes", action="store_true")
    imp = sub.add_parser("import-folder", help="import screenshots from a folder (one subfolder per submitter)")
    imp.add_argument("folder")
    imp.add_argument("--submitter", default=None, help="submitter for every file (overrides subfolder names)")
    imp.add_argument("--no-subfolders", action="store_true", help="do not derive submitters from subfolder names")
    imp.add_argument("--extract", action="store_true", help="run extraction after import")
    imp.add_argument("--extractor", default=None, choices=[None, "auto", "vision", "ocr"])
    ex = sub.add_parser("extract", help="run extraction on pending images and recompute")
    ex.add_argument("--extractor", default=None, choices=[None, "auto", "vision", "ocr"])
    ex.add_argument("--force", action="store_true")
    sub.add_parser("recompute", help="recompute every player summary and conversion")
    s = sub.add_parser("set", help="set a setting (JSON value)")
    s.add_argument("key")
    s.add_argument("value")
    g = sub.add_parser("get", help="print a setting")
    g.add_argument("key")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from rankrecovery.api.app import build_context

    ctx = build_context(args.database_url)
    try:
        return _dispatch(ctx, args)
    finally:
        ctx.db.dispose()


def _dispatch(ctx, args) -> int:
    import json

    from rankrecovery.api.auth import create_api_key, list_api_keys, revoke_api_key
    from rankrecovery.core.enums import ApiRole

    if args.command == "init-db":
        print("database ready:", ctx.db.url)
        print(ctx.db.describe_concurrency())
        return 0
    if args.command == "create-key":
        raw, key_id = create_api_key(ctx, name=args.name, role=ApiRole(args.role), created_by="cli")
        print(f"key id {key_id} role {args.role}\n{raw}\n(store it now; it is not shown again)")
        return 0
    if args.command == "list-keys":
        for k in list_api_keys(ctx):
            print(json.dumps(k))
        return 0
    if args.command == "revoke-key":
        print("revoked" if revoke_api_key(ctx, args.key_id) else "not found")
        return 0
    if args.command == "backup":
        from rankrecovery.db.backup import backup_sqlite

        print(backup_sqlite(ctx.db, args.dir, label=args.label))
        return 0
    if args.command == "restore":
        if not args.yes:
            print("refusing without --yes (this replaces the live database)")
            return 2
        from rankrecovery.db.backup import restore_sqlite

        print(restore_sqlite(ctx.db, args.backup_file))
        return 0
    if args.command == "set":
        ctx.settings.set(args.key, json.loads(args.value), changed_by="cli")
        print("ok")
        return 0
    if args.command == "get":
        print(json.dumps(ctx.settings.get(args.key)))
        return 0
    if args.command in ("import-folder", "extract", "recompute"):
        return _run_pipeline_command(ctx, args)
    return 1


def _run_pipeline_command(ctx, args) -> int:
    from rankrecovery.services.conversion import ConversionService
    from rankrecovery.services.extraction import ExtractionService
    from rankrecovery.services.images import ImageService
    from rankrecovery.services.reconcile import ReconcileService

    if args.command == "import-folder":
        images = ImageService(ctx)
        batch_id = images.create_batch(source="cli", label=str(Path(args.folder).name), submitter_name=args.submitter, created_by="cli")
        files = images.iter_folder(args.folder, recursive=True, submitter_from_subfolder=not args.no_subfolders)
        created = 0
        for path, sub in files:
            submitter = args.submitter or sub
            try:
                outcome = images.import_path(path, submitter_name=submitter, batch_id=batch_id, created_by="cli")
                created += int(outcome.created)
                print(f"{'new' if outcome.created else 'dup'} {path} submitter={submitter or 'unknown'} ai={outcome.ai_status} date={outcome.date_status}")
            except ValueError as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)
        print(f"imported {created} new image(s) of {len(files)} file(s) into batch {batch_id}")
        if not args.extract:
            return 0
    if args.command in ("import-folder", "extract"):
        svc = ExtractionService(ctx)
        extractor = ctx.build_extractor(getattr(args, "extractor", None))
        force = bool(getattr(args, "force", False))
        pending = svc.pending_images(fingerprint=None if force else extractor.settings.fingerprint())
        print(f"extracting {len(pending)} image(s) with {extractor.settings.engine}")
        for image_id in pending:
            result = svc.extract_image(image_id, extractor=extractor, force=force, actor="cli")
            print(f"image {image_id}: detections={result.detections} ranks={result.rank_observations} staff={result.staff_observations}{' cached' if result.cached else ''}{' ERROR ' + result.error if result.error else ''}")
    recon = ReconcileService(ctx)
    ids = recon.all_player_ids() if args.command == "recompute" else recon.stale_player_ids()
    for pid in ids:
        recon.recompute_player(pid)
    counts = ConversionService(ctx).recompute()
    print(f"recomputed {len(ids)} player(s); conversion {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
