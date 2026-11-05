# Deploying the server and bot

## 1. Docker Compose (recommended on the dedicated server)

```bash
git clone https://github.com/danielburstein/rank_program.git
cd rank_program/deploy
cp .env.example .env            # fill in the bot token later
docker compose up -d --build server
docker compose exec server rankrecovery-admin create-key --name desktop --role admin
docker compose exec server rankrecovery-admin create-key --name discord-bot --role staff
```

Put the two keys where they belong: the admin key in the desktop client, the
staff key in `.env` as `RANKRECOVERY_BOT_API_KEY` together with the Discord
token, then `docker compose up -d bot`.

The database lives in the `rankrecovery-data` volume (`/data/rankrecovery.db`).
Backups land in `deploy/backups`, exported TXT files in `deploy/exports` when
you give the client a server-side path; the client can also save the returned
text locally.

Expose port 8420 only through a reverse proxy with TLS (Caddy, nginx, Traefik)
if the desktop client connects over the internet. Example Caddyfile:

```
ranks.example.com {
    reverse_proxy 127.0.0.1:8420
}
```

## 2. Bare metal with systemd

```bash
sudo useradd -r -m -d /opt/rankrecovery rankrecovery
sudo -u rankrecovery bash -c 'cd /opt/rankrecovery && git clone https://github.com/danielburstein/rank_program.git src && python3.12 -m venv .venv && . .venv/bin/activate && pip install -e "src[server,vision,ocr,bot,avif]"'
sudo apt install tesseract-ocr tesseract-ocr-eng
sudo cp src/deploy/systemd/*.service /etc/systemd/system/
sudo cp src/deploy/.env.example /opt/rankrecovery/.env && sudo nano /opt/rankrecovery/.env
sudo systemctl enable --now rankrecovery-server rankrecovery-bot
```

## 3. Environment variables (server)

| Variable | Meaning |
|---|---|
| `RANKRECOVERY_DATA_DIR` | folder for the SQLite file (default `data`) |
| `RANKRECOVERY_DATABASE_URL` | full SQLAlchemy URL; use `postgresql+psycopg://…` for PostgreSQL |
| `RANKRECOVERY_HOST` / `RANKRECOVERY_PORT` | bind address (default 127.0.0.1:8420) |
| `RANKRECOVERY_JOB_WORKERS` | background worker threads (default 2) |
| `RANKRECOVERY_ADMIN_KEY` | optional bootstrap admin key; remove once real keys exist |
| `ANTHROPIC_API_KEY` | vision extractor key (overrides the key stored in settings) |

## 4. API keys and roles

`rankrecovery-admin create-key --name <who> --role <role>`

| Role | Can |
|---|---|
| `readonly` | look up players, dashboards, search, image views |
| `bot` | readonly + submit screenshots, create batches, Discord links, job status |
| `staff` | bot + review detections/flags, run extraction/recompute/lookup/rescan jobs, export preview |
| `admin` | everything: settings, overrides, waivers, merges, CSV import, export writing, backups, keys |

Give the Discord bot a `staff` key (its `/rr recompute` command needs it);
the desktop client needs `admin`.

## 5. Operations

* **Backups**: `rankrecovery-admin backup --dir backups` (online, safe while running) or the client's Settings › Database page. Restore with `rankrecovery-admin restore <file> --yes` after stopping the server.
* **Logs**: structured logs to stdout/journal; the last 2000 records are also available at `GET /api/v1/admin/logs` and in the client's Logs page. Secrets are redacted.
* **Jobs**: everything long-running is a job (`GET /api/v1/jobs`). Jobs interrupted by a restart are re-queued and resume from their checkpoint.
* **Upgrades**: `git pull && pip install -e . && systemctl restart rankrecovery-server`; migrations run on start-up.
* **PostgreSQL**: `pip install -e ".[postgres]"`, set `RANKRECOVERY_DATABASE_URL`, start the server; the same migrations apply.

## 6. Network requirements

* Server → `api.anthropic.com` (vision), `api.mojang.com` / `api.minecraftservices.com` (Java UUIDs), `api.geysermc.org` (Bedrock), optional proxies.
* Bot → `discord.com` gateway and the server API.
* Desktop client → the server API only.
