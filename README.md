# Transmission Cleaner

A self-hosted web app that connects to a [Transmission](https://transmissionbt.com/) torrent client and identifies torrents that have **no hardlinks** — meaning they haven't been imported by Sonarr/Radarr and are safe to delete.

## Features

- Lists all deletable torrents (files with `nlink == 1` → no hardlinks)
- Shows torrent name, size, added date, and trackers
- Sortable columns (name, size, added date, trackers)
- Delete torrent + media files directly via the UI (with confirmation)
- Settings page — configure Transmission connection without restarting
- Config persisted in `config/config.json` (survives container rebuilds)
- Read-only mount of the media share for safe hardlink detection

## Screenshot

> Dark modern UI with sortable table, delete confirmation modal, and settings page.

## Quick Start

```bash
git clone https://github.com/roikeman/autoremove-transmission.git
cd autoremove-transmission
```

Edit `config/config.json` with your Transmission details:

```json
{
  "transmission_host": "192.168.1.x",
  "transmission_port": "9091",
  "transmission_user": "",
  "transmission_pass": "",
  "transmission_rpc_path": "/rpc"
}
```

> **Note:** Standard Transmission installs use `/transmission/rpc`. Some custom builds use `/rpc`.

Then start:

```bash
docker compose up -d
```

Open `http://localhost:5000`

## Docker Compose

```yaml
services:
  transmission-checker:
    image: ghcr.io/roikeman/autoremove-transmission:latest
    ports:
      - "5000:5000"
    volumes:
      - /share:/share:ro      # your media directory — same path as Transmission sees
      - ./config:/config      # persisted config
    restart: unless-stopped
```

The `/share` volume must be mounted at the **same path** that Transmission uses, so hardlink detection (`os.stat().st_nlink`) works correctly.

## How It Works

1. Fetches all torrents from the Transmission RPC API
2. For each torrent, checks every file with `os.stat(path).st_nlink`
3. If **all** files have `nlink == 1` (no other directory entry points to the inode), the torrent is considered deletable
4. Torrents where any file has `nlink > 1` (hardlinked by Sonarr/Radarr into the media library) are excluded

## Configuration

Settings are stored in `config/config.json` and can be edited via the **Settings page** (`/settings`) in the UI without restarting the container.

| Field | Default | Description |
|---|---|---|
| `transmission_host` | `192.168.1.132` | Transmission host |
| `transmission_port` | `9091` | Transmission port |
| `transmission_rpc_path` | `/rpc` | RPC endpoint path |
| `transmission_user` | _(empty)_ | Basic auth username (optional) |
| `transmission_pass` | _(empty)_ | Basic auth password (optional) |

## Development

```bash
# Install deps
pip install -r app/requirements.txt

# Run locally (config reads from ./config/config.json by default)
CONFIG_PATH=./config/config.json python app/app.py
```

## Library cleanup

Beyond torrent hardlink cleanup, the app can also identify and remove stale
media from your Jellyfin library — the imported copies that Sonarr/Radarr
hardlinked in, not just orphaned torrents.

For every Jellyfin title it checks two independent thresholds, both
configurable in **Settings**:

| Setting | Default | Meaning |
|---|---|---|
| `age_days` | 180 | A title must have been added at least this many days ago before it's even considered. |
| `idle_days` | 90 | Once a title qualifies on age, it must also have gone unplayed for at least this many days (or never been played at all). |

A title only becomes a cleanup candidate once **both** thresholds are met.
Each candidate is then sorted into one of five buckets based on watch state:

| Bucket | Label | Pre-ticked? |
|---|---|---|
| A | Never opened | Yes |
| B | Fully watched | Yes |
| C1 | Near complete (≥80% of episodes watched) | Yes |
| C2 | Sampled, dropped (opened and abandoned early) | Yes |
| C3 | Mid-watch | **No** |

Buckets A, B, C1, and C2 are pre-selected in the UI because in every one of
those cases the available watch signal points the same way: either the
title was never engaged with, or it was engaged with and then dropped /
finished. **C3 (mid-watch) is deliberately never pre-ticked** — it covers
titles sitting in an ambiguous middle (partially through a series, or a
Jellyfin metadata state that looks like "in progress"), where marking it for
deletion by default risks deleting something someone is still actively
watching. C3 titles can still be selected manually, but the tool never
does it for you.

### Deletion order

When a run executes, each selected title is deleted in this order:

1. **Sonarr/Radarr (or direct file delete)** — if the title is owned by
   Sonarr or Radarr, the app tells the *arr to delete the item with
   `deleteFiles=true` (and never adds an import list exclusion, so the
   title can be re-added later); otherwise the app deletes the library
   files itself. Either way, this step removes the **library's copy** of
   the file.
2. **Jellyfin** — the item is removed from Jellyfin's database so it
   disappears from the UI immediately.
3. **Transmission sweep** — once every title in the run has been processed,
   a single end-of-run pass checks Transmission for torrents whose files
   match the inodes that were just deleted, and removes any that are no
   longer needed.

This order matters because of how the library copy got there in the first
place: Sonarr/Radarr **hardlinks** an imported file into the library rather
than copying it, so while both copies exist the file's link count
(`nlink`) is 2 — one directory entry in the torrent's download folder, one
in the library. Deleting the library copy (step 1) only drops `nlink` from
2 to 1; the torrent's own directory entry is untouched, so an actively
seeding torrent keeps seeding uninterrupted. Only after the library-side
copy is gone does the Transmission sweep (step 3) look at whether that
torrent's data is now safe to remove — and even then, only if it isn't
protected by the seeding guard.

### Seeding guard

If `seed_guard` is enabled (the default), the Transmission sweep will not
remove a torrent that hasn't met its seed ratio target: per-torrent ratio
limits, the session's global ratio limit, or "seed forever" torrents are
all respected. A torrent that's still obligated to seed is left in place
even though its library copy is gone; it's swept later, on a subsequent
run, once its ratio target is met.

### Blast-radius caps

Every run — whether it's a dry-run plan or the real thing — is capped by
two settings so a misconfiguration or a bad selection can't wipe out the
whole library in one go:

- `max_titles_per_run` (default 50) — the maximum number of titles a single
  run will touch.
- `max_bytes_per_run` (default 1 TiB) — the maximum total size a single run
  will delete.

Exceeding either cap aborts the run before anything is deleted.

### Deletions cannot be undone

**There is no undo.** Once a title's files are deleted, Sonarr/Radarr's
metadata entry is removed, and Jellyfin's library entry is removed, the
only way to get the media back is to re-acquire it. Review the plan
carefully — especially any manually-selected C3 (mid-watch) titles — before
confirming a run.

### Supplying the three API keys

The cleanup feature needs API keys for Jellyfin, Sonarr, and Radarr. There
are two ways to provide them:

- **Settings UI** — enter each key directly on the `/settings` page; it's
  stored in `config/config.json`.
- **Environment override** — set `JELLYFIN_API_KEY`, `SONARR_API_KEY`,
  and/or `RADARR_API_KEY` (see the `environment:` block in
  `docker-compose.yml`). A key supplied this way takes precedence over
  whatever is stored in config, and its settings field is rendered
  read-only in the UI to make that clear.

## License

MIT

## Versioning and branches

The version lives in the repo-root `VERSION` file and is reported by
`GET /api/health`:

```json
{"status": "ok", "build": {"version": "1.0.0", "sha": "a1b2c3d4e5f6", "ref": "dev"}}
```

`sha` and `ref` are baked in at image build time, so a running container tells
you exactly which build it is. A local `docker build` with no build args reports
the `VERSION` value with `sha`/`ref` of `unknown` — that is how you tell a local
image from a CI one.

### Branches and image tags

| Push to | Image tags |
|---|---|
| `master` | `latest`, `<VERSION>`, `<sha>` |
| `dev` | `dev`, `<sha>` |
| tag `v*` | `1.2.3`, `1.2`, `1`, `<sha>` |
| pull request | none — builds only, never pushes |

Feature branches target `dev`; `dev` merges to `master` for a release.

Releasing: bump `VERSION` on `master`, then push a matching `v<version>` git tag
to publish the semver-tagged images.
