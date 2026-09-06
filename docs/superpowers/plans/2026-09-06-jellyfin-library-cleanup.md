# Jellyfin Library Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `autoremove-transmission` with a reviewed workflow that selects stale library content from Jellyfin watch history and removes it from Sonarr/Radarr, Transmission, and Jellyfin in one pass.

**Architecture:** A new `cleanup/` package holds a pure, I/O-free classification engine and a forward-only deletion pipeline; a new `clients/` package holds thin HTTP wrappers for Transmission (moved from `app.py`), Jellyfin, and the *arr services. `app.py` is reduced to Flask routes. Every deletion is human-confirmed through a preflight step.

**Tech Stack:** Python 3.11, Flask 3.1.3, requests 2.32.5, gunicorn 25.1.0, pytest + responses (new, test-only). No new frontend dependencies — server-rendered templates with `fetch()`.

**Spec:** `docs/superpowers/specs/2026-09-06-jellyfin-library-cleanup-design.md`

## Global Constraints

- Python 3.11 (`Dockerfile` uses `python:3.11-slim`). No syntax newer than 3.11.
- No new runtime dependencies. `pytest` and `responses` are test-only, in `app/requirements-dev.txt`.
- No new frontend dependencies. Templates use plain `fetch()` and match the existing style in `app/templates/index.html`.
- **The pipeline is forward-only. There is no rollback.** Never write code implying deletions can be undone.
- **Path guard invariant:** any resolved path outside configured roots aborts that title. Never bypass.
- Secrets (`transmission_pass`, `jellyfin_api_key`, `sonarr_api_key`, `radarr_api_key`) are masked as `••••••••` in every API response, never logged, never written to the journal.
- Default thresholds: `age_days = 180`, `idle_days = 90`.
- All services see media at identical `/share` paths. Never write path-translation logic.
- Bucket identifiers are exactly the strings `"A"`, `"B"`, `"C1"`, `"C2"`, `"C3"`.
- Import exclusions are never added: Sonarr uses `addImportListExclusion=false`, Radarr uses `addImportExclusion=false`.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `app/clients/__init__.py` | package marker |
| `app/clients/transmission.py` | Transmission RPC — moved verbatim from `app.py` |
| `app/clients/jellyfin.py` | Jellyfin HTTP API |
| `app/clients/arr.py` | Sonarr + Radarr v3 API (shared shape) |
| `app/cleanup/__init__.py` | package marker |
| `app/cleanup/buckets.py` | pure classification, zero I/O |
| `app/cleanup/candidates.py` | normalize API responses, ownership matching |
| `app/cleanup/paths.py` | path-safety guard |
| `app/cleanup/journal.py` | append-only run journal |
| `app/cleanup/pipeline.py` | deletion orchestration |
| `app/templates/library.html` | cleanup screen |
| `app/requirements-dev.txt` | test dependencies |
| `tests/` | test suite |

**Modified:**

| Path | Change |
|---|---|
| `app/app.py:7-77` | Transmission logic removed, imported from `clients.transmission` |
| `app/app.py:178-184` | `_get_known_download_dirs` reused by the path guard |
| `app/config.py:7-14` | `DEFAULTS` extended; secret masking and env override added |
| `app/templates/settings.html` | service URL/key fields, thresholds, guards |
| `docker-compose.yml` | env passthrough for optional key overrides |
| `README.md` | new feature documented |

---

## Task 1: Test infrastructure

Nothing in this plan may touch deletion logic before tests can run. This task establishes the harness and locks current `is_deletable` behavior so the Task 2 refactor is provably safe.

