# Architecture

## From the original specification to this design

The original build request describes a single Windows desktop application with
six modes. The real goal is different in shape:

* a **server** on a dedicated machine that owns the database and does the work,
* a **Discord bot** through which players look up their rank and staff submit screenshots,
* a **desktop client** on your own computer for bulk submission and review.

So the project is one Python distribution (`rankrecovery`) with three
runnable programs sharing one codebase:

```
┌──────────────────┐      HTTPS/HTTP + API key      ┌──────────────────────────────┐
│ Discord bot      │ ─────────────────────────────▶ │ Server (FastAPI + job runner) │
│ /rank /submit    │                                │   core rules · services       │
└──────────────────┘                                │   SQLite WAL (or PostgreSQL)  │
┌──────────────────┐                                │   vision / OCR extractors     │
│ Desktop client   │ ─────────────────────────────▶ │   UUID lookup · exports       │
│ (PySide6)        │                                └──────────────────────────────┘
└──────────────────┘
```

Everything the spec calls a "mode" is a server capability exposed over the API;
the desktop client is the review GUI for all six modes, the bot exposes the
two public-facing operations.

| Spec mode | Where it lives | Server API | Client page | Bot |
|---|---|---|---|---|
| 1 Import, extraction, old-rank recovery | `services/images.py`, `services/extraction.py`, `services/reconcile.py` | `/images`, `/extract`, `/players` | Submit, Images, Players | `/submit` |
| 2 Old→new conversion | `services/conversion.py` | `/conversion/*` | Conversion | – |
| 3 UUID lookup, dedup | `identity/*`, `services/identity.py` | `/identity/*` | Identity | – |
| 4 LuckPerms export | `services/export.py` | `/export/*` | Export | – |
| 5 Search, review, manual entry | `services/search.py`, `services/review.py` | `/search`, `/flags`, `/detections/*`, `/observations/*` | Search, Players | `/rr find` |
| 6 Targeted rescan | `services/rescan.py` | `/rescan/*` | Rescan | – |
| Rank lookup by name | `services/playerview.py` | `/players/by-name/{name}` | Players | `/rank` |

## Package layout

```
rankrecovery/
  config/defaults.py      every default setting; seeds the DB settings store
  core/                   pure business rules, no I/O (fully unit tested)
    ranks.py              rank policy: order, aliases, staff prefixes, misread detection
    parsing.py            RawRow -> ParsedDetection (icons, staff suffix rule, Bedrock, partial names)
    evidence.py           evidence records handed to the engine
    reconciliation.py     per-player summary: eligibility, two-submitter rule, fallback, flags
    conversion.py         old->new mapping + flag filter policy
    export.py             LuckPerms command generation and de-duplication
    dates.py / ai_indicators.py / identity.py / usernames.py / text.py / flags.py
  db/                     SQLAlchemy models, engine (WAL + writer lock), Alembic migrations,
                          versioned settings store, backup/restore
  imaging/                decode, hashing (sha256 + perceptual), metadata, region detection,
                          preprocessing, overlays
  extraction/             TextExtractor contract; Anthropic vision extractor; Tesseract OCR; pipeline
  identity/               Mojang / Geyser providers, proxies, failure classification, retry service
  services/               application services that join core + db + extractors
  jobs/                   persistent background jobs (threads, cancel/pause, checkpoints)
  api/                    FastAPI app, API-key auth, routes
  bot/                    discord.py bot
  client/                 PySide6 desktop client
  cli.py                  rankrecovery-admin
scripts/sample_accuracy.py
tests/
```

## Data flow for one screenshot

1. **Import** (`ImageService.import_bytes`): sha256 → exact duplicate check; decode;
   metadata extraction (EXIF/XMP/PNG text/C2PA markers); AI-indicator scan
   (metadata = suspicion, filename = weak suspicion); date decision (embedded
   metadata › Minecraft filename pattern › nothing; file mtime and import time
   are never trusted by default); perceptual hash → duplicate group / near-duplicate flag;
   bytes stored in the DB (configurable). Submitter recorded (or `unknown`).
2. **Extraction** (`ExtractionService.extract_image`): the configured extractor
   returns `RawRow`s + raw output; results are cached by image hash + settings
   fingerprint. The vision extractor detects regions (heuristics + one layout
   call), crops and nearest-neighbour upscales each TAB column / chat / nametag
   / scoreboard and asks for strict JSON per row.
3. **Parsing** (`RowParser`): prefix → rank / seasonal alias / staff / custom
   (SCUFFED) / probable misread (review) / unreadable; trailing ☯ or ∞ after a
   staff name adds a SCUFFED or INFINITE observation; Bedrock dot-names are
   accepted; cut-off names are partial and yield no rank.
4. **Persistence**: `ExtractionRun`, `Detection` (with bboxes), `RankObservation`,
   `StaffObservation`, flags; players are created or matched by alias key only
   (never by similarity).
5. **Reconciliation** (`ReconcileService.recompute_player` → `ReconciliationEngine`):
   builds evidence from all observations of the merge group, decides eligibility
   per observation (AI, date, confidence, review), counts independent sources per
   rank (submitter-based, duplicates collapsed, unknown submitters = one source),
   applies thresholds (2 above IMMORTAL by default, per-rank overrides, waivers),
   applies the IMMORTAL-level fallback, merges trusted CSV and manual overrides,
   and writes `PlayerSummary`, `VerificationResult(+Sources)` and synchronised flags.
   Flags that stop applying are *resolved*, never deleted.
6. **Conversion** → **Export**: `Conversion` rows (mapping + flag filter, verification
   kept separate), `ExportBatch`/`ExportRow` with exact skip reasons; later changes
   mark exports stale.

## Concurrency model

* API requests run in FastAPI's threadpool; long work is a persisted `Job`
  executed by `JobRunner` worker threads (bounded queue, cancel, pause, checkpoint
  resume after restart).
* Every service opens short-lived SQLAlchemy sessions; sessions are never shared
  across threads. Writes on SQLite are serialised by an in-process lock so
  workers queue rather than fail with `database is locked`.
* Vision calls are I/O bound (threads); OCR is CPU bound and runs in the job
  worker; both are bounded by the worker count.

## Trust ladder

raw row → parsed candidate → validated image evidence → eligible observation →
confirmed rank (sources satisfied / CSV / waiver / manual override) → effective rank.
Provisional candidates, AI-flagged, out-of-date and low-confidence evidence stay
visible with their reasons at every level.
