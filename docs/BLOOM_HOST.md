# Running on a Bloom Host / Pterodactyl panel

A Minecraft hosting panel gives you one container per "server" with one
startup command. You cannot install Docker or run extra processes inside the
Minecraft server itself. The way to host the rank-recovery server and the
Discord bot on such a panel is a **split server** running a **Python egg**,
started with a single entry file (`main.py`) that runs the API and the bot in
one process.

Exact menu labels differ slightly between panels; the steps below describe
the standard Pterodactyl flow that Bloom's panel is built on. Where a label
differs, look for the equivalent.

## 1. Create the split server

1. Main server → **Server Split** (left menu) → create a new split.
2. Software/egg: choose a **Python** egg (often named "Python", "Python Generic",
   "Discord bot (Python)" or similar). If only Minecraft eggs are offered, ask
   Bloom support to enable a Python/Discord-bot egg for your plan.
3. Resources: give the split **at least 1 GB RAM** and **2 GB storage**
   (the vision extractor holds a few images in memory; OpenCV and the
   Anthropic SDK need ~500 MB of disk for packages). The main Minecraft
   server restarts while the split is created.
4. Note the split's **IP and port** (its own allocation, e.g. `104.204.222.125:25570`).
   The panel passes that port to the container as `SERVER_PORT`; the API binds to it.

## 2. Put the code on the split server

Either use the egg's **git repository** option (set it to
`https://github.com/danielburstein/rank_program` and branch
`claude/funny-gauss-s9ddqa` if the egg has a branch field), or download a
ZIP of the branch from GitHub and upload/extract it with the **File Manager**
so that `main.py`, `requirements.txt` and the `rankrecovery/` folder sit in
the root of the server's files.

## 3. Startup settings

On the split server's **Startup** page:

| Variable | Value |
|---|---|
| App/entry file (`PY_FILE`) | `main.py` |
| Requirements file (`REQUIREMENTS_FILE`) | `requirements.txt` |
| Extra pip packages (`PY_PACKAGES`) | leave empty |
| Python version | 3.12 (3.11 also works) |

The first start installs the requirements (several minutes) and then
starts the API. Watch the console for `API listening on 0.0.0.0:<port>`.

## 4. Configuration file

Create a file named `.env` in the root of the split server's files
(File Manager → New file) from `deploy/.env.panel.example`:

```
RANKRECOVERY_DATA_DIR=data
ANTHROPIC_API_KEY=sk-ant-...
RANKRECOVERY_BOT_DISCORD_TOKEN=
RANKRECOVERY_BOT_API_KEY=
RANKRECOVERY_BOT_GUILD_IDS=
RANKRECOVERY_BOT_STAFF_ROLE_IDS=
```

Leave the bot lines empty for now; the API starts alone and logs a warning.
Restart the split server after every `.env` change.

## 5. Create the API keys

Type in the panel console (section 8 lists all commands):

```
key create desktop admin
key create discord-bot staff
```

Each prints the key once. Alternative without the console: set
`RANKRECOVERY_ADMIN_KEY=<long random string>` in `.env`, restart, and create
the real keys from your PC with the HTTP API:

```bash
curl -X POST http://<split-ip>:<port>/api/v1/admin/keys -H "X-API-Key: <that string>" -H "Content-Type: application/json" -d '{"name":"desktop","role":"admin"}'
curl -X POST http://<split-ip>:<port>/api/v1/admin/keys -H "X-API-Key: <that string>" -H "Content-Type: application/json" -d '{"name":"discord-bot","role":"staff"}'
```

Then remove `RANKRECOVERY_ADMIN_KEY` from `.env` and restart.

## 6. Start the bot

Fill `RANKRECOVERY_BOT_DISCORD_TOKEN`, `RANKRECOVERY_BOT_API_KEY` (the staff
key), `RANKRECOVERY_BOT_GUILD_IDS` and `RANKRECOVERY_BOT_STAFF_ROLE_IDS` in
`.env` (see `docs/BOT.md` for where each value comes from) and restart the
split server. The console shows `Discord bot starting`; try `/rr status` in
Discord.

## 7. Connect the desktop client

Server URL `http://<split-ip>:<port>`, API key = the admin key.

**Security note.** The panel cannot terminate TLS for an arbitrary port, so
the API key travels in clear text between your PC and the split server. Use
a long random key, create a new key if you ever suspect it leaked, and keep
`.env` out of any shared place. If you want encryption, host the stack on a
small VPS behind a reverse proxy instead (see `docs/DEPLOYMENT.md`).

## 8. Panel console commands

The box at the bottom of the panel console is wired to the app. Type `help`
for the list; the most useful ones:

| Command | Does |
|---|---|
| `status` | counts, vision/model state, active jobs |
| `env` | which secrets are set (masked) and the bot/guild configuration |
| `vision test` | checks the Anthropic key and model with one cheap API call and says exactly what is wrong |
| `vision model claude-sonnet-5-5` | switch the vision model |
| `job 1` | a job with every per-image error message |
| `extract --force` | re-run extraction on every image (for example after fixing the key) |
| `player <name>` / `players [search]` | rank summary with the verification panel in text |
| `image <id>` | runs, detections and flags of one screenshot |
| `settings get/set/list` | read or change any setting (policy keys queue a recompute) |
| `key create <name> <role>` / `keys` / `key revoke <id>` | API keys without curl |
| `identity-import usercache.json` | link old names to UUIDs from the old server's records (upload the file with the File Manager first); `--create` also adds names not seen in screenshots |
| `names` | fetch the current Mojang name of every player with a known UUID |
| `lookup --all --force` | re-run UUID lookups for everyone (Mojang current names, Geyser for Bedrock) |
| `export preview`, `backup`, `logs 50 warning`, `stop` | |

## 9. Applying the export

The export is a TXT file of `lp user <uuid> parent set <group>` lines.
Paste them into the console of whichever server runs LuckPerms (the backend
server, or the Velocity proxy if LuckPerms lives there). Paste in chunks of a
few dozen lines; the panel console accepts one command per line.

## Limits on a panel

* Local OCR is unavailable (no Tesseract in the container); the vision
  extractor is used. Without an Anthropic key, uploads are stored but
  extraction jobs fail with "no extractor available".
* The database is the SQLite file under `data/`; back it up with the panel's
  **Backups** feature or the client's Settings › Database › Backup button
  (which writes into `backups/` inside the container; download it from the
  File Manager).
* RAM: the job runner defaults to two workers; set `RANKRECOVERY_JOB_WORKERS=1`
  in `.env` on a 1 GB split.
