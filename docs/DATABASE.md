# Database choice and concurrency

## Options assessed

| Option | Verdict |
|---|---|
| **H2 (embedded Java DB) via JDBC** | Rejected. There is no maintained SQLAlchemy dialect for H2. Access from Python would need a JVM in-process (JayDeBeApi/JPype) with a generic DB-API shim: no ORM type support worth relying on, no Alembic migrations, a JVM dependency on every machine, and no benefit over SQLite/PostgreSQL for this workload. |
| **SQLite in WAL mode** | **Chosen default.** Zero-ops single file, excellent read concurrency, fast bulk inserts, trivial backups. Writes are serialised (one writer at a time). |
| **PostgreSQL** | Supported through the same code path (`RANKRECOVERY_DATABASE_URL=postgresql+psycopg://…`). Use it if many clients and the bot write heavily at the same time, or if you want true parallel writes. |
| DuckDB / others | Not suited: DuckDB is analytical with single-writer semantics and weaker ORM support. |

## What SQLAlchemy QueuePool does and does not do

`QueuePool` keeps a pool of open DBAPI connections so threads do not pay the
connect cost each time. It does **not** make a database accept parallel writes.
On SQLite, two connections writing at once still contend for the single write
lock; one waits (`busy_timeout`) or fails with `database is locked`.

## How the application keeps SQLite safe and fast

* `PRAGMA journal_mode=WAL` – readers never block the writer and vice versa.
* `PRAGMA busy_timeout=30000` and `synchronous=NORMAL` – short waits instead of errors; durable enough with WAL.
* An **in-process writer lock** (`Database.write_session()`): job workers and API
  threads queue for the write slot. Transactions are short (one image, one player,
  one batch of rows) so the queue drains quickly.
* **Batched writes**: detections and observations for an image are inserted in one transaction; CSV rows per batch.
* Sessions are per-call and never shared between threads.
* Extraction results are cached by image hash + extractor settings so nothing is re-sent to the API or re-OCR'd.
* Indexes on every lookup column (hashes, username keys, ranks, flags, statuses).

Honest limitation: with SQLite, two processes (for example the server and a
separate CLI run) writing at the same time are still serialised by SQLite's
file lock and may wait up to the busy timeout. Run bulk CLI imports through the
server API, or stop the server first, or use PostgreSQL.

## Schema

35 tables (see `rankrecovery/db/models.py`). Raw evidence tables
(`source_images`, `image_metadata`, `extraction_runs`, `detections`, `csv_rows`,
`manual_assertions`, `lookup_attempts`) are never rewritten by recomputes.
Derived tables (`player_summaries`, `verification_results`, `conversions`,
`export_batches`) are recomputed from evidence and carry the policy fingerprint
and version used.

`uuid_ranks` is the flat hand-off table: one row per resolved account (a
player whose UUID is verified or set by hand), keyed by `uuid`, with the
highest effective rank across every name record that maps to that UUID
(`highest_rank`, `rank_priority`), the new rank it maps to (`new_rank`), every
username involved (`usernames`), the contributing `player_ids`, the ranks each
record had (`ranks_seen`) and `current_name`. Players without a resolved UUID
are not in it and are left as they are. It is rebuilt in full after every
lookup, extraction, recompute, conversion and approved claim, or on demand
with `uuid-ranks rebuild` / `POST /uuid-ranks/rebuild`; `uuid-ranks csv` and
`GET /uuid-ranks/export.csv` give it as CSV.

Migrations: Alembic (`rankrecovery/db/migrations`). `rankrecovery-admin init-db`
or the server start-up upgrades to head automatically. Create a new revision with
`alembic revision --autogenerate -m "..."` after changing models.

## Settings and policy versioning

All settings live in the `settings` table (seeded from `config/defaults.py`).
Every change writes `settings_history` (old/new value, who, why; secrets are
not recorded). Changes to rank order, aliases, staff prefixes, mapping or
verification rules create a `policy_versions` snapshot; every summary, conversion
and export records the fingerprint it was computed with, so an old export can
always be explained.

## Backup and restore

* `rankrecovery-admin backup --dir backups` or the client's Settings › Database page
  uses SQLite's online backup API (safe while the server runs).
* `rankrecovery-admin restore <file> --yes` (stop the server first) replaces the
  live file after validating the backup and keeping a `.pre-restore` copy.
* PostgreSQL: use `pg_dump` / `pg_restore`.

## Image storage policy

`images.store_bytes_in_database` (default **on**) stores the original bytes in
`source_images.content` so evidence survives moved or deleted source folders
and can always be rescanned. Turn it off only if you keep the original files on
the server and accept that a missing file makes an image non-rescannable
(`availability=missing`).
