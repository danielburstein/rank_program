# HTTP API

Base path `/api/v1`. Authenticate with `X-API-Key: <key>` (or `Authorization: Bearer <key>`).
Interactive docs: `/docs` (Swagger UI) and `/openapi.json` on the running server.
Roles: readonly < bot < staff < admin (see DEPLOYMENT.md).

| Method & path | Role | Purpose |
|---|---|---|
| GET `/health` | none | version, database mode, vision configured |
| GET `/dashboard` | readonly | counts for the dashboard |
| GET `/players` | readonly | Mode 1 table (`search, limit, offset, sort, descending, only_stale, only_provisional, flag`) |
| GET `/players/by-name/{name}` | readonly | full detail for a username or alias (404 if unknown) |
| GET `/players/{id}` | readonly | full detail (summary, verification, observations, staff, flags, detections, images, csv rows, lookups, resolutions, merges, manual assertions, waivers, exports) |
| POST `/players/{id}/recompute` | staff | recompute summary + conversion |
| POST `/players/{id}/override-rank` `{rank, reason}` | admin | manual rank override (audited) |
| POST `/players/{id}/staff` `{staff, reason}` | admin | manual staff assertion |
| POST `/players/{id}/waivers` `{rank, reason}` / DELETE `/waivers/{id}` | admin | waive / revoke the submitter threshold for one rank |
| POST `/players/{id}/uuid` `{uuid, reason}` | admin | manual UUID |
| POST `/players/{id}/aliases` `{username}` | admin | add a known alias |
| POST `/players/{id}/review-status` | staff | set review status |
| POST `/players/{id}/conversion-approval` | admin | manual approval for Mode 2 inclusion |
| GET `/players/{id}/overlay/{image_id}` | readonly | PNG overlay highlighting that player |
| POST `/batches` (form) | bot | create an import batch |
| POST `/images` (multipart `files`, form `submitter, batch_id, original_paths, file_modified, discord_user_id, extract`) | bot | submit screenshots; returns outcomes and the extraction job id |
| GET `/images`, GET `/images/{id}`, GET `/images/{id}/content`, GET `/images/{id}/overlay?highlight=` | readonly | listing, full context, bytes, overlay |
| POST `/images/{id}/submitter` | staff | correct the submitter (audited) |
| POST `/images/{id}/ai-review`, `/images/{id}/date-review` | admin | manual AI / date decisions |
| GET `/submitters` | readonly | submitters with image counts |
| POST `/detections/{id}/review` `{accept, note}`, POST `/detections/bulk-review` | staff | accept/reject detections |
| POST `/observations/{id}/verify`, `/observations/{id}/deactivate` | staff / admin | verification and deactivation |
| GET `/flags?entity_type&entity_id`, GET `/flags/catalogue`, POST `/flags`, POST `/flags/{id}/resolve`, `/flags/{id}/reopen` | staff | structured flags |
| POST `/jobs`, GET `/jobs`, GET `/jobs/kinds`, GET `/jobs/{id}`, POST `/jobs/{id}/cancel|pause|resume` | staff / readonly | background jobs |
| POST `/extract` `{image_ids, force, extractor}` | staff | extraction job |
| POST `/recompute` `{player_ids, all, then_convert}` | staff | recompute job |
| POST `/csv/preview`, POST `/csv/import` (multipart + `mapping` JSON, `delimiter`, `reject_invalid`, `notes`) | admin | trusted purchase CSV |
| POST `/csv/manual-row`, GET `/csv/batches`, GET `/csv/batches/{id}/rows`, POST `/csv/rows/{id}/deactivate|reactivate` | admin / readonly | purchase rows |
| POST `/identity/lookup` `{player_ids, retry_failed, retry_proxy_failures, force, workers}` | staff | UUID lookup job (players with a UUID are skipped unless `player_ids` and `force` are given) |
| GET `/identity/review-queue?limit&offset&include_skipped&search` | staff | Names without a UUID for manual review, highest rank first, with aliases, why the lookup failed, candidate accounts and a NameMC link |
| POST `/identity/review-queue/{player_id}/uuid` `{uuid, note}` | staff | Set the UUID by hand; Mojang confirms the current name; recompute, conversion and `uuid_ranks` follow; returns `{uuid, current_name, verified, effective_rank, new_rank, ...}` |
| POST `/identity/review-queue/{player_id}/skip` `{reason}`, `.../unskip` | staff | Leave a name out of (or put it back into) the review queue |
| GET `/uuid-ranks?limit&offset&search&rank` | readonly | The `uuid_ranks` table: one row per resolved UUID with its highest rank, new rank, usernames and contributing records |
| GET `/uuid-ranks/export.csv` | readonly | The same table as CSV (list columns joined with `\|`) |
| POST `/uuid-ranks/rebuild` | staff | Rebuild the table from the current players and summaries (also happens after every lookup, extraction, recompute and conversion job) |
| GET `/ingest/folder` | staff | The `newuploads` folder path and the files waiting in it |
| POST `/ingest/folder` `{submitter, extract}` | staff | Job: submit every image in `newuploads` (and sub-folders) as if uploaded, remove the files, queue extraction; returns `{job_id, found, folder}`, the job's result is the ingest summary |
| POST `/ingest/drive` `{url, submitter, ingest}` | staff | Job: download a shared Google Drive folder/file into `newuploads` and (by default) submit it |
| GET `/identity`, POST `/identity/merge/preview`, POST `/identity/merge`, POST `/identity/unmerge/{id}`, GET `/identity/proxies` | staff / admin | identity table and merges |
| POST `/conversion/run[?sync=true]`, GET `/conversion/preview?included=` | staff / readonly | Mode 2 |
| GET `/export/preview`, POST `/export/write` `{path, staff_warning_acknowledged, options}`, GET `/export/history`, GET `/export/{id}` | staff / admin | Mode 4 |
| POST `/search` (SearchBody) | readonly | Mode 5 universal search |
| GET `/audit?entity_type&entity_id` | readonly | audit log |
| POST `/rescan/candidates`, POST `/rescan`, POST `/rescan/manual-correction` | staff | Mode 6 |
| GET `/settings`, PUT `/settings` `{values, reason}`, POST `/settings/reset`, GET `/settings/history`, GET `/settings/policy`, GET `/settings/policy-versions` | staff / admin | persistent settings; policy changes mark summaries stale and queue a recompute |
| POST `/discord/link`, GET/DELETE `/discord/link/{discord_user_id}` | bot | Discord ↔ IGN links |
| POST `/admin/keys`, GET `/admin/keys`, DELETE `/admin/keys/{id}` | admin | API keys |
| POST `/admin/backup`, GET `/admin/backups`, POST `/admin/restore` | admin | SQLite backups |
| GET `/admin/logs?limit&level` | staff | recent log records (secrets redacted) |

Errors use standard HTTP codes: 400 validation/policy error, 401 missing key,
403 insufficient role, 404 unknown entity, 409 export warning not acknowledged.
