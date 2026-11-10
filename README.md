# Rank Recovery

Recover historical Minecraft Java ranks from screenshots and confirmed purchase
records, resolve players to UUIDs, convert old ranks to new ranks, and export
LuckPerms commands. Evidence is never thrown away: every screenshot, every
reading, every purchase row, every manual decision and every flag stays in the
database with its provenance.

The system has three parts that share one codebase:

| Part | Runs on | What it does |
|---|---|---|
| **Server** (`rankrecovery-server`) | your dedicated server | REST API, database, extraction, reconciliation, lookups, exports, background jobs |
| **Discord bot** (`rankrecovery-bot`) | the server (or anywhere) | `/rank <name>` for players, `/submit` for staff screenshots, `/link` to bind Discord users to IGNs |
| **Desktop client** (`rankrecovery-client`) | your Windows PC | bulk screenshot submission, review of all six modes, settings, exports |

Hosting on a Minecraft panel (Bloom Host / Pterodactyl) is covered in
`docs/BLOOM_HOST.md`: a split server with a Python egg runs `main.py`, which
starts the API and the bot in one process.

Read `docs/ARCHITECTURE.md` for the design, `docs/FEATURES.md` for what is
implemented and the known limitations, `docs/DATABASE.md` for the database
choice, `docs/DEPLOYMENT.md` for the server, `docs/BOT.md` and `docs/CLIENT.md`
for the two front ends, `docs/API.md` for the HTTP API, and
`docs/NAME_HISTORY.md` for what name-history sources still exist and how a
join-time rank check would use them.

## Quick start (one machine, for trying it out)

```bash
python3.12 -m venv .venv && . .venv/bin/activate      # Windows: py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -e ".[server,vision,ocr,bot,client,dev]"
rankrecovery-admin init-db                             # creates data/rankrecovery.db (SQLite, WAL)
rankrecovery-admin create-key --name desktop --role admin   # prints the key once; keep it
rankrecovery-server --host 127.0.0.1 --port 8420
```

Then start the desktop client (`rankrecovery-client`), enter
`http://127.0.0.1:8420` and the key, and go to **Settings › Extraction** to
paste your Anthropic API key (stored in the database, never logged; the
`ANTHROPIC_API_KEY` environment variable on the server overrides it). Without a
key the server falls back to local Tesseract OCR, which is markedly worse on the
Minecraft font (see the limitations).

## Windows setup for the desktop client only

1. Install Python 3.12 from python.org (tick "Add to PATH").
2. In PowerShell:
   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[client]"
   rankrecovery-client
   ```
3. Enter the server URL (for example `https://ranks.example.com`) and the API key your server admin created with `rankrecovery-admin create-key --role admin`.

On a Mac, `brew install python@3.12` first; on macOS older than 14.4 also run
`pip install "PySide6>=6.8,<6.9"` after the install (see `docs/CLIENT.md`).

OCR dependencies are only needed on the **server** (where extraction runs):
`apt install tesseract-ocr tesseract-ocr-eng` on Debian/Ubuntu, or the UB
Mannheim installer on Windows (add it to PATH). AVIF support needs
`pip install pillow-avif-plugin` (included in the `avif` extra).

## Organising screenshots by submitter

The **submitter** is the player who *provided* a screenshot, not necessarily the
player shown in it. Submitters matter because ranks above IMMORTAL need
screenshots from **two different submitters** when the *Require 2 independent
submitters for ranks above IMMORTAL* checkbox is on (off by default: one clean
screenshot confirms any rank).

Folder import in the desktop client or CLI expects one subfolder per submitter:

```
input/
  Alice/      2025-03-14_18.22.01.png  tab2.png
  Bob/        photo.jpg
  unknown/    (anything you cannot attribute; counts as at most one source)
```

`rankrecovery-admin import-folder input --extract` imports with the subfolder
name as submitter. Screenshots submitted through Discord record the submitter
from the `submitter` option, or the caller's linked IGN, or their display name.

## Trusted purchase CSV

Confirmed purchase records are authoritative evidence for their rank (and, when
the checkbox *Treat trusted purchase CSV records as confirmed and exempt from
submitter verification* is on, exempt from the two-submitter rule). Columns are
auto-detected and editable in the preview: `username`, `uuid` (with or without
dashes), `rank` (old rank or seasonal alias such as SOLAR/COSMIC), optional
`purchase_date`, `transaction_id`/`order_id`. Each row needs a rank and at least
one of username or UUID. UTF-8 with BOM, `;` and tab delimiters are detected.
Examples: `examples/purchases_*.csv` (templates only, never imported).

