# Jellyfin Library Cleanup — Design

**Date:** 2026-09-06
**Status:** Approved design, pending implementation
**Extends:** `autoremove-transmission`

## Problem

The media DAS is at 98% capacity — 232 GB free of 10.9 TB — while content is
arriving at roughly 1.1 TB/month. Reclaiming space today means manually
cross-referencing what nobody watches against three separate systems, then
deleting from each by hand. Miss the Sonarr/Radarr step and the *arr stack
simply re-downloads what was removed.

This tool selects stale library content by watch history and removes it from
every system that holds it, in one reviewed operation.

### Relationship to the existing tool

`autoremove-transmission` today identifies torrents whose files have no
hardlink elsewhere (`nlink == 1`) — that is, torrents whose library copy is
already gone. It cleans the *download* side after the library side disappears.

This feature is the other half: it removes the *library* side. The two compose
naturally — deleting a library file drops its torrent's link count to 1, which
the existing `is_deletable()` predicate then recognises.

## Environment

Established by direct inspection on 2026-09-06.

| Component | Location | Notes |
|---|---|---|
| Jellyfin | `192.168.1.170` (`gmk`) | podman container; HTTP API reachable from the VM |
| Sonarr, Radarr, Prowlarr, Transmission | `192.168.1.132` (`debian-cosmos`) | Docker; QEMU VM hosted on `gmk` |
| Media | `/share` | virtiofs export of the host's `magnetic-12tb` |
| This tool | `192.168.1.132` | Docker, `network_mode: host` |

**The decisive environmental fact:** `/share` is mounted identically by
Jellyfin, Sonarr, Radarr, and Transmission. Paths need no translation between
systems, which makes path-based ownership matching viable.

**The decisive constraint:** `jellyfin.db` lives on `/2t-storage` on `.170` and
is *not* exported to the VM. The tool cannot read the database file. It uses
the Jellyfin HTTP API, which was confirmed reachable (`HTTP 200`).

## Approach

Three options were considered:

- **Jellyfin HTTP API** (chosen). Survives schema changes — the database
  recently migrated to EF Core, which would have broken raw SQL. Deletion via
  the API lets Jellyfin clean its own metadata rather than leaving orphaned
  rows. Costs several hundred HTTP calls where one query would do.
- **Direct SQLite reads.** Faster and already validated during analysis, but
  requires exporting `/2t-storage` from `.170` into the VM — new coupling
  across the host boundary — and binds the tool to a schema that moves.
  Rejected.
- **Jellystat for watch history.** Jellystat records playback *events* where
  Jellyfin stores mutable current state. Genuinely better data if `UserData`
  is ever wiped by a deleted user or a re-added library. Neither applies here,
  and Jellyfin's `UserData` answers the criterion directly. Rejected as
  unnecessary; can be added later behind the same interface.

## Selection criteria

An item is a **candidate** when both hold:

```
added       <= now - age_threshold      (default 6 months)
last_played <  now - idle_threshold     (default 3 months), or never played
```

Both thresholds are configurable.

### Buckets

Candidates are classified by how much of the content was actually consumed.
The bucket drives whether a row is pre-selected in the UI.

| Bucket | Rule | Pre-ticked |
|---|---|---|
| A · never opened | no `last_played`, `watched == 0` | yes |
| B · fully watched | `watched >= episodes` | yes |
| C1 · near complete | `watched / episodes >= 0.8` | yes |
| C2 · sampled, dropped | `watched == 0` but `last_played` exists | yes |
| C3 · mid-watch | everything else | **no** |

C3 is content someone abandoned partway through — the only group requiring
real judgment, so it is never pre-selected.

Measured against the live library on 2026-09-06 at the default thresholds:
1.23 TB across 141 titles, of which C3 accounts for ~213 GB.

### Data quality flags

Two known data hazards are surfaced rather than silently absorbed:

- **`added-date-unreliable`** — set when `last_played < added`. A Sonarr quality
  upgrade replaces the file and resets `DateCreated` while watch history
  persists, making content look newer than it is. Observed on real titles.
- **`frees-less-than-listed`** — set when a file has `nlink > 1`. Its size is
  shared with a torrent's copy, so deleting it reclaims less than the reported
  figure.

## Architecture

`app.py` currently mixes Flask routes, Transmission RPC, and filesystem
deletion in ~300 lines. Three more API clients and a classification engine
would double it, so the extension begins by splitting along existing seams.

```
app/
  app.py              Flask routes only
  config.py           extended DEFAULTS
  clients/
    transmission.py   moved from app.py, behaviour unchanged
    jellyfin.py       new
    arr.py            new — Sonarr and Radarr share the v3 API shape
  cleanup/
    candidates.py     fetch and normalize
    buckets.py        pure classification, no I/O
    pipeline.py       deletion orchestration
  templates/
    index.html        existing torrent view
    library.html      new
    settings.html     extended
```

`buckets.py` performs no I/O. The entire decision engine is unit-testable with
plain dataclasses and no live Jellyfin. `clients/` modules only translate HTTP
into dataclasses.

