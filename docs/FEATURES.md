# Feature inventory and limitations

## Implemented

**Evidence capture (Mode 1)**
- Import single files, folders (recursive, one subfolder per submitter), Discord attachments; PNG/JPG/WEBP/BMP/TIFF/GIF/AVIF (AVIF via `pillow-avif-plugin`); animated sources sample frames.
- sha256 exact duplicates (same content imported twice → one image, both paths kept, flagged when re-submitted by someone else); perceptual-hash duplicate groups and near-duplicate review flags.
- Full metadata capture (EXIF, XMP, PNG text, GIF comments, C2PA markers) with per-field reasons for AI and date classification.
- AI indicators: metadata = `AI_INDICATOR_FOUND` (ineligible until manually cleared), filename pattern = `AI_FILENAME_INDICATOR` (weak, informational unless the policy says otherwise), absence = `NO_AI_INDICATOR_FOUND` (never "proven authentic").
- Date policy: embedded capture date › Minecraft/phone filename timestamp › (optionally) file mtime; import time never; `OUT_OF_DATE` with the exact source and reason, `DATE_UNKNOWN` otherwise; configurable cutoff (default 2026-01-01) and exclusion toggle; manual date review.
- Vision extractor (Anthropic Messages API): region detection, per-column crops, nearest-neighbour upscaling, strict JSON schema per row, raw responses stored, token usage recorded, result cache by image hash + settings, refusal/truncation surfaced, server-side refusal fallback enabled.
- Local OCR fallback (Tesseract, optional EasyOCR) with Minecraft-oriented preprocessing and icon-blob classification; used automatically when no API key is configured.
- Parser rules: diamonds/∞/☯ icons, seasonal aliases before custom prefixes, SCUFFED custom prefixes (with and without ☯ evidence), staff prefixes incl. SR MOD/SRMOD etc., trailing ☯/∞ after staff names, chat colon rule, join/mention/system-message exclusion, scoreboard lower credibility, Bedrock dot-names, partial (cut-off) names, probable misreads to review (HER0, Nod, Ouner), ambiguous ties to review without guessing, IMMORTAL vs IMMORTAL+ and ADMIN vs ADMIN+ never fuzzed.
- Every detection stored with bounding boxes, confidence, status and flags; overlays rendered server-side.

**Reconciliation**
- All observations kept; summary lists observed ranks (ordered), highest observed, highest confirmed, effective rank, provisional candidates with `n/m` progress, staff ranks, custom prefixes, counts, reasoning.
- Two-independent-submitter rule above IMMORTAL (checkbox, default off), per-rank thresholds, unknown submitters = one source, duplicate content never adds a source, same image/pass/appearance counted once, IMMORTAL-level fallback for otherwise-eligible single-source high ranks, 0/2 explanations when all evidence is AI/date/confidence-excluded.
- Trusted CSV exemption checkbox (default on); CSV rows count as one ordinary source when the exemption is off.
- Audited waivers (never automatic), manual rank overrides, manual staff assertions, manual verification of detections (parsing reliability only; never adds a source).
- Flags as rows with severity, details, evidence reference, resolution history; recompute resolves (never deletes) flags that stop applying.

**Mode 2** conversion with editable mapping (exact spelling), DEFAULT deliberately unmapped, flag-category checkboxes with Select All/Clear All/presets, resolved-flag policy (auto-resolved flags never block; human-resolved flags block unless allowed), verification kept separate from flag inclusion, exact skip reasons, stale tracking.

**Mode 3** Mojang current-name lookup (ten names per request through the bulk endpoint, single lookups only for what it could not answer, a shared cool-down for every worker when a provider answers 429 that doubles on each repeat and never counts against the technical-failure retry budget; players with a UUID are skipped), Minecraft Services alternative, Geyser/Floodgate for Bedrock names (Java endpoint never used for them), crafty.gg previous-holder lookup as the fallback for names nobody holds today (most recent holder taken, others flagged; laby.net and a generic adapter configurable), proxies with rotation/sticky/direct modes and safe identifiers, bounded retries with jitter, every attempt persisted with the exact failure code, NOT_FOUND distinguished from technical failures, retry all/failed/proxy-failures, manual UUIDs and aliases, auto-merge only on identical verified UUIDs, merge preview/undo, identity conflicts flagged for review.

