from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from rankrecovery.db.engine import Database, sqlite_url
from rankrecovery.db.migrate import current_revision
from rankrecovery.db.models import Player, PolicyVersion, Setting, SettingHistory
from rankrecovery.db.settings_store import SettingsStore


@pytest.fixture
def db(tmp_path):
    database = Database(sqlite_url(tmp_path / "t.db"))
    database.run_migrations()
    yield database
    database.dispose()


def test_migrations_create_schema_and_wal(db):
    assert current_revision(db) is not None
    with db.engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_settings_defaults_history_and_policy_versions(db):
    store = SettingsStore(db)
    assert store.get("verification.require_two_submitters_above_immortal") is False
    assert store.set("verification.require_two_submitters_above_immortal", True, changed_by="tester", reason="test") is True
    assert store.set("verification.require_two_submitters_above_immortal", True) is False  # unchanged
    assert store.get("verification.require_two_submitters_above_immortal") is True
    hist = store.history("verification.require_two_submitters_above_immortal")
    assert hist and hist[0]["old"] is False and hist[0]["new"] is True and hist[0]["changed_by"] == "tester"
    with db.read_session() as s:
        versions = s.execute(select(PolicyVersion).where(PolicyVersion.kind == "verification")).scalars().all()
        assert len(versions) == 1 and versions[0].payload["verification.require_two_submitters_above_immortal"] is True
    # restore defaults
    assert store.reset("verification.require_two_submitters_above_immortal", changed_by="tester")
    assert store.get("verification.require_two_submitters_above_immortal") is False


def test_secret_setting_masked(db):
    store = SettingsStore(db)
    store.set("vision.anthropic_api_key", "sk-ant-secret", changed_by="tester")
    assert store.get_all()["vision.anthropic_api_key"] == "***"
    assert store.get_all(include_secrets=True)["vision.anthropic_api_key"] == "sk-ant-secret"
    with db.read_session() as s:
        h = s.execute(select(SettingHistory).where(SettingHistory.key == "vision.anthropic_api_key")).scalars().first()
        assert h.new_value is None and h.old_value is None


def test_concurrent_writers_do_not_lock(db):
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for i in range(20):
                with db.write_session() as s:
                    s.add(Player(display_name=f"p{n}-{i}", name_key=f"p{n}-{i}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with db.read_session() as s:
        assert len(s.execute(select(Player)).scalars().all()) == 120
        assert s.execute(select(Setting)).scalars().all() == []


def test_backup_restore(db, tmp_path):
    from rankrecovery.db.backup import backup_sqlite, restore_sqlite

    with db.write_session() as s:
        s.add(Player(display_name="keep", name_key="keep"))
    target = backup_sqlite(db, tmp_path / "backups", label="t")
    assert target.exists()
    with db.write_session() as s:
        s.add(Player(display_name="after", name_key="after"))
    restore_sqlite(db, target)
    with db.read_session() as s:
        names = {p.display_name for p in s.execute(select(Player)).scalars()}
    assert names == {"keep"}
