# Desktop client

The desktop client is the program that runs on your own computer. It never
opens the database: everything goes through the server's HTTP API with an
API key, so the same server can be used by the Discord bot and by several
staff members at once.

## Install on Windows

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[client]
rankrecovery-client
```

On first start a connection dialog asks for the server URL (for example
`http://your-server:8420`) and an API key with the `admin` role. Create the key
on the server with:

```bash
rankrecovery-admin create-key --name "daniel-desktop" --role admin
```

The key is saved in plain text in `~/.rankrecovery/client.json` (owner-only
file permissions where the OS supports them). Delete that file to forget it.

## Submitting screenshots

Use **Submit screenshots**. Either drag files or folders onto the drop area,
or use *Add files* / *Add folder*. The recommended folder layout is one
subfolder per submitter:

```
screenshots/
  Alice/
    2025-03-14_18.22.01.png
  Bob/
    tab.png
```

With *Use subfolder names as submitter* enabled, every file is recorded under
the player who provided it. Files dropped loose use the *Submitter* box; leave
it empty and the screenshot is recorded with an unknown submitter (all unknown
submitters together count as at most one source for high-rank verification).

Uploads go up in groups of ten files; identical content is detected by hash
and never processed twice. *Extract after upload* starts the extraction job
on the server and the page follows its progress.

## The six modes

* **Mode 1 · Players** – the main table (every column the migration needs,
  right-click the header to hide columns) with an inspector: summary and
  reasoning, the verification panel (each candidate rank, its `n/m` sources,
  every evidence image with its submitter and the reason it did or did not
  count), observations colour-coded as automatic, manual/verified or trusted
  CSV, staff evidence, flags (active vs resolved), CSV rows, identity (UUID,
  lookups and their failure codes, manual UUID, aliases, merges), manual
  actions, images with overlay, exports and the audit trail.
* **Screenshots** – browse every image, see the metadata that produced an
  AI or date flag, accept/reject detections in bulk, correct the submitter,
  review AI indicators and capture dates (all with a required note).
* **Trusted CSV** – preview a purchase CSV (auto-detected header mapping you
  can change, per-row validation, duplicates), import it, deactivate or
  reactivate rows, or add a manual purchase row.
* **Mode 2 · Conversion** – choose which flag categories are allowed to pass,
  save presets, run the conversion and read the exact reason for every
  excluded player. Allowing a flag never bypasses a failed verification.
* **Mode 3 · Identities** – run UUID lookups (all unresolved, failed only,
  proxy failures only, selected, forced), see provider/proxy failure codes.
* **Mode 4 · Export** – preview the LuckPerms commands and the skipped
  players with reasons; acknowledge the warning; choose where to save the
  TXT file. The server also records every export so later changes can mark
  it outdated.
* **Mode 5 · Search** – search every table with the filters from the
  specification and jump to the player or image behind any result.
* **Mode 6 · Rescan** – find a username in indexed text, run a targeted or
  full rescan, and record a manual correction by drawing the username and
  prefix boxes on the screenshot.
* **Settings** – every persisted setting with real checkboxes, the rank /
  staff / mapping editors, verification thresholds, the vision API key
  (stored on the server, never shown again), proxies, backups and restore.
* **Jobs & logs** – job progress, cancel/pause/resume, error inspection and
  the server log tail.

## What a manual correction means

A correction saved in Mode 6 is attached to the screenshot it was read from
and counts **under that screenshot's submitter**. It fixes what the
extractor misread; it never creates a second independent submitter, so a rank
above IMMORTAL still needs evidence from two different people (or a trusted
purchase record, or an explicitly audited waiver).

## Export warning

`lp user <uuid> parent set <group>` replaces a player's existing parent
groups in LuckPerms, including staff groups. The export page shows which
included players have staff evidence and refuses to write the file until the
warning is acknowledged. The client never runs commands against a server.
