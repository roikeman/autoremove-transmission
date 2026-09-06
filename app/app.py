import contextlib
import errno
import fcntl
import json
import os
import requests
from flask import Flask, jsonify, render_template, request as flask_request
import config as cfg_mod
import version
from clients.transmission import (
    rpc_call,
    get_all_torrents,
    is_deletable,
    reset_session,
    _cfg,
    _rpc_url,
    _auth,
)
from clients.jellyfin import JellyfinClient
from clients.arr import ArrClient
from clients import transmission as tx
from cleanup import buckets, candidates as cand, journal, pipeline

app = Flask(__name__)
# Masked secrets use literal bullet characters (config.MASK); without this,
# Flask's default jsonify escapes them to • sequences in the response
# body. Functionally equivalent once parsed, but callers and tests that
# check the raw text should see the same characters config.py produces.
app.json.ensure_ascii = False


def _state_dir():
    """Directory next to the config file, for the lock and plan files.

    Derived from config.CONFIG_PATH (not hardcoded) so tests can redirect
    both the config and this state into tmp_path.
    """
    directory = os.path.dirname(cfg_mod.CONFIG_PATH)
    return directory if directory else "."


def _lock_path():
    return os.path.join(_state_dir(), "cleanup.lock")


def _plan_path():
    return os.path.join(_state_dir(), "last_plan.json")


@contextlib.contextmanager
def _run_lock():
    """Cross-process mutual exclusion for a cleanup run.

    gunicorn runs this app as multiple worker PROCESSES (see Dockerfile),
    so a threading.Lock -- which is per-process -- cannot prevent two
    workers from each starting a run at the same time. An flock()'d file
    is visible to every process on the host, so it actually serializes
    concurrent /api/library/execute calls regardless of which worker
    handles them.
    """
    directory = _state_dir()
    if directory:
        os.makedirs(directory, exist_ok=True)
    path = _lock_path()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                yield False
                return
            os.close(fd)
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _save_last_plan(jf_ids):
    """Persist the plan's selected id set to a file next to the config.

    Module-level Python state doesn't survive across gunicorn worker
    processes, so a plan served by one worker would be invisible to an
    execute routed to another. Writing the selection to disk makes the
    preflight guard work regardless of which worker handles each request.
    No secrets are ever stored here -- only the opaque jf_ids.
    """
    directory = _state_dir()
    if directory:
        os.makedirs(directory, exist_ok=True)
    path = _plan_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"ids": sorted(jf_ids)}, f)
    os.replace(tmp_path, path)


def _load_last_plan():
    """Read the persisted plan's id set. Fails closed: a missing or corrupt
    file is treated as "no plan recorded" (empty set), so execute's
    equality check against it will not spuriously match."""
    path = _plan_path()
    try:
        with open(path) as f:
            data = json.load(f)
        ids = data.get("ids")
        if not isinstance(ids, list):
            return set()
        return set(ids)
    except (OSError, ValueError):
        return set()


