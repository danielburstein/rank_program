# Discord bot

The bot is a thin client of the Rank Recovery server. It never touches the
database directly: every command calls the server's HTTP API with a bot API
key, so the bot can run on the dedicated server next to the API or anywhere
that can reach it.

## What it does

| Command | Who | What |
|---|---|---|
| `/rank <username>` | everyone (or staff only when `PUBLIC_LOOKUP=false`) | Rank summary for a historical username: effective old rank, highest observed, all observed ranks, mapped new rank, staff/custom prefixes, provisional candidates with `n/m sources`, evidence counts, identity, friendly notes. Staff additionally see export eligibility and raw flag codes. |
| `/myrank` | everyone | Same, for the caller's linked username. |
| `/link <username> [member]` | everyone / staff | Links the caller's Discord account to a Minecraft username (unverified). Staff can link another `member`; that link is stored as verified. |
| `/unlink` | everyone | Removes the caller's link. |
| `/claim <old_name> [current_name]` | everyone | "That old username in the screenshots is me." The current name (default: the caller's `/link` name) is resolved to its account right away; staff approve or reject the claim. |
| `/rr claims`, `/rr approve <id> [note] [force]`, `/rr reject <id> <reason>` | staff | Review claims. Approval gives the old-name record the current account's UUID and name, merges any separate record of the new name, and recomputes the rank. |
| `/rr link-old <old_name> <current_name> [note] [force]` | staff | Same link applied immediately, without a claim. |
| *User context menu* → **Rank lookup** | everyone | Rank summary for a member's linked username. |
| `/submit screenshot1 [screenshot2..5] [submitter] [note]` | staff | Uploads up to five screenshots to the server, starts extraction and reports progress. |
| *Message context menu* → **Submit screenshots** | staff | Takes every image attachment on the right-clicked message and submits it; the message author's linked name (or display name) is the default submitter. |
| `/rr status` | staff | API health, whether the vision model is configured, dashboard counts. |
| `/rr job <id>` | staff | Progress and errors of a processing job. |
| `/rr recompute <username>` | staff | Recomputes one player's summary from stored evidence and shows it. |
| `/rr lookup [retry_failed] [force]` | staff | Queues the UUID lookup for every player still missing one (players with a UUID are skipped): Mojang first, ten names per request, then the most recent account that held the name according to crafty.gg. Reports the job number for `/rr job`. |
| `/rr unresolved [count] [include_skipped]` | staff | Shows up to five names that have no UUID, highest rank first, each with a NameMC search link and two buttons: **Set UUID** opens a form to paste the UUID (Mojang then confirms the account's current name and the rank is recomputed), **Skip** keeps a plainly misread name out of the queue. |
| `/rr setuuid <username> <uuid> [note]`, `/rr skipname <username> [reason]` | staff | The same two actions as plain commands. |
| `/rr uuidranks` | staff | Rebuilds the `uuid_ranks` table (one row per resolved UUID with its highest rank) and reports the counts. |
| `/rr ingest [submitter]` | staff | Submits every screenshot dropped into the server's `newuploads` folder and its sub-folders (panel File Manager, drag and drop), removes the processed files and emptied sub-folders, skips folders named `rejected`, and queues extraction. |
| `/rr drive <link> [submitter]` | staff | Downloads the photos behind a Google Drive folder or file link shared with "Anyone with the link" into `newuploads`, then submits them. |
| `/rr find <text>` | staff | Searches players by name, alias or UUID fragment. |

"Staff" means a member holding one of the configured staff/admin role ids,
or a guild administrator. Commands in DMs are never treated as staff.

## Submitter semantics (read this)

Every screenshot is stored with a **submitter**: the player who *provided*
the screenshot, not necessarily the player shown in it. The server's
high-rank rule (optional, off by default) requires screenshots from **two different submitters** for
ranks above IMMORTAL, and all images from one submitter count as a single
source, so the submitter must be recorded truthfully:

* `/submit` defaults the submitter to the staff member's linked username
  (or their Discord display name). When you are forwarding screenshots that
  another player sent you, pass `submitter:<their username>`.
* The message context menu defaults to the **message author's** linked name
  or display name, which is right when a player posts their own screenshots
  in a channel.
* Every reply states `Recorded submitter: <name>`; if it is wrong, correct it
  in the desktop client (Images → set submitter), which writes an audit entry.
* A duplicate of an already-known image never adds a source, even when it
  comes from a different submitter; the reply says "counts once".

## Creating the Discord application

1. Open <https://discord.com/developers/applications>, **New Application**.
2. **Bot** tab → **Reset Token** → copy the token (this is `RANKRECOVERY_BOT_DISCORD_TOKEN`). Keep it secret.
3. **Privileged Gateway Intents**: none are required. (Server Members Intent is
   not needed; leave Message Content off, slash commands and context menus do
   not use it.)
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; bot
   permissions `Send Messages`, `Embed Links`, `Attach Files`, `Read Message
   History`. Open the generated URL and add the bot to your server.
5. Get your server id and the staff role ids: enable **Developer Mode**
   (User Settings → Advanced), then right-click the server → *Copy Server ID*
   and Server Settings → Roles → right-click a role → *Copy Role ID*.

## Server-side key

Create a bot key on the server (role `bot` can submit screenshots, look up
ranks and manage links; it cannot change settings or review evidence):

```bash
rankrecovery-admin create-key --name discord-bot --role bot
```

## Configuration

All values come from environment variables or a `.env` file in the working
directory, prefix `RANKRECOVERY_BOT_`:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `RANKRECOVERY_BOT_DISCORD_TOKEN` | yes | | bot token |
| `RANKRECOVERY_BOT_API_KEY` | yes | | server API key with role `bot` |
| `RANKRECOVERY_BOT_API_BASE_URL` | | `http://127.0.0.1:8420` | server URL |
| `RANKRECOVERY_BOT_STAFF_ROLE_IDS` | | empty | comma-separated or JSON list of role ids allowed to submit and use `/rr` |
| `RANKRECOVERY_BOT_ADMIN_ROLE_IDS` | | empty | additional staff roles |
| `RANKRECOVERY_BOT_GUILD_IDS` | | empty | sync commands to these servers instantly; empty = global sync (can take up to an hour to appear) |
| `RANKRECOVERY_BOT_MAX_ATTACHMENTS_PER_SUBMIT` | | 10 | cap per submission (context menu) |
| `RANKRECOVERY_BOT_MAX_ATTACHMENT_MB` | | 25 | per-file size cap |
| `RANKRECOVERY_BOT_LOOKUP_COOLDOWN_SECONDS` | | 5 | per-user cooldown for `/rank` (staff exempt) |
| `RANKRECOVERY_BOT_PUBLIC_LOOKUP` | | true | allow non-staff lookups |
| `RANKRECOVERY_BOT_EPHEMERAL_REPLIES` | | false | make rank replies visible only to the caller |
| `RANKRECOVERY_BOT_LOG_LEVEL` | | INFO | |
| `RANKRECOVERY_BOT_API_TIMEOUT_SECONDS` | | 30 | normal request timeout |
| `RANKRECOVERY_BOT_UPLOAD_TIMEOUT_SECONDS` | | 300 | upload timeout |
| `RANKRECOVERY_BOT_JOB_POLL_INTERVAL_SECONDS` / `_MAX_SECONDS` | | 3 / 120 | how long a submission reply keeps updating |

Example `.env`:

```
RANKRECOVERY_BOT_DISCORD_TOKEN=...
RANKRECOVERY_BOT_API_KEY=rr_...
RANKRECOVERY_BOT_API_BASE_URL=http://127.0.0.1:8420
RANKRECOVERY_BOT_GUILD_IDS=123456789012345678
RANKRECOVERY_BOT_STAFF_ROLE_IDS=234567890123456789,345678901234567890
```

## Running

```bash
pip install -e ".[bot]"
rankrecovery-bot
```

A systemd unit and a docker-compose service are provided under `deploy/`.
Logs redact the token and the API key.

## How a submission flows

1. The bot validates attachments (png/jpg/jpeg/webp/bmp/tiff/tif/gif/avif,
   size cap) and downloads them from Discord.
2. `POST /api/v1/batches` creates an import batch (`source=bot`, label with
   the Discord user and message id) with the submitter.
3. `POST /api/v1/images` uploads the files with `submitter`, `batch_id`,
   `discord_user_id` and `extract=true`. The server hashes each file, records
   metadata, AI indicators, date evidence and duplicates, and queues an
   extraction job for new images.
4. The reply lists each file (imported / duplicate / error, with AI and date
   notes) and the job id, then updates every few seconds until the job
   finishes or two minutes pass; `/rr job <id>` shows it later.
5. Extraction (vision model, or local OCR when no API key is configured)
   creates detections and observations; the server recomputes affected
   players, so `/rank` reflects the new evidence as soon as the job completes.

## Caveats

* Rank replies show what the server has *confirmed* versus what is
  *provisional*; a provisional candidate is never presented as the player's
  rank.
* Global command sync can take up to an hour; set `GUILD_IDS` for instant
  availability during setup.
* The bot does not resolve UUIDs or export commands; those are admin actions
  in the desktop client or API.
