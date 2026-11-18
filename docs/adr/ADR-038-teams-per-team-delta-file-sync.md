# ADR-038: Teams file sync uses one drive-delta call per team with parent-id routing

## Status: Accepted

## Context

The Teams integration syncs two content types from a Microsoft Team
into the workspace's knowledge base: **messages** and **files**.
Message sync has always used Graph's channel message `delta` API and
incremental cross-cycle deduplication. File sync did not.

Prior to this ADR, `_sync_channel_files` in `teams-sync/handler.py`
called `list_folder_files` per channel every scheduled cycle. Each
call enumerated every file in the channel's SharePoint folder and
unconditionally queued every returned file to file-ingestion, which
then unconditionally downloaded from the pre-authenticated URL and
put to S3, which then triggered process-documents and re-embedded
the file in the vector database. Every hourly sync therefore
re-processed every file whether or not anything had changed.

Every other Microsoft-Graph-backed source in the platform already
used incremental sync:

- **SharePoint files** — `get_drive_delta` with a per-drive
  `deltaLink` persisted on the connection row.
- **SharePoint pages** — client-side `lastModifiedDateTime >
  last_sync_at` filter.
- **Teams messages** — Graph message delta API with per-channel
  `delta_links`.
- **OneDrive** — `get_drive_delta`.
- **OneNote** — content arrives inline; no re-download surface.

Only Teams files remained a full enumeration + full upload every
cycle. At enterprise-scale (hundreds of files per team across
hundreds of teams) the wasted Graph API calls, S3 PutObject charges,
and process-documents embedding recomputation added up.

## Decision

Teams file sync is now **per-team drive-delta with parent-id
routing** and a **two-layer defense**:

- **Layer 1 (teams-sync):** one `get_drive_delta` call per team,
  not per channel. Each Microsoft Team is backed by a single
  Microsoft 365 Group with one default SharePoint document library
  (one drive); every channel is a folder within that drive. One
  delta call covers every channel's files.

- **Layer 2 (file-ingestion):** an `eTag` pre-flight check in
  `process_teams_file`. The item's Graph `eTag` is threaded through
  the SQS body; before downloading, `_teams_file_unchanged` calls
  `head_object` on the target S3 key and compares against the
  stored `teams_etag` metadata. Match = skip the download + put +
  downstream re-embedding.

### Contract

`teams-sync/handler.py`:

- `_sync_team_files(access_token, workspace_id, connection_id,
  team_id, channels, delta_links, channel_folder_maps, sync_id)`:
  one call per team, per sync cycle. Returns the number of files
  queued.
- Delta token persisted on the connection row as
  `delta_links["team_drive_{drive_id}"]`.
- Ancestor map persisted on the connection row as
  `channel_folder_maps["team_drive_{drive_id}"]`: dict of
  `{folder_id: channel_name}`. Seeded from each channel's
  filesFolder id; grown incrementally as delta returns subfolder
  driveItems.
- Routing: `parentReference.id` on each delta item is looked up in
  the ancestor map. A file with no matching ancestor is skipped.

`file-ingestion/handler.py`:

- SQS body's `file.etag` is the Graph item's `eTag`.
- `_teams_file_unchanged(s3_key, graph_etag)`: `head_object` +
  metadata match. Fail-safe: return `False` on any error other
  than a match, so the caller falls through to the download+put
  path. A broken check never silently drops a real change.
- Metadata written on put now includes `teams_etag: <graph eTag>`
  (up to 256 chars).
- Two CloudWatch log lines for observability:
  - `[teams-file-skip] <key> unchanged (etag=...); short-circuiting`
  - `[teams-file-put] <key> uploaded (etag=...); size=N`

### Why parent-id routing, not path-prefix

Graph's `driveItem: delta` docs are explicit:

> The `parentReference` property on items won't include a value
> for **path**. This occurs because renaming a folder doesn't
> result in any descendants of the folder being returned from
> **delta**. When using delta you should always track items by id.