Moving the Transmission code is a relocation with no behavioural change:
`rpc_call`, `get_all_torrents`, and `is_deletable` transfer as-is.

### Data model

```python
@dataclass
class Candidate:
    jf_id: str
    kind: str                 # 'series' | 'movie'
    title: str
    path: str                 # /share/... — identical on every host
    size_bytes: int
    added: datetime           # DateLastMediaAdded for series
    last_played: datetime | None
    episodes: int             # 1 for a movie
    watched: int              # episodes played; 0/1 for a movie
    progress_pct: float
    owner: str | None         # 'sonarr' | 'radarr' | None
    owner_id: int | None
    bucket: str
    flags: list[str]
```

### Deriving `watched`

`watched` drives every bucket rule, so its derivation is fixed here:

- **Series** — `episodes - UserData.UnplayedItemCount`, taken from the Series
  object. Where multiple users exist, the per-user values are merged by taking
  the **maximum watched count** and the **latest** `LastPlayedDate`: if anyone
  in the household finished it, it counts as watched, and the most recent
  viewing by anyone determines staleness.
- **Movie** — `1` when `UserData.Played` is true, else `0`.
  `progress_pct` comes from `UserData.PlayedPercentage`, treated as `0` when
  absent. A movie with a `LastPlayedDate` but no stored resume position was
  opened and abandoned within the first few minutes; Jellyfin does not persist
  a resume point that early. This is bucket C2, not C3.

If `UnplayedItemCount` proves unavailable on the Series object, the fallback is
a per-series episode query — a correctness-preserving change that costs one
request per candidate series. The first implementation task settles which
applies.

### Ownership matching

A `path -> owner` index is built once from `/api/v3/series` and
`/api/v3/movie`, then candidates are matched by path prefix. This works only
because virtiofs makes `/share` identical across hosts.

Unmatched candidates get `owner = None`, are flagged **"no *arr owner"** in the
UI, and are deleted at the file level — skipping the *arr step. This keeps the
unmanaged libraries (`reality` 721 GB, `israeli` 78 GB, `kids` 37 GB,
`youtube` 13 GB) in scope.

### API surface