**Parallel extraction** one extraction job reads `extraction.vision_concurrency` images at a time (default 4) and, inside each image, `extraction.vision_region_concurrency` crops at a time (default 3), all under one cap of `extraction.vision_max_concurrent_calls` model calls in flight (default 8); an image the API rate-limits waits 15s, 30s, 60s... and is retried `extraction.rate_limit_retries` times. None of these change the settings fingerprint, so tuning them never re-reads finished images.

**Bulk submissions** the `newuploads` folder on the server (drop files, run `ingest`/`/rr ingest`; processed files are removed, unreadable ones moved to `rejected/`) and Google Drive links (`drive <link>`/`/rr drive`: the Drive API with `drive.api_key`, else gdown's 50-file page).

**Mode 4** LuckPerms export (`lp user <uuid> parent set <group>`), one command per UUID, preview with skip reasons, staff-group warning that must be acknowledged, history with stale marking when evidence or policy changes.

**Mode 5** universal search over players, images, detections, CSV rows and lookup attempts with the filters listed in the spec; audit log; manual add/edit/deactivate for observations, flags, purchase rows, aliases, UUIDs, merges.

**Mode 6** candidate search over indexed text and detections (fuzzy, confusion-aware), scoped rescans (all/player/flagged/batches/submitters/images), full rescan, `NAME_FOUND_RANK_UNREADABLE` findings with image and box, manual corrections saved as superseding records under the screenshot's submitter, downstream recompute and stale-export marking.

**Platform** FastAPI server with hashed API keys and roles, persisted resumable jobs, versioned settings with history and policy snapshots, SQLite WAL + writer lock or PostgreSQL, Alembic migrations, backup/restore, Discord bot, PySide6 client, CLI, Docker/systemd files, 180+ automated tests.

## Limitations (please read)

1. **Local OCR accuracy is poor on the Minecraft font.** With the stock Tesseract English model the sample set scores 2 of 88 rows by exact username (prefixes are read better than names; phone photos of monitors are worst). The vision extractor is the intended production path; it could not be exercised in the build environment (no API key, no network), so run `scripts/sample_accuracy.py --extractor vision` with your key as the first thing and tune `extraction.*` settings. A Minecraft-font `tessdata` or EasyOCR/PaddleOCR would improve OCR and can be added behind the same interface.
2. **Old names are matched to accounts by public name-history records, which can point at the wrong person.** Mojang removed its name-history endpoint in 2022 and NameMC has no API, so the fallback for a name nobody holds today is crafty.gg's `usernames` endpoint, which lists the accounts that used the name and when each took it (laby.net has the same data but gates it behind a "challenge token"; its provider is kept for anyone laby.net grants access to). The most recent holder is taken automatically, every other holder is recorded on the player and an `IDENTITY_NAME_REUSED` flag, and `lookup.history_pick = ambiguous` makes staff decide instead. A Mojang hit is final by default (`lookup.history_mode = fallback`); `always` cross-checks every name against history. Coverage follows what crafty.gg observed: accounts it never saw before September 2022 can have gaps, so the old server's own records (`usercache.json`, LuckPerms or Essentials exports) and trusted purchase UUIDs remain the stronger sources and are imported with the identity import.
3. **Provider endpoints are configurable and must be verified against current documentation** (the Mojang and GeyserMC wikis were unreachable from the build environment): Mojang `GET /users/profiles/minecraft/{name}`, Minecraft Services `GET /minecraft/profile/lookup/name/{name}`, Geyser `GET /v2/utils/uuid/bedrock_or_java/{name}?prefix=.`. Geyser only resolves Bedrock players that have passed through Geyser's cache; others need a manual Floodgate UUID.
4. **AI detection is metadata-based suspicion only.** Missing metadata proves nothing; the UI says `NO_AI_INDICATOR_FOUND`, never "authentic".
5. **SQLite serialises writes.** Many readers, one writer; worker writes queue in-process. Use PostgreSQL for parallel writers.
6. **Region detection on photos of monitors is heuristic**; the vision extractor also asks the model for the layout, OCR relies on heuristics alone.
7. **Discord global command sync can take up to an hour**; set `RANKRECOVERY_BOT_GUILD_IDS` for instant per-guild sync.
8. The Discord bot and the desktop client were verified structurally and with mocked HTTP; neither could be connected to a live Discord gateway or a display in the build environment (the client is exercised headless with `QT_QPA_PLATFORM=offscreen`).
9. Secrets stored in the settings table (API key, proxy list) are stored as-is in the database file; protect the data directory with file permissions, or prefer environment variables on the server.