**Files:**
- Create: `app/requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_is_deletable.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces: `pytest` runnable from repo root; `tmp_tree` fixture creating files with controlled link counts

- [ ] **Step 1: Add dev dependencies**

Create `app/requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.4
responses==0.25.6
```

- [ ] **Step 2: Add pytest config and ignore cache**

Create `pytest.ini` at repo root:

```ini
[pytest]
testpaths = tests
pythonpath = app
```

Append to `.gitignore`:

```
.pytest_cache/
tests/fixtures/*.local.json
```

- [ ] **Step 3: Create the package marker and shared fixture**

Create `tests/__init__.py` as an empty file.

Create `tests/conftest.py`:

```python
import os
import pytest


@pytest.fixture
def tmp_tree(tmp_path):
    """Build files with controlled hardlink counts.

    Returns a helper: make(name, size, linked=False) -> str path.
    A linked file gets a second hardlink, so st_nlink == 2.
    """
    links_dir = tmp_path / "links"
    files_dir = tmp_path / "files"
    links_dir.mkdir()
    files_dir.mkdir()

    def make(name, size=16, linked=False):
        path = files_dir / name
        path.write_bytes(b"x" * size)
        if linked:
            os.link(path, links_dir / name)
        return str(path)

    return make
```

- [ ] **Step 4: Write the characterization test**

Create `tests/test_is_deletable.py`:

```python
import os
from app import is_deletable


def _torrent(paths):
    return {
        "downloadDir": os.path.dirname(paths[0]) if paths else "",
        "files": [{"name": os.path.basename(p)} for p in paths],
    }


def test_unlinked_files_are_deletable(tmp_tree):
    a = tmp_tree("a.mkv")
    assert is_deletable(_torrent([a])) is True


def test_hardlinked_file_blocks_deletion(tmp_tree):
    a = tmp_tree("a.mkv", linked=True)
    assert is_deletable(_torrent([a])) is False


def test_any_hardlinked_file_blocks_deletion(tmp_tree):
    a = tmp_tree("a.mkv")
    b = tmp_tree("b.mkv", linked=True)
    assert is_deletable(_torrent([a, b])) is False


def test_torrent_with_no_files_is_not_deletable():
    assert is_deletable({"downloadDir": "/nope", "files": []}) is False


def test_missing_file_is_skipped_not_fatal(tmp_tree):
    a = tmp_tree("a.mkv")
    t = _torrent([a])
    t["files"].append({"name": "ghost.mkv"})
    assert is_deletable(t) is True
```

- [ ] **Step 5: Run the tests**

```bash
pip install -r app/requirements-dev.txt
pytest -v
```

Expected: 5 passed. If `test_missing_file_is_skipped_not_fatal` fails, stop — it means current behavior differs from the spec's description and Task 2 cannot proceed safely.

- [ ] **Step 6: Commit**

```bash
git add pytest.ini .gitignore app/requirements-dev.txt tests/
git commit -m "test: add pytest harness and characterize is_deletable"
```

---

## Task 2: Extract the Transmission client

Pure refactor. Task 1's tests must stay green with no modification — that is the proof of no behavior change.

**Files:**
- Create: `app/clients/__init__.py`
- Create: `app/clients/transmission.py`
- Modify: `app/app.py:1-77`
- Modify: `tests/test_is_deletable.py:2`

**Interfaces:**
- Consumes: `config.load()` from `app/config.py`
- Produces: `clients.transmission.rpc_call(method, arguments)`, `get_all_torrents()`, `is_deletable(torrent)`, `reset_session()`

- [ ] **Step 1: Create the client module**

Create `app/clients/__init__.py` as an empty file.

Create `app/clients/transmission.py` by moving lines 7–77 of `app/app.py` verbatim, adding `reset_session()` to replace the direct `_session_id` manipulation in `save_settings`:

```python
import os
import threading
import requests
import config as cfg_mod

_session = requests.Session()
_session_id = None
_session_lock = threading.Lock()


def _cfg():
    return cfg_mod.load()


def _auth(cfg):
    return (cfg["transmission_user"], cfg["transmission_pass"]) if cfg["transmission_user"] else None


def _rpc_url(cfg):
    return f"http://{cfg['transmission_host']}:{cfg['transmission_port']}{cfg['transmission_rpc_path']}"


def _refresh_session_id(cfg):
    global _session_id
    resp = _session.post(_rpc_url(cfg), json={}, auth=_auth(cfg), timeout=10)
    if resp.status_code == 409:
        _session_id = resp.headers.get("X-Transmission-Session-Id", "")


def reset_session():
    """Force re-authentication on the next call."""
    global _session_id
    with _session_lock:
        _session_id = None


def rpc_call(method, arguments):
    global _session_id
    cfg = _cfg()

    with _session_lock:
        if not _session_id:
            _refresh_session_id(cfg)

    headers = {"X-Transmission-Session-Id": _session_id or ""}
    payload = {"method": method, "arguments": arguments}
    resp = _session.post(_rpc_url(cfg), json=payload, headers=headers, auth=_auth(cfg), timeout=30)

    if resp.status_code == 409:
        with _session_lock:
            _refresh_session_id(cfg)
        headers["X-Transmission-Session-Id"] = _session_id or ""
        resp = _session.post(_rpc_url(cfg), json=payload, headers=headers, auth=_auth(cfg), timeout=30)

    resp.raise_for_status()
    return resp.json()


def get_all_torrents():
    result = rpc_call("torrent-get", {
        "fields": ["id", "name", "totalSize", "downloadDir", "files", "addedDate",
                   "trackers", "percentDone", "uploadRatio", "secondsSeeding",
                   "seedRatioLimit", "seedRatioMode"],
    })
    return result["arguments"]["torrents"]


def remove_torrent(torrent_id, delete_data=True):
    """Remove a torrent, optionally deleting its payload."""
    rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": bool(delete_data)})


def is_deletable(torrent):
    """Return True if none of the torrent's files have hardlinks (nlink == 1)."""
    files = torrent.get("files", [])
    download_dir = torrent.get("downloadDir", "")

    if not files:
        return False

    for file_entry in files:
        path = os.path.join(download_dir, file_entry["name"])
        try:
            if os.stat(path).st_nlink > 1:
                return False
        except (FileNotFoundError, PermissionError, OSError):
            continue

    return True
```

Note the added fields on `get_all_torrents` (`uploadRatio`, `secondsSeeding`, `seedRatioLimit`, `seedRatioMode`) — Task 11's seeding guard needs them. Extra fields are harmless to existing callers.

- [ ] **Step 2: Update `app.py` to import instead of define**

In `app/app.py`, delete lines 7–77 (from `_session = requests.Session()` through the end of `is_deletable`). Replace the import block at lines 1–5 with:

```python
import os
import threading
from flask import Flask, jsonify, render_template, request as flask_request
import config as cfg_mod
from clients.transmission import (
    rpc_call,
    get_all_torrents,
    is_deletable,
    reset_session,
    _cfg,
    _rpc_url,
    _auth,
)
```

In `save_settings`, replace the direct session reset:

```python
    # Reset RPC session so next call re-authenticates with new settings
    reset_session()
```

Delete the now-unused `global _session_id` line and the `with _session_lock:` block around it.

- [ ] **Step 3: Point the test at the new module**

In `tests/test_is_deletable.py`, change line 2:

```python
from clients.transmission import is_deletable
```

- [ ] **Step 4: Run tests and verify the app still imports**

```bash
pytest -v
python -c "import sys; sys.path.insert(0, 'app'); import app; print('routes:', len(list(app.app.url_map.iter_rules())))"
```

Expected: 5 passed; route count printed without error.

- [ ] **Step 5: Commit**

```bash
git add app/clients/ app/app.py tests/test_is_deletable.py
git commit -m "refactor: extract transmission client from app.py"
```

---

## Task 3: Capture real Jellyfin API fixtures

**This task requires a Jellyfin API key and must be run by the repository owner.** The entire candidate engine assumes three fields that were verified in the Jellyfin *database* but never observed in an API response. Confirm them before building on them.

**Files:**
- Create: `scripts/capture_jellyfin_fixtures.py`
- Create: `tests/fixtures/jellyfin_series.json`
- Create: `tests/fixtures/jellyfin_movie.json`
- Create: `tests/fixtures/jellyfin_users.json`

**Interfaces:**
- Consumes: `JELLYFIN_URL` and `JELLYFIN_API_KEY` from the environment
- Produces: fixture files consumed by Tasks 6 and 8

- [ ] **Step 1: Write the capture script**

Create `scripts/capture_jellyfin_fixtures.py`:

```python
"""Capture real Jellyfin API responses as test fixtures.

Usage:
    export JELLYFIN_URL=http://192.168.1.170:8096
    export JELLYFIN_API_KEY=<your key>
    python scripts/capture_jellyfin_fixtures.py

Writes sanitized fixtures to tests/fixtures/. The API key is read from the
environment and never written to disk.
"""
import json
import os
import sys
import requests

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures")

REQUIRED_SERIES_FIELDS = ["Id", "Name", "Path", "DateCreated", "DateLastMediaAdded", "UserData"]
REQUIRED_USERDATA_FIELDS = ["Played", "LastPlayedDate", "UnplayedItemCount"]


def main():
    base = os.environ.get("JELLYFIN_URL", "").rstrip("/")
    key = os.environ.get("JELLYFIN_API_KEY", "")
    if not base or not key:
        sys.exit("Set JELLYFIN_URL and JELLYFIN_API_KEY")

    s = requests.Session()
    s.headers["Authorization"] = f'MediaBrowser Token="{key}"'

    users = s.get(f"{base}/Users", timeout=30).json()
    uid = users[0]["Id"]

    common = {
        "Recursive": "true",
        "Fields": "Path,DateCreated,DateLastMediaAdded,ProviderIds",
        "Limit": "3",
    }

    series = s.get(f"{base}/Users/{uid}/Items",
                   params={**common, "IncludeItemTypes": "Series"}, timeout=30).json()
    movies = s.get(f"{base}/Users/{uid}/Items",
                   params={**common, "IncludeItemTypes": "Movie"}, timeout=30).json()

    os.makedirs(OUT, exist_ok=True)
    _write("jellyfin_users.json", [{"Id": u["Id"], "Name": u["Name"]} for u in users])
    _write("jellyfin_series.json", series)
    _write("jellyfin_movie.json", movies)

    _report("Series", series.get("Items", []), REQUIRED_SERIES_FIELDS)
    _report("Movie", movies.get("Items", []), REQUIRED_SERIES_FIELDS)


def _write(name, data):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"wrote {name}")


def _report(label, items, required):
    if not items:
        print(f"{label}: NO ITEMS RETURNED")
        return
    item = items[0]
    print(f"\n{label} field check:")
    for field in required:
        print(f"  {field}: {'PRESENT' if field in item else 'MISSING'}")
    ud = item.get("UserData", {})
    for field in REQUIRED_USERDATA_FIELDS:
        print(f"  UserData.{field}: {'PRESENT' if field in ud else 'MISSING'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the capture**

```bash
export JELLYFIN_URL=http://192.168.1.170:8096
export JELLYFIN_API_KEY=<your key>
python scripts/capture_jellyfin_fixtures.py
```

Expected: three fixture files written, and a field report.

- [ ] **Step 3: Act on the field report**

Read the printed report before continuing:

- **All PRESENT** — proceed to Task 4 unchanged.
- **`DateLastMediaAdded` MISSING on Series** — Task 8 must derive `added` from a per-series episode query (`/Users/{uid}/Items?ParentId={series_id}&IncludeItemTypes=Episode&Fields=DateCreated`, taking the maximum). Record this in the task before implementing.
- **`UserData.UnplayedItemCount` MISSING** — Task 8 uses the same per-series episode query, counting episodes where `UserData.Played` is true. The spec anticipates this fallback.

Do not guess. If a field is missing, update Task 8's notes first.

- [ ] **Step 4: Verify no secrets leaked into fixtures**

```bash
grep -riE "api[_-]?key|token|password" tests/fixtures/ || echo "clean"
```

Expected: `clean`. If anything matches, remove the field from the fixture before committing.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_jellyfin_fixtures.py tests/fixtures/
git commit -m "test: capture real jellyfin api fixtures"
```

---

## Task 4: Extend configuration

**Files:**
- Modify: `app/config.py:7-41`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `config.load()` returning the extended dict with env overrides applied; `config.save(data)`; `config.SECRET_KEYS`; `config.ENV_OVERRIDES`; `config.mask(cfg)`; `config.env_locked()`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import json
import config


def _cfg_file(tmp_path, monkeypatch, data=None):
    path = tmp_path / "config.json"
    if data is not None:
        path.write_text(json.dumps(data))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return path


def test_defaults_include_new_services(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    cfg = config.load()
    assert cfg["age_days"] == 180
    assert cfg["idle_days"] == 90
    assert cfg["seed_guard"] is True
    assert cfg["jellyfin_url"] == ""
    assert cfg["library_roots"] == ["/share"]


def test_env_overrides_api_key(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "from-file"})
    monkeypatch.setenv("JELLYFIN_API_KEY", "from-env")
    cfg = config.load()
    assert cfg["jellyfin_api_key"] == "from-env"
    assert "jellyfin_api_key" in config.env_locked()


def test_env_locked_empty_without_env(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    assert config.env_locked() == set()


def test_mask_hides_all_secrets(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "abc", "sonarr_api_key": ""})
    masked = config.mask(config.load())
    assert masked["jellyfin_api_key"] == "••••••••"
    assert masked["sonarr_api_key"] == ""
    assert masked["age_days"] == 180


def test_save_preserves_masked_secret(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "real-key"})
    config.save({"jellyfin_api_key": "••••••••", "age_days": 30})
    cfg = config.load()
    assert cfg["jellyfin_api_key"] == "real-key"
    assert cfg["age_days"] == 30


def test_save_sets_owner_only_permissions(tmp_path, monkeypatch):
    import os
    path = _cfg_file(tmp_path, monkeypatch)
    config.save({"jellyfin_api_key": "s3cret"})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_save_rejects_unknown_keys(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    saved = config.save({"nonsense": 1, "age_days": 45})
    assert "nonsense" not in saved
    assert saved["age_days"] == 45
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_config.py -v
```

Expected: FAIL — `AttributeError: module 'config' has no attribute 'mask'`.

- [ ] **Step 3: Implement**

Replace the contents of `app/config.py`:

```python
import json
import os
import threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")

MASK = "••••••••"

DEFAULTS = {
    "transmission_host":     "192.168.1.132",
    "transmission_port":     "9091",
    "transmission_user":     "",
    "transmission_pass":     "",
    "transmission_rpc_path": "/transmission/rpc",
    "exclude_paths":         [],
    "jellyfin_url":          "",
    "jellyfin_api_key":      "",
    "sonarr_url":            "",
    "sonarr_api_key":        "",
    "radarr_url":            "",
    "radarr_api_key":        "",
    "age_days":              180,
    "idle_days":             90,
    "seed_guard":            True,
    "max_titles_per_run":    50,
    "max_bytes_per_run":     1099511627776,
    "library_roots":         ["/share"],
}

SECRET_KEYS = {
    "transmission_pass",
    "jellyfin_api_key",
    "sonarr_api_key",
    "radarr_api_key",
}

ENV_OVERRIDES = {
    "jellyfin_api_key": "JELLYFIN_API_KEY",
    "sonarr_api_key":   "SONARR_API_KEY",
    "radarr_api_key":   "RADARR_API_KEY",
}

_INT_KEYS = {"age_days", "idle_days", "max_titles_per_run", "max_bytes_per_run"}

_lock = threading.Lock()


def env_locked():
    """Secret keys currently supplied by the environment."""
    return {k for k, env in ENV_OVERRIDES.items() if os.environ.get(env)}


def load():
    with _lock:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        merged = {**DEFAULTS, **data}

    for key, env in ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value:
            merged[key] = value

    return merged


def mask(cfg):
    """Copy of cfg with non-empty secrets replaced by MASK."""
    out = dict(cfg)
    for key in SECRET_KEYS:
        if out.get(key):
            out[key] = MASK
    return out


def save(data):
    with _lock:
        existing = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                existing = json.load(f)

        incoming = {k: v for k, v in data.items() if k in DEFAULTS}

        # A masked secret means "unchanged" — keep the stored value.
        for key in SECRET_KEYS:
            if key in incoming and str(incoming[key]).startswith("••"):
                incoming.pop(key)

        if "exclude_paths" in incoming:
            raw = incoming["exclude_paths"]
            if isinstance(raw, str):
                raw = [p for p in raw.splitlines() if p.strip()]
            incoming["exclude_paths"] = [p.strip() for p in raw if str(p).strip()]

        if "library_roots" in incoming:
            raw = incoming["library_roots"]
            if isinstance(raw, str):
                raw = [p for p in raw.splitlines() if p.strip()]
            incoming["library_roots"] = [p.strip() for p in raw if str(p).strip()]

        for key in _INT_KEYS:
            if key in incoming:
                incoming[key] = int(incoming[key])

        if "seed_guard" in incoming:
            incoming["seed_guard"] = bool(incoming["seed_guard"])

        merged = {**DEFAULTS, **existing, **incoming}

        directory = os.path.dirname(CONFIG_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(merged, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)

        return merged
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_config.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Update `app.py`'s masking to use the shared helper**

In `app/app.py`, replace the body of `get_settings` (lines 93–98):

```python
@app.route("/api/settings", methods=["GET"])
def get_settings():
    safe = cfg_mod.mask(cfg_mod.load())
    safe["env_locked"] = sorted(cfg_mod.env_locked())
    return jsonify(safe)
```

And in `save_settings`, delete the manual `transmission_pass` placeholder check (lines 108–110) — `config.save` now handles every secret uniformly.

- [ ] **Step 6: Run the full suite and commit**

```bash
pytest -v
git add app/config.py app/app.py tests/test_config.py
git commit -m "feat: extend config with service credentials, thresholds, and masking"
```

---

## Task 5: Bucket classification engine

The decision core. Pure functions, no I/O, exhaustively tested at every boundary.

**Files:**
- Create: `app/cleanup/__init__.py`
- Create: `app/cleanup/buckets.py`
- Create: `tests/test_buckets.py`

**Interfaces:**
- Consumes: nothing
- Produces: `buckets.classify(kind, episodes, watched, last_played, progress_pct) -> str`; `buckets.PRETICKED`; `buckets.LABELS`; `buckets.quality_flags(added, last_played, has_hardlink) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_buckets.py`:

```python
from datetime import datetime
import pytest
from cleanup import buckets

T = datetime(2026, 1, 1)


@pytest.mark.parametrize("episodes,watched,last_played,expected", [
    (10, 0, None, "A"),     # never opened
    (10, 10, T,   "B"),     # fully watched
    (43, 42, T,   "C1"),    # 97% — near complete
    (10, 8,  T,   "C1"),    # exactly 80% — boundary, inclusive
    (10, 0,  T,   "C2"),    # sampled, never finished an episode
    (10, 7,  T,   "C3"),    # 70% — mid-watch
    (198, 1, T,   "C3"),    # abandoned immediately
])
def test_series_buckets(episodes, watched, last_played, expected):
    assert buckets.classify("series", episodes, watched, last_played, 0.0) == expected


def test_series_80_percent_boundary_is_inclusive():
    assert buckets.classify("series", 10, 8, T, 0.0) == "C1"
    assert buckets.classify("series", 100, 79, T, 0.0) == "C3"
    assert buckets.classify("series", 100, 80, T, 0.0) == "C1"


def test_watched_exceeding_episodes_is_fully_watched():
    assert buckets.classify("series", 8, 9, T, 0.0) == "B"


def test_zero_episode_series_never_divides_by_zero():
    assert buckets.classify("series", 0, 0, None, 0.0) == "A"
    assert buckets.classify("series", 0, 0, T, 0.0) == "C2"


@pytest.mark.parametrize("watched,last_played,progress,expected", [
    (0, None, 0.0,  "A"),    # never opened
    (1, T,    100.0,"B"),    # watched
    (0, T,    0.0,  "C2"),   # opened, no resume point stored
    (0, T,    64.0, "C3"),   # genuinely mid-watch
])
def test_movie_buckets(watched, last_played, progress, expected):
    assert buckets.classify("movie", 1, watched, last_played, progress) == expected


def test_preticked_excludes_only_c3():
    assert buckets.PRETICKED == {"A", "B", "C1", "C2"}
    assert "C3" not in buckets.PRETICKED


def test_every_bucket_has_a_label():
    for key in ("A", "B", "C1", "C2", "C3"):
        assert buckets.LABELS[key]


def test_added_date_unreliable_flag():
    added = datetime(2025, 12, 29)
    played = datetime(2025, 11, 21)
    assert "added-date-unreliable" in buckets.quality_flags(added, played, False)


def test_no_flag_when_dates_are_ordered():
    added = datetime(2025, 1, 1)
    played = datetime(2025, 6, 1)
    assert buckets.quality_flags(added, played, False) == []


def test_no_flag_when_never_played():
    assert buckets.quality_flags(datetime(2025, 1, 1), None, False) == []


def test_hardlink_flag():
    assert "frees-less-than-listed" in buckets.quality_flags(T, None, True)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_buckets.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup'`.

- [ ] **Step 3: Implement**

Create `app/cleanup/__init__.py` as an empty file.

Create `app/cleanup/buckets.py`:

```python
"""Pure classification of cleanup candidates. No I/O belongs in this module."""

NEVER_OPENED = "A"
FULLY_WATCHED = "B"
NEAR_COMPLETE = "C1"
SAMPLED = "C2"
MID_WATCH = "C3"

PRETICKED = {NEVER_OPENED, FULLY_WATCHED, NEAR_COMPLETE, SAMPLED}

LABELS = {
    NEVER_OPENED:  "Never opened",
    FULLY_WATCHED: "Fully watched",
    NEAR_COMPLETE: "Near complete",
    SAMPLED:       "Sampled, dropped",
    MID_WATCH:     "Mid-watch",
}

NEAR_COMPLETE_RATIO = 0.8

# Jellyfin stores no resume point for the first few minutes of playback, so a
# movie with a play date but no stored position was opened and abandoned early.
MOVIE_SAMPLED_MAX_PROGRESS = 5.0


def classify(kind, episodes, watched, last_played, progress_pct):
    """Return the bucket identifier for one candidate.

    kind:         'series' or 'movie'
    episodes:     total episodes (1 for a movie)
    watched:      episodes played (0 or 1 for a movie)
    last_played:  datetime or None
    progress_pct: movie resume position, 0-100
    """
    if last_played is None and watched <= 0:
        return NEVER_OPENED

    if kind == "movie":
        if watched >= 1:
            return FULLY_WATCHED
        if progress_pct < MOVIE_SAMPLED_MAX_PROGRESS:
            return SAMPLED
        return MID_WATCH

    if episodes > 0 and watched >= episodes:
        return FULLY_WATCHED
    if watched <= 0:
        return SAMPLED
    if episodes > 0 and (watched / episodes) >= NEAR_COMPLETE_RATIO:
        return NEAR_COMPLETE
    return MID_WATCH


def quality_flags(added, last_played, has_hardlink):
    """Data-quality warnings surfaced in the UI."""
    flags = []
    if last_played is not None and added is not None and last_played < added:
        flags.append("added-date-unreliable")
    if has_hardlink:
        flags.append("frees-less-than-listed")
    return flags
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_buckets.py -v
```

Expected: 20 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup/ tests/test_buckets.py
git commit -m "feat: add pure bucket classification engine"
```

---

## Task 6: Jellyfin client

**Files:**
- Create: `app/clients/jellyfin.py`
- Create: `tests/test_jellyfin_client.py`

**Interfaces:**
- Consumes: `tests/fixtures/jellyfin_*.json` from Task 3
- Produces: `JellyfinClient(base_url, api_key)` with `.users()`, `.items(user_id, item_type)`, `.episodes(user_id, series_id)`, `.delete_item(item_id)`, `.refresh_library()`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jellyfin_client.py`:

```python
import pytest
import responses
from clients.jellyfin import JellyfinClient

BASE = "http://jf.local:8096"


@pytest.fixture
def client():
    return JellyfinClient(BASE, "test-key")


@responses.activate
def test_auth_header_uses_mediabrowser_token(client):
    responses.add(responses.GET, f"{BASE}/Users", json=[], status=200)
    client.users()
    assert 'MediaBrowser Token="test-key"' in responses.calls[0].request.headers["Authorization"]


@responses.activate
def test_items_requests_required_fields(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items",
                  json={"Items": [{"Id": "x"}]}, status=200)
    items = client.items("u1", "Series")
    assert items == [{"Id": "x"}]
    qs = responses.calls[0].request.url
    assert "IncludeItemTypes=Series" in qs
    assert "Recursive=true" in qs
    assert "DateLastMediaAdded" in qs


@responses.activate
def test_items_returns_empty_list_when_absent(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items", json={}, status=200)
    assert client.items("u1", "Movie") == []


@responses.activate
def test_episodes_scopes_to_parent(client):
    responses.add(responses.GET, f"{BASE}/Users/u1/Items",
                  json={"Items": []}, status=200)
    client.episodes("u1", "series-9")
    assert "ParentId=series-9" in responses.calls[0].request.url
    assert "IncludeItemTypes=Episode" in responses.calls[0].request.url


@responses.activate
def test_delete_item_issues_delete(client):
    responses.add(responses.DELETE, f"{BASE}/Items/abc", status=204)
    client.delete_item("abc")
    assert responses.calls[0].request.method == "DELETE"


@responses.activate
def test_delete_item_treats_404_as_success(client):
    responses.add(responses.DELETE, f"{BASE}/Items/gone", status=404)
    client.delete_item("gone")  # must not raise


@responses.activate
def test_delete_item_raises_on_server_error(client):
    responses.add(responses.DELETE, f"{BASE}/Items/x", status=500)
    with pytest.raises(Exception):
        client.delete_item("x")


@responses.activate
def test_refresh_library_posts(client):
    responses.add(responses.POST, f"{BASE}/Library/Refresh", status=204)
    client.refresh_library()
    assert responses.calls[0].request.method == "POST"


def test_base_url_trailing_slash_is_normalized():
    assert JellyfinClient(BASE + "/", "k").base_url == BASE
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_jellyfin_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'clients.jellyfin'`.

- [ ] **Step 3: Implement**

Create `app/clients/jellyfin.py`:

```python
import requests

ITEM_FIELDS = "Path,DateCreated,DateLastMediaAdded,MediaSources"


class JellyfinClient:
    def __init__(self, base_url, api_key, timeout=30):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f'MediaBrowser Token="{api_key}"'

    def _get(self, path, params=None):
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def users(self):
        return self._get("/Users")

    def items(self, user_id, item_type):
        data = self._get(f"/Users/{user_id}/Items", {
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "Fields": ITEM_FIELDS,
        })
        return data.get("Items", [])

    def episodes(self, user_id, series_id):
        data = self._get(f"/Users/{user_id}/Items", {
            "ParentId": series_id,
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Fields": "DateCreated,MediaSources",
        })
        return data.get("Items", [])

    def delete_item(self, item_id):
        resp = self._session.delete(f"{self.base_url}/Items/{item_id}", timeout=self.timeout)
        if resp.status_code == 404:
            return  # already gone — idempotent
        resp.raise_for_status()

    def refresh_library(self):
        resp = self._session.post(f"{self.base_url}/Library/Refresh", timeout=self.timeout)
        resp.raise_for_status()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_jellyfin_client.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/clients/jellyfin.py tests/test_jellyfin_client.py
git commit -m "feat: add jellyfin api client"
```

---

## Task 7: Sonarr/Radarr client

**Files:**
- Create: `app/clients/arr.py`
- Create: `tests/test_arr_client.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ArrClient(base_url, api_key, kind)` with `.list_items()`, `.delete_item(item_id)`; `kind` is `"sonarr"` or `"radarr"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arr_client.py`:

```python
import pytest
import responses
from clients.arr import ArrClient

BASE = "http://arr.local:8989"


@responses.activate
def test_sonarr_lists_series():
    responses.add(responses.GET, f"{BASE}/api/v3/series",
                  json=[{"id": 1, "path": "/share/series/Show"}], status=200)
    items = ArrClient(BASE, "k", "sonarr").list_items()
    assert items[0]["path"] == "/share/series/Show"


@responses.activate
def test_radarr_lists_movies():
    responses.add(responses.GET, f"{BASE}/api/v3/movie",
                  json=[{"id": 7, "path": "/share/movies/Film"}], status=200)
    items = ArrClient(BASE, "k", "radarr").list_items()
    assert items[0]["id"] == 7


@responses.activate
def test_api_key_sent_as_header():
    responses.add(responses.GET, f"{BASE}/api/v3/series", json=[], status=200)
    ArrClient(BASE, "secret-key", "sonarr").list_items()
    assert responses.calls[0].request.headers["X-Api-Key"] == "secret-key"


@responses.activate
def test_sonarr_delete_sends_correct_params():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/5", status=200)
    ArrClient(BASE, "k", "sonarr").delete_item(5)
    url = responses.calls[0].request.url
    assert "deleteFiles=true" in url
    assert "addImportListExclusion=false" in url


@responses.activate
def test_radarr_delete_sends_correct_params():
    responses.add(responses.DELETE, f"{BASE}/api/v3/movie/9", status=200)
    ArrClient(BASE, "k", "radarr").delete_item(9)
    url = responses.calls[0].request.url
    assert "deleteFiles=true" in url
    assert "addImportExclusion=false" in url


@responses.activate
def test_delete_treats_404_as_success():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/404", status=404)
    ArrClient(BASE, "k", "sonarr").delete_item(404)  # must not raise


@responses.activate
def test_delete_raises_on_server_error():
    responses.add(responses.DELETE, f"{BASE}/api/v3/series/1", status=500)
    with pytest.raises(Exception):
        ArrClient(BASE, "k", "sonarr").delete_item(1)


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        ArrClient(BASE, "k", "lidarr")
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_arr_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'clients.arr'`.

- [ ] **Step 3: Implement**

Create `app/clients/arr.py`:

```python
import requests

# Sonarr and Radarr share the v3 API shape but differ in resource name and in
# the spelling of the exclusion parameter.
KINDS = {
    "sonarr": {"resource": "series", "exclusion_param": "addImportListExclusion"},
    "radarr": {"resource": "movie",  "exclusion_param": "addImportExclusion"},
}


class ArrClient:
    def __init__(self, base_url, api_key, kind, timeout=60):
        if kind not in KINDS:
            raise ValueError(f"unknown arr kind: {kind}")
        self.kind = kind
        self.resource = KINDS[kind]["resource"]
        self.exclusion_param = KINDS[kind]["exclusion_param"]
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["X-Api-Key"] = api_key

    def list_items(self):
        resp = self._session.get(
            f"{self.base_url}/api/v3/{self.resource}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete_item(self, item_id):
        """Delete the entry and its files. Never adds an import exclusion."""
        resp = self._session.delete(
            f"{self.base_url}/api/v3/{self.resource}/{item_id}",
            params={"deleteFiles": "true", self.exclusion_param: "false"},
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return  # already gone — idempotent
        resp.raise_for_status()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_arr_client.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/clients/arr.py tests/test_arr_client.py
git commit -m "feat: add sonarr/radarr client"
```

---

## Task 8: Candidate normalization and ownership matching

**Files:**
- Create: `app/cleanup/candidates.py`
- Create: `tests/test_candidates.py`

**Interfaces:**
- Consumes: `cleanup.buckets`, Jellyfin item dicts, *arr item dicts
- Produces: `Candidate` dataclass; `parse_dt(value) -> datetime|None`; `is_stale(added, last_played, now, age_days, idle_days) -> bool`; `build_owner_index(sonarr_items, radarr_items) -> dict`; `match_owner(path, index) -> (str|None, int|None)`; `from_series(item, owner_index) -> Candidate`; `from_movie(item, owner_index) -> Candidate`

**Note from Task 3:** if the field report showed `DateLastMediaAdded` or `UserData.UnplayedItemCount` MISSING, `from_series` takes an extra `episodes` argument built from `JellyfinClient.episodes()` instead of reading the Series object. Confirm before implementing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_candidates.py`:

```python
from datetime import datetime
import pytest
from cleanup import candidates as C

NOW = datetime(2026, 9, 6)


def test_parse_dt_handles_jellyfin_iso():
    assert C.parse_dt("2025-08-23T22:44:18.6029234Z") == datetime(2025, 8, 23, 22, 44, 18)


def test_parse_dt_handles_none_and_empty():
    assert C.parse_dt(None) is None
    assert C.parse_dt("") is None


def test_stale_requires_both_conditions():
    old = datetime(2025, 1, 1)
    recent = datetime(2026, 8, 1)
    # old enough, idle long enough
    assert C.is_stale(old, datetime(2026, 1, 1), NOW, 180, 90) is True
    # old enough but played recently
    assert C.is_stale(old, recent, NOW, 180, 90) is False
    # never played but added recently
    assert C.is_stale(datetime(2026, 8, 1), None, NOW, 180, 90) is False
    # never played and old
    assert C.is_stale(old, None, NOW, 180, 90) is True


def test_owner_index_maps_paths():
    idx = C.build_owner_index(
        [{"id": 1, "path": "/share/series/Show"}],
        [{"id": 7, "path": "/share/movies/Film"}],
    )
    assert idx["/share/series/Show"] == ("sonarr", 1)
    assert idx["/share/movies/Film"] == ("radarr", 7)


def test_match_owner_exact():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/Show", idx) == ("sonarr", 1)


def test_match_owner_by_prefix():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/Show/Season 1/ep.mkv", idx) == ("sonarr", 1)


def test_match_owner_does_not_match_sibling_prefix():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/ShowTwo", idx) == (None, None)


def test_match_owner_unowned():
    assert C.match_owner("/share/reality/Thing", {}) == (None, None)


def _series_item(**over):
    item = {
        "Id": "s1",
        "Name": "Test Show",
        "Path": "/share/series/Show",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "DateLastMediaAdded": "2025-02-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None, "UnplayedItemCount": 2},
        "RecursiveItemCount": 10,
    }
    item.update(over)
    return item


def test_from_series_computes_watched_from_unplayed_count():
    c = C.from_series(_series_item(), {})
    assert c.episodes == 10
    assert c.watched == 8
    assert c.bucket == "C1"


def test_from_series_uses_date_last_media_added():
    c = C.from_series(_series_item(), {})
    assert c.added == datetime(2025, 2, 1)


def test_from_series_falls_back_to_date_created():
    item = _series_item()
    del item["DateLastMediaAdded"]
    assert C.from_series(item, {}).added == datetime(2025, 1, 1)


def test_from_series_assigns_owner():
    idx = {"/share/series/Show": ("sonarr", 4)}
    c = C.from_series(_series_item(), idx)
    assert (c.owner, c.owner_id) == ("sonarr", 4)


def test_from_series_unowned_is_none():
    c = C.from_series(_series_item(), {})
    assert c.owner is None


def test_from_series_flags_unreliable_added_date():
    c = C.from_series(_series_item(
        DateLastMediaAdded="2025-12-29T00:00:00.0000000Z",
        UserData={"Played": False, "LastPlayedDate": "2025-11-21T00:00:00.0000000Z",
                  "UnplayedItemCount": 0},
    ), {})
    assert "added-date-unreliable" in c.flags


def _movie_item(**over):
    item = {
        "Id": "m1",
        "Name": "Test Film",
        "Path": "/share/movies/Film/film.mkv",
        "DateCreated": "2025-03-01T00:00:00.0000000Z",
        "UserData": {"Played": True, "LastPlayedDate": "2026-01-01T00:00:00.0000000Z",
                     "PlayedPercentage": 100.0},
        "MediaSources": [{"Size": 4294967296}],
    }
    item.update(over)
    return item


def test_from_movie_watched():
    c = C.from_movie(_movie_item(), {})
    assert c.kind == "movie"
    assert c.watched == 1
    assert c.bucket == "B"
    assert c.size_bytes == 4294967296


def test_from_movie_sampled_has_no_resume_point():
    c = C.from_movie(_movie_item(
        UserData={"Played": False, "LastPlayedDate": "2026-01-01T00:00:00.0000000Z"},
    ), {})
    assert c.progress_pct == 0.0
    assert c.bucket == "C2"


def test_from_movie_handles_missing_media_sources():
    item = _movie_item()
    del item["MediaSources"]
    assert C.from_movie(item, {}).size_bytes == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_candidates.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup.candidates'`.

- [ ] **Step 3: Implement**

Create `app/cleanup/candidates.py`:

```python
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cleanup import buckets


@dataclass
class Candidate:
    jf_id: str
    kind: str
    title: str
    path: str
    size_bytes: int
    added: datetime
    last_played: datetime
    episodes: int
    watched: int
    progress_pct: float
    owner: str
    owner_id: int
    bucket: str
    flags: list = field(default_factory=list)

    def to_dict(self):
        return {
            "jf_id": self.jf_id,
            "kind": self.kind,
            "title": self.title,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "added": self.added.isoformat() if self.added else None,
            "last_played": self.last_played.isoformat() if self.last_played else None,
            "episodes": self.episodes,
            "watched": self.watched,
            "progress_pct": self.progress_pct,
            "owner": self.owner,
            "owner_id": self.owner_id,
            "bucket": self.bucket,
            "flags": list(self.flags),
        }


def parse_dt(value):
    """Parse a Jellyfin timestamp. Sub-second precision exceeds datetime's range."""
    if not value:
        return None
    text = str(value).replace("Z", "").split("+")[0]
    if "." in text:
        text = text.split(".")[0]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_stale(added, last_played, now, age_days, idle_days):
    if added is None or added > now - timedelta(days=age_days):
        return False
    if last_played is None:
        return True
    return last_played < now - timedelta(days=idle_days)


def build_owner_index(sonarr_items, radarr_items):
    index = {}
    for item in sonarr_items or []:
        if item.get("path"):
            index[os.path.normpath(item["path"])] = ("sonarr", item["id"])
    for item in radarr_items or []:
        if item.get("path"):
            index[os.path.normpath(item["path"])] = ("radarr", item["id"])
    return index


def match_owner(path, index):
    """Exact match, else the nearest ancestor directory. Never a sibling prefix."""
    if not path:
        return (None, None)
    norm = os.path.normpath(path)
    if norm in index:
        return index[norm]
    for owned_path, owner in index.items():
        if norm.startswith(owned_path + os.sep):
            return owner
    return (None, None)


def _size_of(item):
    for source in item.get("MediaSources") or []:
        if source.get("Size"):
            return int(source["Size"])
    return int(item.get("Size") or 0)


def from_series(item, owner_index, episodes=None, watched=None):
    user = item.get("UserData") or {}
    total = episodes if episodes is not None else int(item.get("RecursiveItemCount") or 0)
    if watched is None:
        unplayed = int(user.get("UnplayedItemCount") or 0)
        watched = max(total - unplayed, 0)

    added = parse_dt(item.get("DateLastMediaAdded")) or parse_dt(item.get("DateCreated"))
    last_played = parse_dt(user.get("LastPlayedDate"))
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)

    return Candidate(
        jf_id=item.get("Id"),
        kind="series",
        title=item.get("Name") or "",
        path=path,
        size_bytes=_size_of(item),
        added=added,
        last_played=last_played,
        episodes=total,
        watched=watched,
        progress_pct=0.0,
        owner=owner,
        owner_id=owner_id,
        bucket=buckets.classify("series", total, watched, last_played, 0.0),
        flags=buckets.quality_flags(added, last_played, False),
    )


def from_movie(item, owner_index):
    user = item.get("UserData") or {}
    watched = 1 if user.get("Played") else 0
    progress = float(user.get("PlayedPercentage") or 0.0)

    added = parse_dt(item.get("DateCreated"))
    last_played = parse_dt(user.get("LastPlayedDate"))
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)

    return Candidate(
        jf_id=item.get("Id"),
        kind="movie",
        title=item.get("Name") or "",
        path=path,
        size_bytes=_size_of(item),
        added=added,
        last_played=last_played,
        episodes=1,
        watched=watched,
        progress_pct=progress,
        owner=owner,
        owner_id=owner_id,
        bucket=buckets.classify("movie", 1, watched, last_played, progress),
        flags=buckets.quality_flags(added, last_played, False),
    )
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_candidates.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup/candidates.py tests/test_candidates.py
git commit -m "feat: add candidate normalization and ownership matching"
```

---

## Task 9: Path safety guard

The invariant everything else rests on. Adversarial tests are mandatory.

**Files:**
- Create: `app/cleanup/paths.py`
- Create: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing
- Produces: `PathOutsideRoots` exception; `assert_within_roots(path, roots) -> str`; `delete_file(path, roots) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_paths.py`:

```python
import os
import pytest
from cleanup.paths import assert_within_roots, delete_file, PathOutsideRoots


def test_accepts_path_inside_root(tmp_path):
    root = str(tmp_path)
    target = os.path.join(root, "a", "b.mkv")
    assert assert_within_roots(target, [root]) == os.path.normpath(target)


def test_rejects_path_outside_root(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots("/etc/passwd", [str(tmp_path)])


def test_rejects_traversal(tmp_path):
    root = str(tmp_path / "share")
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(os.path.join(root, "..", "..", "etc", "passwd"), [root])


def test_rejects_sibling_prefix(tmp_path):
    root = str(tmp_path / "share")
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(tmp_path / "shared" / "x.mkv"), [root])


def test_rejects_relative_path(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots("relative/path.mkv", [str(tmp_path)])


def test_rejects_empty_roots(tmp_path):
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(tmp_path / "x.mkv"), [])


def test_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x" * 8)
    link = root / "link.mkv"
    os.symlink(outside, link)
    with pytest.raises(PathOutsideRoots):
        assert_within_roots(str(link), [str(root)])


def test_delete_file_removes_and_reports_bytes(tmp_path):
    root = tmp_path
    target = root / "a.mkv"
    target.write_bytes(b"x" * 100)
    assert delete_file(str(target), [str(root)]) == 100
    assert not target.exists()


def test_delete_file_missing_is_zero_not_error(tmp_path):
    assert delete_file(str(tmp_path / "ghost.mkv"), [str(tmp_path)]) == 0


def test_delete_file_refuses_outside_root(tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    root = tmp_path / "share"
    root.mkdir()
    with pytest.raises(PathOutsideRoots):
        delete_file(str(outside), [str(root)])
    assert outside.exists()


def test_delete_file_prunes_empty_parent(tmp_path):
    nested = tmp_path / "show" / "season"
    nested.mkdir(parents=True)
    target = nested / "ep.mkv"
    target.write_bytes(b"x")
    delete_file(str(target), [str(tmp_path)])
    assert not nested.exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_paths.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup.paths'`.

- [ ] **Step 3: Implement**

Create `app/cleanup/paths.py`:

```python
import os


class PathOutsideRoots(Exception):
    """Raised when a path resolves outside every configured root."""


def assert_within_roots(path, roots):
    """Return the resolved path, or raise if it escapes every root.

    Symlinks are resolved before the check, so a link inside a root that
    points outside it is rejected.
    """
    if not path or not os.path.isabs(path):
        raise PathOutsideRoots(f"not an absolute path: {path!r}")

    resolved = os.path.realpath(path)

    for root in roots or []:
        real_root = os.path.realpath(root)
        if resolved == real_root or resolved.startswith(real_root + os.sep):
            return os.path.normpath(resolved)

    raise PathOutsideRoots(f"path outside configured roots: {path!r}")


def delete_file(path, roots):
    """Delete one file inside the roots. Returns bytes freed (0 if absent)."""
    safe = assert_within_roots(path, roots)

    try:
        size = os.stat(safe).st_size
    except FileNotFoundError:
        return 0

    os.remove(safe)

    parent = os.path.dirname(safe)
    try:
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass

    return size
```

Note: `assert_within_roots` resolves symlinks, so a nonexistent path still normalizes safely — `realpath` does not require existence.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_paths.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup/paths.py tests/test_paths.py
git commit -m "feat: add path safety guard"
```

---

## Task 10: Run journal

**Files:**
- Create: `app/cleanup/journal.py`
- Create: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing
- Produces: `journal.append(entry)`; `journal.read_recent(limit=100) -> list[dict]`; `journal.JOURNAL_PATH`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_journal.py`:

```python
import json
from cleanup import journal


def _redirect(tmp_path, monkeypatch):
    path = tmp_path / "cleanup-journal.jsonl"
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(path))
    return path


def test_append_writes_one_line_per_entry(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A", "status": "deleted"})
    journal.append({"title": "B", "status": "failed"})
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["title"] == "A"


def test_append_adds_timestamp(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A"})
    assert "ts" in journal.read_recent()[0]


def test_read_recent_returns_newest_first(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    for name in ("A", "B", "C"):
        journal.append({"title": name})
    assert [e["title"] for e in journal.read_recent()] == ["C", "B", "A"]


def test_read_recent_respects_limit(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    for i in range(10):
        journal.append({"title": str(i)})
    assert len(journal.read_recent(limit=3)) == 3


def test_read_recent_on_missing_file(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    assert journal.read_recent() == []


def test_corrupt_line_is_skipped(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    path.write_text('{"title": "good"}\nnot json\n')
    assert [e["title"] for e in journal.read_recent()] == ["good"]


def test_secrets_are_never_written(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    journal.append({"title": "A", "jellyfin_api_key": "leak", "sonarr_api_key": "leak"})
    assert "leak" not in path.read_text()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_journal.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup.journal'`.

- [ ] **Step 3: Implement**

Create `app/cleanup/journal.py`:

```python
import json
import os
import threading
from datetime import datetime

import config as cfg_mod

JOURNAL_PATH = os.environ.get("JOURNAL_PATH", "/config/cleanup-journal.jsonl")

_lock = threading.Lock()


def append(entry):
    """Append one entry. Append-only: existing lines are never rewritten."""
    record = {k: v for k, v in entry.items() if k not in cfg_mod.SECRET_KEYS}
    record["ts"] = datetime.now().isoformat(timespec="seconds")

    with _lock:
        directory = os.path.dirname(JOURNAL_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_recent(limit=100):
    """Return the most recent entries, newest first. Corrupt lines are skipped."""
    if not os.path.exists(JOURNAL_PATH):
        return []

    with _lock:
        with open(JOURNAL_PATH) as f:
            lines = f.readlines()

    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_journal.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cleanup/journal.py tests/test_journal.py
git commit -m "feat: add append-only run journal"
```

---

## Task 11: Deletion pipeline

Orchestration. Step ordering is the correctness property under test.

**Files:**
- Create: `app/cleanup/pipeline.py`
- Create: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `cleanup.paths`, `cleanup.journal`, `clients.arr.ArrClient`, `clients.jellyfin.JellyfinClient`, `clients.transmission`
- Produces: `Clients` dataclass; `plan(candidates, cfg) -> dict`; `execute(candidates, cfg, clients) -> list[dict]`; `should_keep_seeding(torrent) -> bool`; `BlastRadiusExceeded`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
import os
import pytest
from cleanup import pipeline, journal
from cleanup.candidates import Candidate
from datetime import datetime


@pytest.fixture(autouse=True)
def _journal_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "j.jsonl"))


def _candidate(**over):
    base = dict(
        jf_id="s1", kind="series", title="Show", path="/share/series/Show",
        size_bytes=1000, added=datetime(2025, 1, 1), last_played=None,
        episodes=10, watched=0, progress_pct=0.0,
        owner="sonarr", owner_id=4, bucket="A", flags=[],
    )
    base.update(over)
    return Candidate(**base)


class FakeArr:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


class FakeJellyfin:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


class FakeTransmission:
    def __init__(self, torrents=None):
        self.torrents = torrents or []
        self.removed = []

    def get_all_torrents(self):
        return self.torrents

    def remove_torrent(self, tid, delete_data=True):
        self.removed.append(tid)

    def is_deletable(self, torrent):
        return True


def _clients(**over):
    c = pipeline.Clients(
        sonarr=FakeArr(), radarr=FakeArr(),
        jellyfin=FakeJellyfin(), transmission=FakeTransmission(),
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


CFG = {"library_roots": ["/share"], "seed_guard": True,
       "max_titles_per_run": 50, "max_bytes_per_run": 10 ** 12}


def test_plan_reports_totals_without_deleting():
    clients = _clients()
    result = pipeline.plan([_candidate()], CFG)
    assert result["count"] == 1
    assert result["titles"][0]["title"] == "Show"
    assert clients.sonarr.deleted == []


def test_plan_lists_steps_for_owned_title():
    steps = pipeline.plan([_candidate()], CFG)["titles"][0]["steps"]
    assert steps[0].startswith("sonarr:")
    assert any(s.startswith("jellyfin:") for s in steps)


def test_plan_skips_arr_step_for_unowned():
    steps = pipeline.plan([_candidate(owner=None, owner_id=None)], CFG)["titles"][0]["steps"]
    assert not any(s.startswith("sonarr:") for s in steps)
    assert any(s.startswith("files:") for s in steps)


def test_plan_raises_when_over_title_cap():
    cands = [_candidate(jf_id=str(i)) for i in range(5)]
    cfg = {**CFG, "max_titles_per_run": 3}
    with pytest.raises(pipeline.BlastRadiusExceeded):
        pipeline.plan(cands, cfg)


def test_plan_raises_when_over_byte_cap():
    cfg = {**CFG, "max_bytes_per_run": 500}
    with pytest.raises(pipeline.BlastRadiusExceeded):
        pipeline.plan([_candidate(size_bytes=1000)], cfg)


def test_execute_deletes_from_arr_then_jellyfin():
    clients = _clients()
    results = pipeline.execute([_candidate()], CFG, clients)
    assert clients.sonarr.deleted == [4]
    assert clients.jellyfin.deleted == ["s1"]
    assert results[0]["status"] == "deleted"


def test_execute_uses_radarr_for_movies():
    clients = _clients()
    pipeline.execute([_candidate(kind="movie", owner="radarr", owner_id=9)], CFG, clients)
    assert clients.radarr.deleted == [9]
    assert clients.sonarr.deleted == []


def test_execute_records_partial_on_jellyfin_failure():
    class Boom(FakeJellyfin):
        def delete_item(self, item_id):
            raise RuntimeError("jellyfin down")

    clients = _clients(jellyfin=Boom())
    results = pipeline.execute([_candidate()], CFG, clients)
    assert results[0]["status"] == "partial"
    assert clients.sonarr.deleted == [4]
    completed = [s["step"] for s in results[0]["steps"] if s["status"] == "ok"]
    assert any(s.startswith("sonarr") for s in completed)


def test_execute_records_failed_when_first_step_fails():
    class Boom(FakeArr):
        def delete_item(self, item_id):
            raise RuntimeError("sonarr down")

    clients = _clients(sonarr=Boom())
    results = pipeline.execute([_candidate()], CFG, clients)
    assert results[0]["status"] == "failed"
    assert clients.jellyfin.deleted == []


def test_execute_writes_journal_entry():
    pipeline.execute([_candidate()], CFG, _clients())
    entries = journal.read_recent()
    assert entries[0]["title"] == "Show"


def test_execute_continues_after_one_title_fails():
    class BoomOnce(FakeArr):
        def delete_item(self, item_id):
            if item_id == 1:
                raise RuntimeError("nope")
            self.deleted.append(item_id)

    clients = _clients(sonarr=BoomOnce())
    results = pipeline.execute(
        [_candidate(jf_id="a", owner_id=1), _candidate(jf_id="b", owner_id=2)],
        CFG, clients)
    assert results[0]["status"] == "failed"
    assert results[1]["status"] == "deleted"


def test_seed_guard_keeps_unsatisfied_torrent():
    torrent = {"uploadRatio": 0.4, "seedRatioLimit": 1.0, "seedRatioMode": 1}
    assert pipeline.should_keep_seeding(torrent) is True


def test_seed_guard_releases_satisfied_torrent():
    torrent = {"uploadRatio": 1.5, "seedRatioLimit": 1.0, "seedRatioMode": 1}
    assert pipeline.should_keep_seeding(torrent) is False


def test_seed_guard_ignores_torrent_with_no_limit():
    torrent = {"uploadRatio": 0.1, "seedRatioLimit": 0, "seedRatioMode": 0}
    assert pipeline.should_keep_seeding(torrent) is False
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_pipeline.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'cleanup.pipeline'`.

- [ ] **Step 3: Implement**

Create `app/cleanup/pipeline.py`:

```python
"""Forward-only deletion pipeline. Deletions cannot be undone."""
import os
from dataclasses import dataclass

from cleanup import journal
from cleanup.paths import assert_within_roots, delete_file, PathOutsideRoots


class BlastRadiusExceeded(Exception):
    """Selection exceeds the configured per-run caps."""


@dataclass
class Clients:
    sonarr: object
    radarr: object
    jellyfin: object
    transmission: object


# seedRatioMode: 0 = global, 1 = per-torrent limit, 2 = seed forever
_MODE_UNLIMITED = 2


def should_keep_seeding(torrent):
    """True when the torrent has not met its seed ratio and must be kept."""
    mode = torrent.get("seedRatioMode", 0)
    if mode == _MODE_UNLIMITED:
        return True
    limit = float(torrent.get("seedRatioLimit") or 0)
    if limit <= 0:
        return False
    return float(torrent.get("uploadRatio") or 0) < limit


def _steps_for(candidate):
    steps = []
    if candidate.owner:
        steps.append(f"{candidate.owner}:delete/{candidate.owner_id}")
    else:
        steps.append(f"files:delete/{candidate.path}")
    steps.append("transmission:sweep")
    steps.append(f"jellyfin:delete/{candidate.jf_id}")
    return steps


def plan(candidates, cfg):
    """Describe what execute() would do. Deletes nothing."""
    max_titles = int(cfg.get("max_titles_per_run") or 0)
    max_bytes = int(cfg.get("max_bytes_per_run") or 0)
    total_bytes = sum(c.size_bytes for c in candidates)

    if max_titles and len(candidates) > max_titles:
        raise BlastRadiusExceeded(
            f"{len(candidates)} titles exceeds the cap of {max_titles}")
    if max_bytes and total_bytes > max_bytes:
        raise BlastRadiusExceeded(
            f"{total_bytes} bytes exceeds the cap of {max_bytes}")

    titles = []
    for c in candidates:
        titles.append({
            "jf_id": c.jf_id,
            "title": c.title,
            "owner": c.owner,
            "size_bytes": c.size_bytes,
            "real_bytes": _real_bytes(c, cfg),
            "steps": _steps_for(c),
        })

    return {
        "count": len(candidates),
        "total_bytes": total_bytes,
        "real_bytes": sum(t["real_bytes"] for t in titles),
        "titles": titles,
    }


def _real_bytes(candidate, cfg):
    """Bytes actually reclaimed: files with extra hardlinks free nothing."""
    roots = cfg.get("library_roots") or []
    try:
        safe = assert_within_roots(candidate.path, roots)
    except PathOutsideRoots:
        return 0

    total = 0
    if os.path.isfile(safe):
        paths = [safe]
    else:
        paths = []
        for dirpath, _dirs, files in os.walk(safe):
            paths.extend(os.path.join(dirpath, f) for f in files)

    for path in paths:
        try:
            info = os.stat(path)
        except OSError:
            continue
        if info.st_nlink <= 1:
            total += info.st_size
    return total


def execute(candidates, cfg, clients):
    """Run the pipeline. Each title is independent; one failure never aborts the rest."""
    results = []
    for candidate in candidates:
        results.append(_execute_one(candidate, cfg, clients))
    return results


def _execute_one(candidate, cfg, clients):
    steps = []
    freed = 0
    status = "deleted"

    # 1. Remove from the *arr that owns it, or delete files directly.
    try:
        if candidate.owner == "sonarr":
            clients.sonarr.delete_item(candidate.owner_id)
            steps.append({"step": f"sonarr:delete/{candidate.owner_id}", "status": "ok", "detail": ""})
        elif candidate.owner == "radarr":
            clients.radarr.delete_item(candidate.owner_id)
            steps.append({"step": f"radarr:delete/{candidate.owner_id}", "status": "ok", "detail": ""})
        else:
            freed += _delete_tree(candidate.path, cfg.get("library_roots") or [])
            steps.append({"step": "files:delete", "status": "ok", "detail": str(freed)})
    except Exception as exc:
        steps.append({"step": "owner:delete", "status": "error", "detail": str(exc)})
        return _finish(candidate, "failed", steps, freed)

    # 2. Sweep torrents whose library link is now gone.
    try:
        kept = _sweep_torrents(cfg, clients)
        steps.append({"step": "transmission:sweep", "status": "ok", "detail": f"kept={kept}"})
    except Exception as exc:
        steps.append({"step": "transmission:sweep", "status": "error", "detail": str(exc)})
        status = "partial"

    # 3. Drop the Jellyfin entry and its metadata.
    try:
        clients.jellyfin.delete_item(candidate.jf_id)
        steps.append({"step": f"jellyfin:delete/{candidate.jf_id}", "status": "ok", "detail": ""})
    except Exception as exc:
        steps.append({"step": "jellyfin:delete", "status": "error", "detail": str(exc)})
        status = "partial"

    return _finish(candidate, status, steps, freed)


def _delete_tree(path, roots):
    safe = assert_within_roots(path, roots)
    if os.path.isfile(safe):
        return delete_file(safe, roots)

    freed = 0
    for dirpath, _dirs, files in os.walk(safe, topdown=False):
        for name in files:
            freed += delete_file(os.path.join(dirpath, name), roots)
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return freed


def _sweep_torrents(cfg, clients):
    """Remove torrents with no remaining hardlink. Returns the count kept for seeding."""
    kept = 0
    for torrent in clients.transmission.get_all_torrents():
        if not clients.transmission.is_deletable(torrent):
            continue
        if cfg.get("seed_guard") and should_keep_seeding(torrent):
            kept += 1
            continue
        clients.transmission.remove_torrent(torrent["id"], delete_data=True)
    return kept


def _finish(candidate, status, steps, freed):
    result = {
        "jf_id": candidate.jf_id,
        "title": candidate.title,
        "owner": candidate.owner,
        "status": status,
        "steps": steps,
        "bytes_freed": freed,
    }
    journal.append(dict(result))
    return result
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_pipeline.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Run the whole suite**

```bash
pytest -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/cleanup/pipeline.py tests/test_pipeline.py
git commit -m "feat: add forward-only deletion pipeline"
```

---

## Task 12: HTTP endpoints

**Files:**
- Modify: `app/app.py` (append routes; extend `test_connection`)
- Create: `tests/test_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 4–11
- Produces: routes `/library`, `/api/library/candidates`, `/api/library/plan`, `/api/library/execute`, `/api/library/journal`, `/api/test-connection/<service>`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes.py`:

```python
import json
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    from cleanup import journal
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "j.jsonl"))
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_library_page_renders(client):
    assert client.get("/library").status_code == 200


def test_settings_never_leaks_secret(client, tmp_path):
    import config
    config.save({"jellyfin_api_key": "super-secret"})
    body = client.get("/api/settings").get_data(as_text=True)
    assert "super-secret" not in body
    assert "••••••••" in body


def test_settings_reports_env_locked_keys(client, monkeypatch):
    monkeypatch.setenv("SONARR_API_KEY", "from-env")
    data = client.get("/api/settings").get_json()
    assert "sonarr_api_key" in data["env_locked"]


def test_candidates_requires_jellyfin_config(client):
    resp = client.get("/api/library/candidates")
    assert resp.status_code == 503
    assert "jellyfin" in resp.get_json()["error"].lower()


def test_execute_rejects_empty_selection(client):
    resp = client.post("/api/library/execute", json={"jf_ids": []})
    assert resp.status_code == 400


def test_execute_rejects_missing_body(client):
    assert client.post("/api/library/execute", json={}).status_code == 400


def test_journal_endpoint_returns_list(client):
    from cleanup import journal
    journal.append({"title": "X", "status": "deleted"})
    assert client.get("/api/library/journal").get_json()[0]["title"] == "X"


def test_test_connection_rejects_unknown_service(client):
    assert client.post("/api/test-connection/lidarr", json={}).status_code == 400
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_routes.py -v
```

Expected: FAIL — 404 on `/library`.

- [ ] **Step 3: Implement**

Append to `app/app.py`:

```python
import threading as _threading

from clients.jellyfin import JellyfinClient
from clients.arr import ArrClient
from clients import transmission as tx
from cleanup import buckets, candidates as cand, journal, pipeline

_run_lock = _threading.Lock()
_last_plan = {"ids": set()}


def _require(cfg, *keys):
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"not configured: {', '.join(missing)}")


def _build_clients(cfg):
    return pipeline.Clients(
        sonarr=ArrClient(cfg["sonarr_url"], cfg["sonarr_api_key"], "sonarr")
        if cfg.get("sonarr_url") else None,
        radarr=ArrClient(cfg["radarr_url"], cfg["radarr_api_key"], "radarr")
        if cfg.get("radarr_url") else None,
        jellyfin=JellyfinClient(cfg["jellyfin_url"], cfg["jellyfin_api_key"]),
        transmission=tx,
    )


def _scan(cfg):
    """Build the full candidate list. Merges user data across all Jellyfin users."""
    from datetime import datetime

    jf = JellyfinClient(cfg["jellyfin_url"], cfg["jellyfin_api_key"])

    sonarr_items = []
    radarr_items = []
    if cfg.get("sonarr_url"):
        sonarr_items = ArrClient(cfg["sonarr_url"], cfg["sonarr_api_key"], "sonarr").list_items()
    if cfg.get("radarr_url"):
        radarr_items = ArrClient(cfg["radarr_url"], cfg["radarr_api_key"], "radarr").list_items()
    owner_index = cand.build_owner_index(sonarr_items, radarr_items)

    merged = {}
    for user in jf.users():
        uid = user["Id"]
        for item in jf.items(uid, "Series"):
            _merge(merged, cand.from_series(item, owner_index))
        for item in jf.items(uid, "Movie"):
            _merge(merged, cand.from_movie(item, owner_index))

    now = datetime.now()
    age = int(cfg["age_days"])
    idle = int(cfg["idle_days"])
    return [c for c in merged.values()
            if cand.is_stale(c.added, c.last_played, now, age, idle)]


def _merge(store, candidate):
    """Keep the most-watched, most-recently-played view across users."""
    existing = store.get(candidate.jf_id)
    if existing is None:
        store[candidate.jf_id] = candidate
        return

    if candidate.watched > existing.watched:
        existing.watched = candidate.watched
    if candidate.last_played and (
            existing.last_played is None or candidate.last_played > existing.last_played):
        existing.last_played = candidate.last_played
    existing.progress_pct = max(existing.progress_pct, candidate.progress_pct)
    existing.bucket = buckets.classify(
        existing.kind, existing.episodes, existing.watched,
        existing.last_played, existing.progress_pct)
    existing.flags = buckets.quality_flags(existing.added, existing.last_played, False)


@app.route("/library")
def library_page():
    return render_template("library.html")


@app.route("/api/library/candidates")
def api_candidates():
    cfg = cfg_mod.load()
    try:
        _require(cfg, "jellyfin_url", "jellyfin_api_key")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    for key in ("age_days", "idle_days"):
        override = flask_request.args.get(key)
        if override:
            cfg[key] = int(override)

    try:
        found = _scan(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return jsonify({
        "candidates": [c.to_dict() for c in found],
        "preticked": sorted(buckets.PRETICKED),
        "labels": buckets.LABELS,
        "age_days": cfg["age_days"],
        "idle_days": cfg["idle_days"],
    })


def _selected(cfg, jf_ids):
    wanted = set(jf_ids)
    return [c for c in _scan(cfg) if c.jf_id in wanted]


@app.route("/api/library/plan", methods=["POST"])
def api_plan():
    cfg = cfg_mod.load()
    body = flask_request.get_json(force=True) or {}
    jf_ids = body.get("jf_ids") or []
    if not jf_ids:
        return jsonify({"error": "no titles selected"}), 400

    try:
        selection = _selected(cfg, jf_ids)
        result = pipeline.plan(selection, cfg)
    except pipeline.BlastRadiusExceeded as e:
        return jsonify({"error": str(e), "blast_radius": True}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    _last_plan["ids"] = {c.jf_id for c in selection}
    return jsonify(result)


@app.route("/api/library/execute", methods=["POST"])
def api_execute():
    cfg = cfg_mod.load()
    body = flask_request.get_json(force=True) or {}
    jf_ids = body.get("jf_ids") or []
    if not jf_ids:
        return jsonify({"error": "no titles selected"}), 400

    if set(jf_ids) != _last_plan["ids"]:
        return jsonify({"error": "selection changed since preflight; re-run the plan"}), 409

    if not _run_lock.acquire(blocking=False):
        return jsonify({"error": "a cleanup run is already in progress"}), 409

    try:
        selection = _selected(cfg, jf_ids)
        pipeline.plan(selection, cfg)  # re-check caps immediately before deleting
        results = pipeline.execute(selection, cfg, _build_clients(cfg))
    except pipeline.BlastRadiusExceeded as e:
        return jsonify({"error": str(e), "blast_radius": True}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        _run_lock.release()

    _last_plan["ids"] = set()
    return jsonify({"results": results})


@app.route("/api/library/journal")
def api_journal():
    return jsonify(journal.read_recent(limit=200))


@app.route("/api/test-connection/<service>", methods=["POST"])
def test_service_connection(service):
    data = flask_request.get_json(force=True) or {}
    cfg = cfg_mod.load()
    url = (data.get("url") or "").strip()
    key = (data.get("api_key") or "").strip()

    if service not in ("jellyfin", "sonarr", "radarr"):
        return jsonify({"error": "unknown service"}), 400

    if key.startswith("••") or not key:
        key = cfg.get(f"{service}_api_key", "")
    if not url:
        url = cfg.get(f"{service}_url", "")

    try:
        if service == "jellyfin":
            count = len(JellyfinClient(url, key).users())
            return jsonify({"status": "ok", "detail": f"{count} users"})
        count = len(ArrClient(url, key, service).list_items())
        return jsonify({"status": "ok", "detail": f"{count} items"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_routes.py -v
```

Expected: 8 passed. `test_library_page_renders` fails until Task 13 creates the template — create an empty `app/templates/library.html` now to satisfy it, then fill it in Task 13.

- [ ] **Step 5: Commit**

```bash
git add app/app.py app/templates/library.html tests/test_routes.py
git commit -m "feat: add library cleanup endpoints"
```

---

## Task 13: Library cleanup UI

**Files:**
- Modify: `app/templates/library.html`
- Modify: `app/templates/index.html` (nav link)

**Interfaces:**
- Consumes: `/api/library/candidates`, `/api/library/plan`, `/api/library/execute`, `/api/library/journal`
- Produces: no code interface

- [ ] **Step 1: Read the existing style**

```bash
head -80 app/templates/index.html
```

Match its CSS conventions, colour variables, and table markup. Do not introduce a framework.

- [ ] **Step 2: Build the page**

Replace `app/templates/library.html` with a page containing:

- A header with `age_days` / `idle_days` number inputs and a **Rescan** button calling `GET /api/library/candidates?age_days=&idle_days=`.
- A sticky summary bar showing selected title count and summed `real_bytes`, recomputed on every checkbox change.
- One `<section>` per bucket in order `A`, `B`, `C1`, `C2`, `C3`, each headed by `labels[bucket]` plus count and size. Sections `A`, `B`, `C1`, `C2` render `open`; `C3` renders closed.
- A table per section with columns: checkbox, Title, Kind, Progress (`watched/episodes` for series, `progress_pct%` for movies), Size, Added, Last played (`never` when null), Owner (`no *arr owner` when null), Flags.
- Checkboxes default-checked when `preticked.includes(bucket)`.
- A **Preflight** button posting selected `jf_ids` to `/api/library/plan`, rendering the returned `titles[].steps` and `real_bytes` in a modal.
- Inside the modal, a **Delete** button posting the same `jf_ids` to `/api/library/execute`. On HTTP 409 with `blast_radius: true`, show the error and require a second confirmation click before retrying.
- A results panel rendering each `results[]` entry with its `status` and `steps`, and a **Journal** toggle loading `/api/library/journal`.

Required behaviours:

- Never call `/api/library/execute` without a successful `/api/library/plan` first — the server rejects it, and the UI must not offer it.
- Escape all title text with `textContent`, never `innerHTML`. Titles contain user-controlled text.
- Show the server's `error` string verbatim on any non-2xx response.

- [ ] **Step 3: Add the nav link**

In `app/templates/index.html`, alongside the existing Settings link, add:

```html
<a href="/library">Library cleanup</a>
```

- [ ] **Step 4: Verify manually**

```bash
CONFIG_PATH=/tmp/cfg.json python -c "import sys; sys.path.insert(0,'app'); import app; app.app.run(port=5001)"
```

Open `http://127.0.0.1:5001/library`. Expect the page to render and show the "not configured" error from `/api/library/candidates` — that is correct without credentials.

- [ ] **Step 5: Commit**

```bash
git add app/templates/library.html app/templates/index.html
git commit -m "feat: add library cleanup UI"
```

---

## Task 14: Settings UI for services

**Files:**
- Modify: `app/templates/settings.html`

**Interfaces:**
- Consumes: `/api/settings`, `/api/test-connection/<service>`
- Produces: no code interface

- [ ] **Step 1: Add the service fields**

For each of Jellyfin, Sonarr, and Radarr, add a fieldset with a URL text input, an API key password input, and a **Test connection** button posting `{url, api_key}` to `/api/test-connection/<service>` and displaying `detail` or `error`.

Follow the existing pattern in `settings.html`: the key input is populated with the masked value from `GET /api/settings`, and submitting it unchanged preserves the stored secret.

- [ ] **Step 2: Honour environment overrides**

When `env_locked` from `/api/settings` contains `<service>_api_key`, render that key input `readonly` with the note "set by environment".

- [ ] **Step 3: Add the cleanup settings fieldset**

Number inputs for `age_days`, `idle_days`, `max_titles_per_run`, `max_bytes_per_run`; a checkbox for `seed_guard`; a textarea for `library_roots` (one path per line, matching the existing `exclude_paths` control).

- [ ] **Step 4: Verify round-trip**

Save settings, reload the page, and confirm each key still shows `••••••••` and non-secret values persist.

- [ ] **Step 5: Commit**

```bash
git add app/templates/settings.html
git commit -m "feat: add service credentials and cleanup settings to UI"
```

---

## Task 15: Deployment and documentation

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `.github/workflows/docker.yml`

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1: Pass optional key overrides through compose**

Replace `docker-compose.yml`:

```yaml
version: "3.9"

services:
  transmission-checker:
    build: .
    network_mode: host
    volumes:
      - /share:/share:rw
      - ./config:/config
    environment:
      # Optional. When set, these override the values stored in config.json
      # and render the corresponding settings field read-only.
      - JELLYFIN_API_KEY=${JELLYFIN_API_KEY:-}
      - SONARR_API_KEY=${SONARR_API_KEY:-}
      - RADARR_API_KEY=${RADARR_API_KEY:-}
    restart: unless-stopped
```

- [ ] **Step 2: Run tests in CI**

In `.github/workflows/docker.yml`, add a job that runs before the image build:

```yaml
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r app/requirements-dev.txt
      - run: pytest -v
```

Add `needs: test` to the existing build job.

- [ ] **Step 3: Document the feature**

Add a `## Library cleanup` section to `README.md` covering: what it does, the two thresholds and their defaults, the five buckets and why C3 is never pre-selected, the three-step deletion order, the seeding guard, the blast-radius caps, that **deletions cannot be undone**, and how to supply the three API keys (settings UI, or environment override).

- [ ] **Step 4: Full verification**

```bash
pytest -v
docker compose build
```

Expected: all tests pass; image builds.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml README.md .github/workflows/docker.yml
git commit -m "chore: wire up CI tests, compose env, and docs"
```

---

## Task 16: Live verification on the deployment host

The stack under test runs on `192.168.1.132` (`debian-cosmos`, `ssh froike@`),
where the container `autoremove-transmission` is deployed. The repository owner
has authorised updating that container to verify the finished feature.

Do this only after Tasks 1-15 are complete and `pytest` is green. This is
verification against live data — **the deletions it performs are real.**

**Files:** none — deployment only.

- [ ] **Step 1: Capture the current container configuration before changing it**

```bash
ssh froike@192.168.1.132 'docker inspect autoremove-transmission \
  --format "IMAGE={{.Config.Image}} NET={{.HostConfig.NetworkMode}} RESTART={{.HostConfig.RestartPolicy.Name}}"; \
  docker inspect autoremove-transmission --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}"; \
  docker port autoremove-transmission'
```

Record the output. The container must be recreated with exactly these mounts,
network mode, and port bindings — do not assume the values in
`docker-compose.yml` match what is actually deployed.

- [ ] **Step 2: Build the image from the feature branch**

Push the branch, let CI build it, then pull the tagged image:

```bash
ssh froike@192.168.1.132 'docker pull ghcr.io/roikeman/autoremove-transmission:<sha>'
```

Use the commit SHA tag, never `latest` — `latest` tracks `master` and would not
contain the feature under test.

- [ ] **Step 3: Recreate the container**

Stop and remove the old container, then recreate it with the configuration
captured in Step 1 and the image from Step 2. Keep the `/config` volume so the
existing settings and journal survive.

- [ ] **Step 4: Verify without deleting anything**

```bash
ssh froike@192.168.1.132 'curl -s localhost:5000/api/health'
```

Then in the browser: open the library page, confirm candidates load, confirm
bucket counts and sizes are plausible against the figures in the spec
(~1.23 TB across ~141 titles at the 180/90 defaults), and run **Preflight** on
a small selection. **Stop there.** Preflight deletes nothing; confirm its
reported steps and byte counts look right before anything is executed.

- [ ] **Step 5: First real deletion — one low-risk title**

Choose a single bucket A (never opened) title, execute, then verify:

- the file is gone from `/share`
- the entry is gone from Sonarr or Radarr
- the entry is gone from Jellyfin
- `/api/library/journal` records the run with all steps `ok`
- the freed space appears in `df -h /media/magnetic-12tb` on `192.168.1.170`

Only after this end-to-end confirmation should a larger selection be run.

- [ ] **Step 6: Roll back if verification fails**

```bash
ssh froike@192.168.1.132 'docker stop autoremove-transmission && docker rm autoremove-transmission'
```

Recreate from the previously deployed image recorded in Step 1. Note that
rolling back the container does **not** restore deleted media — there is no
undo. That is why Step 5 uses exactly one title.

---

## Self-Review Notes

**Spec coverage:** every spec section maps to a task — selection criteria and buckets → Task 5; data-quality flags → Tasks 5 and 8; architecture and file structure → Tasks 2, 5–11; data model and `watched` derivation → Task 8; ownership matching → Task 8; API surface → Task 12; deletion pipeline and ordering → Task 11; seeding guard → Task 11; preflight → Tasks 11 and 12; blast radius → Task 11; failure handling and journal → Tasks 10 and 11; UI → Tasks 13 and 14; credentials → Tasks 4, 12, 14, 15; testing → every task; first implementation task → Task 3.

**Known deviation:** `/api/library/retry` appears in the spec's API table but has no task. Retrying a partial title means re-running steps whose failure modes are already idempotent, so it is deferred rather than built blind — the journal records exactly what is needed to add it later. Raise this with the owner if per-step retry is considered required for the first release.

**Type consistency:** `Candidate` field names are identical in Tasks 8, 11, and 12. Bucket strings are `"A"`, `"B"`, `"C1"`, `"C2"`, `"C3"` throughout. `Clients` field names (`sonarr`, `radarr`, `jellyfin`, `transmission`) match between Tasks 11 and 12. `pipeline.plan` and `pipeline.execute` take `(candidates, cfg)` and `(candidates, cfg, clients)` consistently.

**Live verification:** Task 16 covers deployment to `192.168.1.132` for
end-to-end verification, authorised by the repository owner. It is deliberately
last and deliberately narrow — one bucket A title — because the pipeline it
exercises has no undo.