Store exports work directly: a Tebex payments CSV maps `Username`, `Uuid`,
`Date`, `Transaction ID` and `Packages` automatically, only `Complete`
payments count (the accepted statuses are a setting), and the rank is read
from the package name: `'Galactic to Infinite'` proves INFINITE, `'Immortal
Rank', 'Fly Perk'` proves IMMORTAL. Packages that do not name a rank are
listed as unknown; map them once with `package-map add "<package>" <RANK>`
in the console (or the `csv.package_rank_mapping` setting), retire the batch
with `csv-retire <id>` and import the file again.

## Bulk submissions: the `newuploads` folder and Google Drive

The server creates a `newuploads` folder next to `main.py` (visible in the
panel's File Manager). Drag screenshots into it, then run `ingest` in the
console or `/rr ingest` in Discord: each readable image is submitted exactly
like a Discord upload, extraction is queued, and the file is removed from the
folder. Files that are not images stay put and are listed; files that cannot be
decoded move to `newuploads/rejected`. `drive <link>` (or `/rr drive`) pulls the
photos behind a Google Drive folder or file link that is shared with "Anyone
with the link" into the same folder and submits them. Without an API key the
Drive page lists at most 50 files per folder; set `drive.api_key` to a Google
API key with the Drive API enabled for bigger folders.

## Old usernames that have since changed

The UUID lookup (`lookup` in the panel console, `/rr lookup` in Discord, or
the Identity page of the desktop client) works through every player without a
UUID, skipping anyone who already has one, in two steps. Mojang is asked who holds the name today (ten names per
request; a 429 pauses every worker and the pause doubles each time the same
request is refused again); a hit is taken as the player and recorded, done. When nobody holds the name any more, crafty.gg's
public API is asked which accounts used it before, and the most recent holder
is taken; the record and an `IDENTITY_NAME_REUSED` flag list the other
accounts when there were several. Name reuse means this can hand a rank to the
wrong person occasionally; that trade-off is deliberate and reviewable, and
`lookup.history_pick = ambiguous` turns it back into a manual decision.
NameMC itself has no API and is behind an anti-bot wall, so it is not
scraped; crafty.gg records the same public Mojang data. The old server's own
files remain the most complete source: `usercache.json`, a LuckPerms or
Essentials export, or purchase records pair each historical name with its
UUID, and `identity-import` loads them. Conflicting records are flagged for
review rather than overwritten.

## Players who changed their name

A player types `/claim OldName` in Discord (their current name comes from
`/link`, or they pass it as the second option). The bot resolves the current
name to its account and records a pending claim; staff check it (`/rr claims`)
and approve (`/rr approve <id>`) or reject it. Staff can also link the two
names directly with `/rr link-old OldName NewName`, or `link-old` in the panel
console. On approval the old record gets the account's UUID and the new name
as an alias, so `/rank NewName` answers and the export targets the right UUID.
A claim that contradicts a verified UUID on file is shown with a warning and
needs `force` to apply.

## Proxies for UUID lookups

Optional. Paste or load a TXT list in **Settings › Lookup** (one proxy per line;
formats in `examples/proxies.example.txt`, including `user:pass@host:port`).
Credentials never appear in logs or lookup records; only `scheme://host:port`
does. Proxies are for your own rate-limit hygiene across your own machines;
respect Mojang's and GeyserMC's terms.

## Database

SQLite in WAL mode by default (`data/rankrecovery.db`): many readers, one writer
at a time, writes from workers serialised in-process. PostgreSQL is supported by
setting `RANKRECOVERY_DATABASE_URL`. H2 was evaluated and rejected (no SQLAlchemy
dialect). Details and the honest limitations are in `docs/DATABASE.md`.

## Running the checks

```bash
ruff check rankrecovery tests scripts
QT_QPA_PLATFORM=offscreen python -m pytest -q
python scripts/sample_accuracy.py --samples samples --extractor ocr      # or --extractor vision with ANTHROPIC_API_KEY set
```

## Repository layout

See `docs/ARCHITECTURE.md`. Short version: `rankrecovery/core` holds every
business rule (pure Python, fully tested), `rankrecovery/services` applies them
to the database, `rankrecovery/api` exposes them, `rankrecovery/bot` and
`rankrecovery/client` consume the API.
