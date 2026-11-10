# Name history: can we check a joining player's old names?

Research notes, 9 November 2026. Every endpoint below was called live from the
development sandbox on that date; results may change.

## The question

When a player joins the new server we know their UUID and current name. We want
every name that UUID has ever used, so each old name can be checked against the
screenshot evidence and the player handed the rank. Collisions (someone else
later took an old name) are acceptable.

## What exists in 2026

| Source | Old names for a UUID? | Account(s) that once held an old name? | Key needed | Rate limit seen | Status |
| --- | --- | --- | --- | --- | --- |
| Mojang `api.mojang.com/user/profiles/{uuid}/names` | removed 13 Sep 2022 | no | – | – | HTTP 404 today |
| **crafty.gg** `GET https://api.crafty.gg/api/v2/players/{uuid or name}` | **yes** (`usernames[]`, `changed_at`, `name_changes`) | – | none | `x-ratelimit-limit: 150` | works |
| **crafty.gg** `GET https://api.crafty.gg/api/v2/usernames/{name}` (not in the Redocly page, served by the same API) | – | **yes**: `current_player` plus `historical_players[]` with `uuid`, current `username` and `changed_at` (the date that account took the name); 404 "Username not found" for names it never saw | none | same 150 budget | works, free names included |
| **laby.net** `GET https://laby.net/api/v3/user/{uuid}/names` | **yes** (`name`, `changed_at`, `accurate`, `last_seen_at`) | no: every search endpoint answers HTTP 428 "Challenge token required" | none for `/names` | `x-ratelimit-limit: 10`, `retry-after` ≈ 3.5 min | works, tight limit |
| laby.net `get-previous-accounts`, `search/names`, `get-snippet`, `v3/.../profile` | – | would be, but gated | anti-bot token laby.net does not hand out | – | blocked (428) |
| NameMC | web page only, behind a Cloudflare challenge | web page only | no API | – | not usable by a program |
| PlayerDB `playerdb.co/api/player/minecraft/{name}` | `name_history` is `[]` even for renamed accounts | no | none | – | no history |
| Hypixel API `knownAliases` | removed Sep 2022 (PublicAPI discussion #561) | no | key | – | gone |

Cross-check on one account with five renames (UUID `289a7ee7-…`): crafty.gg
and laby.net returned the same six names and the same change dates
(ted187 → Ben187s → TitanSlayer_ → DeltaNinja → Vantrell → DeltaNinja). The
two sites are independent caches of what Mojang published before 2022 plus
what they have observed since, so an account neither site saw before September
2022 can have gaps. laby.net marks each entry `accurate: true/false`.

crafty.gg publishes its API (https://crafty.gg/public-api, contact
robert@crafty.gg). The docs mark every route "Bearer" but anonymous calls work;
a token would only matter for a higher limit. laby.net's v3 `names` route is
not documented publicly and is the only laby.net route that answered without
the challenge token, so treat it as a courtesy: low volume, cached, honest
User-Agent.

## What this means

* **UUID → old names works, with no API key**, from two independent sources.
  That is exactly the direction a join-time check needs.
* **Old name → account works through crafty.gg's `usernames` route.** For a
  name nobody holds today it lists every account that used it, with the date
  each took it, so the most recent holder can be picked. laby.net has the same
  data but gates it; NameMC has no API and sits behind a Cloudflare challenge,
  so it is not scraped. Coverage follows what crafty.gg observed, mostly
  accounts seen before September 2022 plus what it recorded since.
* For the common case the player never renamed, so the current name already
  matches an alias. Name history only matters for the minority who renamed.

## What is implemented (10 November 2026)

The UUID lookup job (`lookup` in the console, `/rr lookup`, the client's
Identity page, `POST /identity/lookup`) now does, for each player without a
UUID:

1. Mojang current-name lookup. A hit is recorded as the player's UUID and the
   lookup stops there (`lookup.history_mode = fallback`).
2. Otherwise crafty.gg `usernames/{name}` (`lookup.historical_provider =
   crafty`). One previous holder resolves. Several resolve to the most recent
   holder (`lookup.history_pick = most_recent`); the others are kept in the
   resolution note and an `IDENTITY_NAME_REUSED` info flag so staff can see
   and undo a collision. `history_pick = ambiguous` restores the manual
   decision; `history_mode = always` cross-checks even names Mojang resolves.
3. Nothing anywhere: `UUID_LOOKUP_HISTORICAL_NAME_UNRESOLVED` as before.

Rate: Mojang is asked ten names per request (`lookup.bulk_lookups`), so a
list of eighty names costs eight calls; crafty.gg gets one call per name
nobody holds, capped at `lookup.crafty_requests_per_minute` (60). Answers are
cached for `lookup.cache_ttl_hours`. A 429 from either provider pauses every
worker for the `Retry-After` the provider sends, or
`lookup.rate_limit_cooldown_seconds` when it sends none.

## Old-server files beat any website

If the old server's files still exist, they hold the exact UUID ↔ name pairs
for everyone who ever joined, with no rate limit and no gaps:

* **LuckPerms**: table `luckperms_players (uuid, username, primary_group)` in
  MySQL/MariaDB/PostgreSQL, or the H2 file under `plugins/LuckPerms/`
  (`luckperms-h2-v2.mv.db`; older builds `luckperms-h2.mv.db`). `primary_group`
  is the rank itself. `lp export` on that server writes the same data as JSON.
* **EssentialsX** `plugins/Essentials/userdata/<uuid>.yml`: last account name
  plus a list of past usernames.
* **usercache.json**: name, uuid, `expiresOn`; only the roughly 1000 most
  recent players and entries expire after a month, so it is thin on its own.
* Vanilla `world/playerdata/<uuid>.dat` has no username.

`POST /identity/import` already accepts `usercache.json` and `username,uuid`
CSVs; a LuckPerms export or `luckperms_players` dump can be fed the same way.

## Proposed design: join-time check

1. **Paper plugin** (small, Java 21). On `AsyncPlayerPreLoginEvent` (runs off
   the main thread, exposes `getUniqueId()` / `getName()`) it calls the backend
   `POST /join/check` with `{uuid, name}` and a server API key, then on the
   main thread runs `lp user <uuid> parent set <group>` when the answer says
   so. Nothing inbound to the Minecraft server is needed; the backend already
   listens on the panel.
2. **Backend `POST /join/check`**, match order, strongest first:
   1. a player whose canonical UUID equals the joining UUID (trusted CSV,
      server-records import, approved claim);
   2. an alias equal to the current name;
   3. the UUID's name history from crafty.gg, then laby.net as a second
      opinion, cached per UUID for 30 days and stored as aliases with source
      `name_history`; each former name is matched against the aliases;
   4. Bedrock (Floodgate UUIDs `00000000-0000-0000-…`): current gamertag alias
      only, since neither site covers Xbox accounts.
   The response carries the rank, the LuckPerms group, and which rule matched.
   Matches from rule 3 get an info flag so staff can see and undo collisions.
3. **Join grants table**: one row per UUID and group, so the plugin applies a
   rank once, staff can list what was handed out, and a wrong grant can be
   reversed with one command.
4. **Rate budget**: one crafty.gg call per new UUID, laby.net only when crafty
   returns a single name or an error; both cached. 150 and 10 requests per
   window are more than enough for a server seeing tens of new accounts a day.

Fallback without a plugin: the backend polls `list uuids` over RCON every
30 seconds and sends the LuckPerms command the same way. It needs the RCON port
exposed on the panel and adds a short delay after join, so the plugin is the
better route.

## Sources

* Minecraft Wiki, Mojang API change log: name-history endpoint removed
  13 September 2022. https://minecraft.wiki/w/Mojang_API
* crafty.gg Public API reference. https://crafty.gg/public-api
* Hypixel forum thread and PublicAPI discussion #561 on `knownAliases`
  removal. https://hypixel.net/threads/did-knownaliases-get-removed-from-the-hypixel-api.5098206/
* Unofficial NameMC API docs, archived and marked discontinued; NameMC's only
  public endpoint is the friends list. https://github.com/bribes/Unofficial-NameMC-API-Docs
* LuckPerms H2 schema (`{prefix}players`). https://github.com/LuckPerms/LuckPerms/blob/master/common/src/main/resources/me/lucko/luckperms/schema/h2.sql
* Paper javadoc, `AsyncPlayerPreLoginEvent`: "This event is asynchronous, and
  not run using main thread." https://jd.papermc.io/paper/1.21.4/org/bukkit/event/player/AsyncPlayerPreLoginEvent.html