New endpoints, following the existing JSON conventions:

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/library` | Renders `library.html` |
| `GET` | `/api/library/candidates` | Scan and classify; accepts `age_days` and `idle_days` overrides |
| `POST` | `/api/library/plan` | Preflight for a selection — returns per-title actions and real bytes freed; executes nothing |
| `POST` | `/api/library/execute` | Runs the pipeline for a confirmed selection |
| `GET` | `/api/library/journal` | Recent run outcomes, including `partial` rows |
| `POST` | `/api/library/retry` | Retries the failed steps of one journaled title |
| `POST` | `/api/test-connection/{service}` | Extends the existing pattern to `jellyfin`, `sonarr`, `radarr` |

`execute` and `retry` are the only endpoints that delete. Both take the run
lock, both require a selection previously returned by `plan`, and both refuse
to act on a `plan` whose candidate set no longer matches — a stale browser tab
must not delete something the library has since changed.

## Deletion pipeline

Ordering is chosen so no torrent is ever left pointing at files deleted
underneath it.

**For an *arr-owned title:**

1. `DELETE /api/v3/series/{id}?deleteFiles=true&addImportListExclusion=false`
   (Radarr: `movie`, `addImportExclusion=false`). One authoritative call
   removing both the entry and the library files. Removing the entry is what
   prevents a re-grab.
2. **Transmission.** The torrent holds its own hardlink, so step 1 does not
   disturb seeding — `nlink` merely drops to 1. The existing `is_deletable()`
   predicate now identifies it, and it is removed with data.
3. **Jellyfin.** `DELETE /Items/{id}` if the entry survives; otherwise a
   targeted library scan drops the stale row and its metadata.

**For an unowned title:** delete files through the path-safety guard, then
step 3.

Import exclusions are deliberately **not** added — removed titles remain
re-requestable through seerr.

### Seeding guard

Removing a torrent the moment its library copy goes can damage ratio on
private trackers. Torrents that have not met their seed ratio or minimum seed
time are skipped and reported as **"kept, still seeding"** — never silently
deleted, never silently skipped. Enabled by default, toggleable.

### Preflight

A `plan` endpoint returns exactly what would happen per title — which API
calls, which files, and real bytes freed as measured by `stat()` — while
executing nothing. This is what the UI presents for confirmation; it is the
review gate, not a separate dry-run mode.

### Blast radius cap

Execution is refused when a selection exceeds a configurable title count or
total size without a second explicit confirmation. A mistyped threshold must
not be able to take the library with it.

## Failure handling

**The pipeline is forward-only. There is no rollback.** Deletions are
irreversible and the design does not pretend otherwise.

Each title is an independent unit. A per-title append-only journal
(`/config/cleanup-journal.jsonl`) is written before and after each step. A
title whose *arr deletion succeeds but whose Transmission step fails is
recorded `partial` with the completed steps enumerated, and surfaced in the UI
for targeted retry. The journal provides visibility and resumption — not undo.

| Condition | Behaviour |
|---|---|
| Invalid API key, or Jellyfin unreachable | Fail at preflight; never start |
| *arr returns 404 (already gone) | Treat as success — steps are idempotent |
| File already missing at delete time | Success |
| Resolved path outside allowed roots | **Hard refuse** — core safety invariant |
| Transmission HTTP 409 | Existing session-id retry handles it |
| Concurrent run attempted | Blocked by a run lock |

The path guard is the invariant the rest rests on. Allowed roots come from
config (Jellyfin library paths plus download directories); any path that
resolves outside them aborts that title. The existing delete endpoint already
enforces this for downloads; it extends to library roots.

## User interface

Two screens: a new library-cleanup page, and additions to the existing
settings page. Both follow the current templates' plain server-rendered style
with `fetch()` against JSON endpoints — no new frontend dependencies.

### Library cleanup page (`library.html`)

A new nav entry alongside the existing torrent view.

**Candidate table**, grouped by bucket with A/B/C1/C2 collapsed-open and C3
collapsed-closed, since C3 is never pre-selected:

| Column | Notes |
|---|---|
| ☑ | Pre-ticked per bucket rules |
| Title | |
| Kind | series / movie |
| Progress | `42/43` for series, `87%` for movies |
| Size | Jellyfin-reported |
| Added | flagged when unreliable |
| Last played | `never` when null |
| Owner | `sonarr` / `radarr` / **no *arr owner** |
| Flags | `added-date-unreliable`, `frees-less-than-listed` |

A running total of selected titles and reclaimable bytes sits fixed at the top,
recomputed on every tick — the number that answers "is this worth doing".

Threshold controls (age, idle) live on the page itself, not buried in settings,
so retuning and re-scanning is one interaction.

**Confirmation flow.** The delete button opens a preflight summary from the
`plan` endpoint: per-title API calls, files to be removed, real bytes freed as
measured by `stat()`, and any torrents that will be kept for seeding. Nothing
executes until this is confirmed. When the selection trips the blast-radius
cap, the dialog requires a second explicit confirmation naming the count and
total size.

**Results view.** Per-title outcome — `deleted`, `partial`, `failed`, or
`kept (seeding)` — with completed steps enumerated for partial rows and a
retry action targeting only the failed step.

### Settings page additions (`settings.html`)

- **Jellyfin / Sonarr / Radarr**: base URL and API key per service, each with a
  **Test connection** button reusing the existing live-form-values pattern
  rather than saved config. Keys render masked; fields overridden by
  environment render read-only and marked "set by environment".
- **Thresholds**: default age and idle values.
- **Seeding guard**: on/off.
- **Blast-radius caps**: maximum titles and maximum total size per run.

## Credentials

Jellyfin, Sonarr, and Radarr each require a base URL and an API key. All six
values are **editable from the settings UI**, following the existing
`transmission_pass` pattern for consistency with the rest of the application.

Handling rules:

- Stored in `config/config.json`, which is already gitignored.
- Written with `0600` permissions.
- Masked as `••••••••` in every `GET /api/settings` response. A submitted value
  beginning with `••` means "unchanged" and preserves the stored key — the
  mechanism `transmission_pass` already uses.
- Never logged, never echoed in error messages, never included in the journal.
- Environment variables (`JELLYFIN_API_KEY`, `SONARR_API_KEY`,
  `RADARR_API_KEY`) are honoured as an **optional override**: when set, they
  take precedence and the corresponding UI field renders read-only, marked
  "set by environment". This keeps a non-persisting path available without
  making it mandatory.

**Known tradeoff, accepted deliberately.** Persisting API tokens to a plaintext
file conflicts with Check Point's credentials policy, which requires runtime
environment injection for tokens. That policy governs Check Point systems; this
is a personal homelab tool, and UI-editable credentials were chosen knowingly
for usability. The environment override above exists for anyone wanting the
stricter posture.

## Testing

Written test-first. The failure mode is deleting the wrong thing, so the tests
target that directly.

- **`buckets.py`** — pure unit tests at every boundary: 0%, 79.9%, 80%, 100%,
  movie versus series, and both data-quality flags. No network.
- **`clients/`** — mocked HTTP asserting exact URLs and parameters, in
  particular that `deleteFiles=true` and `addImportListExclusion=false` are
  actually transmitted. A silent parameter typo here is a data-loss bug.
- **`pipeline.py`** — mocked clients asserting step ordering, journal contents,
  and partial-failure recording.
- **Path guard** — adversarial cases: `../` traversal, symlinks resolving
  outside roots, and paths that normalize into a root.
- **Manual verification** — preflight against the live stack. No live-system
  tests in CI.

## First implementation task

Capture the real Jellyfin API response for one series and one movie to confirm
that `DateLastMediaAdded`, `UserData.UnplayedItemCount`, and size fields are
exposed as assumed. These captures become the test fixtures.

This is deliberately first: the field availability is assumed from database
inspection, not from an observed API response. If a field is missing, the
candidate query changes shape — better learned before the engine exists than
after.

## Out of scope

- Rollback or undo of any kind
- Import-exclusion management
- Automatic or scheduled execution — every deletion is human-confirmed
- Changes to existing Transmission credential handling
- Music, books, or photo libraries
