"""Alembic helpers that work from an installed package (no alembic.ini needed)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from rankrecovery.db.engine import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db: Database) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db.url.replace("%", "%%"))
    cfg.attributes["connectable"] = db.engine
    return cfg


def upgrade_to_head(db: Database) -> None:
    with db.write_lock:
        command.upgrade(alembic_config(db), "head")


def current_revision(db: Database) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with db.engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
