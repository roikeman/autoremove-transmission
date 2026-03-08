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

## License

MIT
