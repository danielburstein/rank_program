"""Engine and session management.

Database choice (see docs/DATABASE.md for the full discussion):

* **SQLite in WAL mode** is the default. WAL allows many concurrent readers
  alongside one writer; writes are serialised by SQLite itself. We add an
  in-process writer lock so worker threads queue for the write slot instead
  of hitting ``database is locked`` errors, keep write transactions short and
  batch inserts. SQLAlchemy's QueuePool only pools connections; it does not
  create write parallelism.
* **PostgreSQL** is supported through the same code path by passing a
  ``postgresql+psycopg://`` URL. It gives true concurrent writes and is the
  recommended choice when the server is shared by the bot and many clients.
* **H2** has no maintained SQLAlchemy dialect and would require a JVM bridge
  (JayDeBeApi/JPype) with no ORM support worth relying on; it was rejected.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

log = logging.getLogger(__name__)


class _NoopLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


class Database:
    """Owns the engine, the session factory and the writer lock."""

    def __init__(self, url: str, *, echo: bool = False, busy_timeout_ms: int = 30_000, pool_size: int = 10):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        self.busy_timeout_ms = busy_timeout_ms
        if self.is_sqlite:
            path = self._sqlite_path(url)
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            connect_args = {"check_same_thread": False, "timeout": busy_timeout_ms / 1000}
            if path == ":memory:" or not path:
                from sqlalchemy.pool import StaticPool

                self.engine: Engine = create_engine(url, echo=echo, connect_args=connect_args, poolclass=StaticPool, future=True)
            else:
                self.engine = create_engine(
                    url,
                    echo=echo,
                    connect_args=connect_args,
                    poolclass=QueuePool,
                    pool_size=pool_size,
                    max_overflow=pool_size,
                    pool_pre_ping=True,
                    future=True,
                )
            self._install_sqlite_pragmas()
            self.write_lock: threading.RLock | _NoopLock = threading.RLock()
        else:
            self.engine = create_engine(url, echo=echo, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, future=True)
            self.write_lock = _NoopLock()
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @staticmethod
    def _sqlite_path(url: str) -> str:
        if url in ("sqlite://", "sqlite:///:memory:"):
            return ":memory:"
        return url.split("sqlite:///", 1)[-1]

    def _install_sqlite_pragmas(self) -> None:
        busy = self.busy_timeout_ms

        @event.listens_for(self.engine, "connect")
        def _set_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={busy}")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA cache_size=-65536")
            finally:
                cursor.close()

    # ---------------------------------------------------------------- sessions
    @contextlib.contextmanager
    def read_session(self) -> Iterator[Session]:
        """A session for reads. Never shared between threads."""
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    @contextlib.contextmanager
    def write_session(self) -> Iterator[Session]:
        """A session for a short write transaction. Serialised on SQLite."""
        with self.write_lock:
            session = self.session_factory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    # ------------------------------------------------------------------ schema
    def create_all(self) -> None:
        from rankrecovery.db.models import Base

        Base.metadata.create_all(self.engine)

    def run_migrations(self) -> None:
        """Upgrade the schema to head using Alembic (creates tables on a new DB)."""
        from rankrecovery.db.migrate import upgrade_to_head

        upgrade_to_head(self)

    def checkpoint(self) -> None:
        if self.is_sqlite:
            with self.engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    def dispose(self) -> None:
        self.engine.dispose()

    def describe_concurrency(self) -> str:
        if self.is_sqlite:
            return (
                "SQLite (WAL): unlimited concurrent readers, one writer at a time. "
                "Writes from worker threads are serialised through an in-process lock; "
                "QueuePool only pools connections and does not add write parallelism."
            )
        return "Server database: concurrent readers and writers handled by the database server."


def sqlite_url(path: str | Path) -> str:
    return f"sqlite:///{Path(path).resolve()}"