def _clear_last_plan():
    try:
        os.remove(_plan_path())
    except OSError:
        pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    safe = cfg_mod.mask(cfg_mod.load())
    safe["env_locked"] = sorted(cfg_mod.env_locked())
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = flask_request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        saved = cfg_mod.save(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Reset RPC session so next call re-authenticates with new settings
    reset_session()

    return jsonify({"status": "ok", "settings": cfg_mod.mask(saved)})


@app.route("/api/health")
def health():
    # Build identity is reported even when Transmission is unreachable — it is
    # how you confirm which image a container is actually running.
    build = version.info()
    try:
        cfg = _cfg()
        rpc_call("session-get", {"fields": ["version"]})
        return jsonify({
            "status": "ok",
            "build": build,
            "transmission": f"{cfg['transmission_host']}:{cfg['transmission_port']}",
        })
    except Exception as e:
        return jsonify({"status": "error", "build": build, "error": str(e)}), 503


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    """Test connection using form values without touching saved config."""
    data = flask_request.get_json(force=True) or {}
    host     = data.get("transmission_host", "").strip()
    port     = data.get("transmission_port", "").strip()
    rpc_path = data.get("transmission_rpc_path", "/rpc").strip()
    user     = data.get("transmission_user", "").strip()
    password = data.get("transmission_pass", "")

    if not host or not port:
        return jsonify({"status": "error", "error": "Host and port are required"}), 400

    url  = f"http://{host}:{port}{rpc_path}"
    auth = (user, password) if user else None
    try:
        # Step 1: get CSRF token
        r = requests.post(url, json={}, auth=auth, timeout=8, allow_redirects=False)
        sid = r.headers.get("X-Transmission-Session-Id", "")
        # Step 2: actual call
        r2 = requests.post(
            url,
            json={"method": "session-get", "arguments": {"fields": ["version"]}},
            headers={"X-Transmission-Session-Id": sid},
            auth=auth, timeout=8, allow_redirects=False
        )
        if r2.status_code == 200:
            return jsonify({"status": "ok", "transmission": f"{host}:{port}"})
        return jsonify({"status": "error", "error": f"Unexpected status {r2.status_code}"}), 503
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 503


@app.route("/api/torrent/<int:torrent_id>/delete", methods=["POST"])
def delete_torrent(torrent_id):
    try:
        rpc_call("torrent-remove", {
            "ids": [torrent_id],
            "delete-local-data": True
        })
        return jsonify({"status": "ok", "id": torrent_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _get_known_download_dirs():
    try:
        torrents = get_all_torrents()
        return {t.get("downloadDir", "") for t in torrents if t.get("downloadDir")}
    except Exception:
        return set()


def get_orphan_files():
    torrents = get_all_torrents()

    torrent_files = set()
    download_dirs = set()
    for t in torrents:
        dl_dir = t.get("downloadDir", "")
        if dl_dir:
            download_dirs.add(dl_dir)
        for f in t.get("files", []):
            torrent_files.add(os.path.normpath(os.path.join(dl_dir, f["name"])))

    exclude_paths = [os.path.normpath(p) for p in _cfg().get("exclude_paths", [])]

    def _is_excluded(path):
        np = os.path.normpath(path)
        return any(np == ep or np.startswith(ep + os.sep) for ep in exclude_paths)

    _HIDDEN = {".recycle", "@eaDir", "#recycle", "@Recycle"}
    orphans = []
    for scan_dir in download_dirs:
        if not os.path.isdir(scan_dir) or _is_excluded(scan_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(scan_dir, followlinks=False):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _HIDDEN
                           and not _is_excluded(os.path.join(dirpath, d))]
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                if os.path.islink(full_path) or _is_excluded(full_path):
                    continue
                if os.path.normpath(full_path) not in torrent_files:
                    try:
                        size = os.path.getsize(full_path)
                    except OSError:
                        size = 0
                    orphans.append({
                        "name":      filename,
                        "path":      full_path,
                        "parentDir": dirpath,
                        "size":      size,
                    })

    orphans.sort(key=lambda f: f["size"], reverse=True)
    return orphans, sum(f["size"] for f in orphans)


@app.route("/api/orphans")
def api_orphans():
    try:
        orphans, total_bytes = get_orphan_files()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"orphans": orphans, "totalBytes": total_bytes, "count": len(orphans)})


@app.route("/api/orphan/delete", methods=["POST"])
def delete_orphan():
    data = flask_request.get_json(force=True) or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path provided"}), 400

    norm = os.path.normpath(path)
    if not os.path.isabs(norm):
        return jsonify({"error": "Path must be absolute"}), 400

    known_dirs = _get_known_download_dirs()
    if not any(norm.startswith(os.path.normpath(d) + os.sep) for d in known_dirs):
        return jsonify({"error": "Path is outside known download directories"}), 403

    if not os.path.isfile(norm) or os.path.islink(norm):
        return jsonify({"error": "Target is not a regular file"}), 400

    try:
        os.remove(norm)
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    parent = os.path.dirname(norm)
    try:
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass

    return jsonify({"status": "ok", "path": norm})


@app.route("/api/deletable")
def api_deletable():
    try:
        torrents = get_all_torrents()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    deletable = []
    for t in torrents:
        if is_deletable(t):
            deletable.append({
                "id":          t["id"],
                "name":        t["name"],
                "totalSize":   t["totalSize"],
                "downloadDir": t["downloadDir"],
                "addedDate":   t.get("addedDate", 0),
                "trackers":    [tr["announce"] for tr in t.get("trackers", [])],
            })

    deletable.sort(key=lambda t: t["totalSize"], reverse=True)
    total_bytes = sum(t["totalSize"] for t in deletable)

    return jsonify({
        "torrents":   deletable,
        "totalBytes": total_bytes,
        "count":      len(deletable),
    })


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

    _save_last_plan({c.jf_id for c in selection})
    return jsonify(result)


@app.route("/api/library/execute", methods=["POST"])
def api_execute():
    cfg = cfg_mod.load()
    body = flask_request.get_json(force=True) or {}
    jf_ids = body.get("jf_ids") or []
    if not jf_ids:
        return jsonify({"error": "no titles selected"}), 400

    # Refuse a selection that doesn't match the last /plan response: the
    # library may have changed since a stale browser tab last preflighted.
    # The comparison set is persisted to disk (see _load_last_plan) so this
    # still works when the /plan and /execute requests land on different
    # gunicorn worker processes.
    if set(jf_ids) != _load_last_plan():
        return jsonify({"error": "selection changed since preflight; re-run the plan"}), 409

    with _run_lock() as acquired:
        if not acquired:
            return jsonify({"error": "a cleanup run is already in progress"}), 409

        try:
            selection = _selected(cfg, jf_ids)
            # Note: execute() re-checks the blast-radius caps itself, so a second
            # pipeline.plan() call here would be redundant -- omitted.
            results = pipeline.execute(selection, cfg, _build_clients(cfg))
        except pipeline.BlastRadiusExceeded as e:
            return jsonify({"error": str(e), "blast_radius": True}), 409
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    _clear_last_plan()
    return jsonify({"results": results})


@app.route("/api/library/journal")
def api_journal():
    return jsonify(journal.read_recent(limit=200))


@app.route("/api/test-connection/<service>", methods=["POST"])
def test_service_connection(service):
    data = flask_request.get_json(force=True) or {}
    cfg = cfg_mod.load()

    if service not in ("jellyfin", "sonarr", "radarr"):
        return jsonify({"error": "unknown service"}), 400

    url = (data.get("url") or "").strip()
    key = (data.get("api_key") or "").strip()

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
