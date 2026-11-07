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
for the two front ends, and `docs/API.md` for the HTTP API.

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