The initial plan draft routed items to channels by matching
`parentReference.path` against each channel folder's known path
prefix. That is unimplementable — Graph does not populate path on
delta results. Routing has to be by parent driveItem id, which
means the ancestor map must be built incrementally and persisted
across cycles.

### Delta-token invalidation recovery

Graph returns 410 Gone when a stored delta token has expired or
the server state has advanced beyond what the token can express.
`teams_api.DeltaTokenInvalidError` is raised on 410; `_sync_team_files`
catches it once and retries with `delta_token=None`, matching the
documented SharePoint recovery pattern.

## Non-goals

- **Post-migration private channel files at the drive root are NOT
  ingested.** Per Microsoft's own docs, post-migration private
  channels return the drive root as their `filesFolder`. If two or
  more private channels report the same folder id, `_get_channel_roots`
  detects the collision, drops the affected channels from routing,
  and logs a warning:

  > `[teams-file-sync] channels X, Y share drive root (ID) as
  > filesFolder; files at drive root will not be ingested. This is
  > a Microsoft private-channel design constraint (post-migration),
  > not a sync bug.`

  Pre-migration private channels have their own folder id and
  route normally.

- **No etag semantics change for other providers.** The safety net
  in Layer 2 lands in `process_teams_file` only. If skip-rate
  metrics justify, a separate ADR can extend the pattern to
  SharePoint, OneDrive, and OneNote. Those already use delta at
  Layer 1 so the marginal win is smaller.

- **No orphan / deletion handling redesign.** Delta returns
  deleted items with a `deleted` facet or `@removed` annotation;
  `_is_deleted` filters them. Downstream delete-worker orphan
  sweep is unchanged.

- **No mixed drive support.** A single Team's file sync assumes
  one drive. Multi-drive teams (if Microsoft ever ships that) will
  need a design change.

## Consequences

- **Cross-region parity (per CLAUDE.md HA rule).** Both regions
  must ship teams-sync and file-ingestion together. IAM is
  unchanged (Graph access via OAuth token; S3 head_object grant
  is already implicit in the file-ingestion task role from
  put_object).

- **Skip-rate is the eTag stability signal.** The initial implementation
  audit could not confirm from Graph docs alone whether the eTag
  changes on no-content-change saves. Live-verify measures the
  skip-rate in CloudWatch after two full sync cycles. Near-100%
  = eTag is stable (expected). Near-0% = eTag rotates on every
  save; follow-up ADR would replace the eTag comparison with a
  content-hash check.

- **`channel_folder_maps` grows monotonically until pruned.** No
  pruning exists today; a future follow-up can trim on
  folder-delete events. For typical enterprise teams (dozens to a
  couple hundred subfolders per drive) the map is well under the
  400KB DDB item limit.

- **Deploy path.** Two Lambda zips (teams-sync, file-ingestion),
  both regions. `terraform apply`. No IAM changes; no Fargate; no
  new env vars.

## Related ADRs

- **ADR-007** — Documentation-Driven Decisions. Sync patterns
  become architectural over time; this ADR captures the durable
  decision so a future developer doesn't reinvent the routing
  strategy from Graph docs alone (and re-discover the
  `parentReference.path` trap).

## References

- `lambda/teams-sync/teams_api.py::get_drive_delta`.
- `lambda/teams-sync/handler.py::_sync_team_files` +
  `_get_channel_roots` + `_is_deleted`.
- `lambda/file-ingestion/handler.py::process_teams_file` +
  `_teams_file_unchanged`.
- Microsoft Graph docs — `driveItem: delta`
  (https://learn.microsoft.com/en-us/graph/api/driveitem-delta).
- Microsoft Graph docs — `channel: get filesFolder`
  (https://learn.microsoft.com/en-us/graph/api/channel-get-filesfolder),
  including the private-channel migration note.

Version History:

- v1.0 (2026-07-04): Initial decision. Chunks 02+03 shipped.
  Closes the "Teams File Sync: No Delta, Full Re-Processing Every
  Hour" tech-debt entry filed 2026-03-19.
