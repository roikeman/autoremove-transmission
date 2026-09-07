import contextlib
import errno
import fcntl
import json
import os
import threading
from datetime import datetime
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


def _candidates_cache_path():
    return os.path.join(_state_dir(), "candidates_cache.json")


def _scan_lock_path():
    return os.path.join(_state_dir(), "scan.lock")


def _scan_status_path():
    return os.path.join(_state_dir(), "scan_status.json")


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


def _acquire_scan_lock():
    """Try to acquire the dedicated scan lock without blocking.

    Separate from _run_lock()/cleanup.lock: a running scan and a running
    delete must each be exclusive with themselves (only one scan, only one
    delete, at a time), but not with each other -- so this gets its own
    flock()'d file rather than reusing cleanup.lock.

    Returns an open, already-locked fd on success -- the caller owns it and
    must fcntl.flock(fd, LOCK_UN) + os.close(fd) when the scan finishes.
    Returns None if a scan is already running (in this or another gunicorn
    worker process); the fd is closed before returning in that case.
    """
    directory = _state_dir()
    if directory:
        os.makedirs(directory, exist_ok=True)
    path = _scan_lock_path()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fd


def _scan_lock_is_free():
    """True if nothing currently holds the scan lock.

    Probes by acquiring then immediately releasing -- flock() is owned by
    the OS per open file description, not by any Python-level bookkeeping,
    so if the worker process that was running a scan died (OOM kill, crash,
    restart) mid-scan, the lock is already gone and this returns True. That
    is exactly how _reconcile_scan_status() tells a dead scan apart from a
    healthy one still actually running.
    """
    fd = _acquire_scan_lock()
    if fd is None:
        return False
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return True


_SCAN_STATUS_IDLE = {
    "state": "idle", "started_at": None, "finished_at": None,
    "progress": None, "error": None, "heartbeat": None,
}


def _save_scan_status(status):
    directory = _state_dir()
    if directory:
        os.makedirs(directory, exist_ok=True)
    path = _scan_status_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(status, f)
    os.replace(tmp_path, path)


def _load_scan_status():
    """Fails open to "idle" (never raises) on a missing or corrupt status
    file -- same tolerant handling as the plan/cache loaders below."""
    path = _scan_status_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(_SCAN_STATUS_IDLE)
    if not isinstance(data, dict) or "state" not in data:
        return dict(_SCAN_STATUS_IDLE)
    return data


def _reconcile_scan_status():
    """Read the scan status file, correcting a lie it might be telling.

    A status file that says "running" is only trustworthy while something
    actually holds the scan lock -- see _acquire_scan_lock(). A worker that
    dies mid-scan (OOM, crash, restart) releases the lock via the OS but
    never gets to write a final status, so without this check the UI would
    poll "running" forever. If the lock turns out to be free, this rewrites
    the status to "error" with a clear message instead.
    """
    status = _load_scan_status()
    if status.get("state") == "running" and _scan_lock_is_free():
        status = {
            **status,
            "state": "error",
            "error": "scan process stopped unexpectedly (lock was released without finishing)",
            "finished_at": datetime.now().isoformat(),
        }
        _save_scan_status(status)
    return status


def _run_scan_job(cfg, lock_fd):
    """Runs the real (slow) scan on a background thread, started by
    POST /api/library/scan so the request that triggered it can return
    immediately. Owns lock_fd (already flock'd by the caller) for the
    scan's full duration, and always releases + closes it before returning
    -- success or failure -- which is what makes lock-held the source of
    truth for "a scan is actually running" (see _scan_lock_is_free).
    """
    def on_progress(done, total, phase):
        current = _load_scan_status()
        current["progress"] = {"done": done, "total": total, "phase": phase}
        current["heartbeat"] = datetime.now().isoformat()
        _save_scan_status(current)

    status = _load_scan_status()
    try:
        found = _scan(cfg, progress_cb=on_progress)
        scanned_at = datetime.now().isoformat()
        _save_candidates_cache(found, int(cfg["age_days"]), int(cfg["idle_days"]), scanned_at)
        status["state"] = "done"
        status["finished_at"] = scanned_at
        status["heartbeat"] = scanned_at
        status["error"] = None
        _save_scan_status(status)
    except Exception as exc:
        now = datetime.now().isoformat()
        status["state"] = "error"
        status["finished_at"] = now
        status["heartbeat"] = now
        status["error"] = str(exc)
        _save_scan_status(status)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


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


def _save_candidates_cache(candidates, age_days, idle_days, scanned_at):
    """Persist the last scan's candidate list to a file next to the config,
    mirroring _save_last_plan. A 91-user Jellyfin makes a scan take minutes
    (one HTTP round-trip per user per item type), so this is what lets
    GET /api/library/candidates -- and the plan/execute calls that reuse it,
    see _selected() -- avoid repeating that cost on every request.

    No secrets are ever stored here: Candidate.to_dict() only carries
    library metadata (titles, paths, sizes, timestamps), never credentials.
    """
    directory = _state_dir()
    if directory:
        os.makedirs(directory, exist_ok=True)
    path = _candidates_cache_path()
    tmp_path = path + ".tmp"
    data = {
        "candidates": [c.to_dict() for c in candidates],
        "age_days": age_days,
        "idle_days": idle_days,
        "scanned_at": scanned_at,
    }
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def _load_candidates_cache():
    """Read the persisted scan cache. Fails open to "no cache" (None) on a
    missing or corrupt file -- same tolerant handling as _load_last_plan --
    so a broken cache file just costs one extra scan instead of a 500."""
    path = _candidates_cache_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in ("candidates", "age_days", "idle_days", "scanned_at")):
        return None
    if not isinstance(data["candidates"], list):
        return None
    return data


def _candidate_from_cached_dict(d):
    """Reconstruct a Candidate from one Candidate.to_dict() entry as stored
    in the cache file. Validates each field defensively: an older cache
    written before a field existed (missing key), or any field with an
    unexpected type, raises KeyError/TypeError so the caller can treat the
    whole cache as unusable and fall back to a fresh scan -- instead of
    surfacing a 502, or letting a wrong-typed field (e.g. size_bytes as a
    string) blow up later when candidates are sorted or summed.

    added/last_played round-tripped through to_dict() into ISO strings (or
    null), so cand.parse_dt -- the same parser used for Jellyfin's own
    timestamps -- turns them back into datetimes.
    """
    def _str(key):
        v = d[key]
        if not isinstance(v, str):
            raise TypeError(f"{key} must be a string")
        return v

    def _opt_str(key):
        v = d[key]
        if v is not None and not isinstance(v, str):
            raise TypeError(f"{key} must be a string or null")
        return v

    def _int(key):
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"{key} must be an int")
        return v

    def _opt_int(key):
        v = d[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise TypeError(f"{key} must be an int or null")
        return v

    def _num(key):
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{key} must be a number")
        return v

    def _int_default(key, default=0):
        # Unlike _int, a MISSING key is not a corruption -- it just means
        # this entry was cached before the users_finished/started/dropped
        # viewer-breakdown fields existed, so it defaults rather than
        # raising. A key that IS present with the wrong type still raises,
        # same as every other field here, since that indicates real
        # corruption rather than an old cache shape.
        if key not in d:
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"{key} must be an int")
        return v

    flags = d.get("flags") or []
    if not isinstance(flags, list):
        raise TypeError("flags must be a list")

    return cand.Candidate(
        jf_id=_str("jf_id"),
        kind=_str("kind"),
        title=_str("title"),
        path=_str("path"),
        size_bytes=_int("size_bytes"),
        added=cand.parse_dt(d["added"]),
        last_played=cand.parse_dt(d["last_played"]),
        episodes=_int("episodes"),
        watched=_int("watched"),
        progress_pct=_num("progress_pct"),
        owner=_opt_str("owner"),
        owner_id=_opt_int("owner_id"),
        bucket=_str("bucket"),
        flags=list(flags),
        users_finished=_int_default("users_finished"),
        users_started=_int_default("users_started"),
        users_dropped=_int_default("users_dropped"),
    )


def _reconstruct_cached_candidates(cached):
    """Rebuild every Candidate from a loaded cache dict's "candidates" list.
    Returns None (never raises) if any entry is malformed, so callers can
    treat that the same as "no cache" rather than a 502."""
    try:
        return [_candidate_from_cached_dict(d) for d in cached["candidates"]]
    except (KeyError, TypeError, ValueError):
        return None


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


def _user_series_last_played(jf, user_id):
    """One extra call per user: every episode this user has played, folded
    down to that user's most-recent play per series (SeriesId -> datetime).

    Jellyfin populates UserData.LastPlayedDate on the Episode item, never on
    the Series item, so this is how a series' last-played date gets derived
    at all -- without reintroducing the per-series-per-user fan-out (91
    users x ~75 series) that was removed from _scan for performance. This is
    +1 call per user, same order as the two calls per user already made.

    Returns (series_id -> datetime, reliable). `reliable` is False when the
    call raised, or when the response is non-empty but not one single item
    carries a "SeriesId" key at all -- i.e. this Jellyfin version doesn't
    return it for this query shape, so there is no way to attribute any of
    these plays to a series. Callers must then treat "no play found this
    scan" for every series as UNKNOWN, not "never played" -- that
    misattribution is exactly the bug this function exists to fix.

    Deliberately does not trust SortBy=DatePlayed / Filters=IsPlayed being
    honoured: every returned episode is inspected and the max date per
    series is kept (not "first occurrence in a descending list"), and an
    unplayed episode slipping through the filter simply has no parseable
    LastPlayedDate and is skipped -- so a sort/filter quirk in some Jellyfin
    version degrades silently to "did nothing" rather than a wrong date.
    """
    try:
        episodes = jf.played_episodes(user_id)
    except Exception:
        return {}, False

    if not episodes:
        return {}, True

    if not any("SeriesId" in ep for ep in episodes):
        return {}, False

    per_series = {}
    for ep in episodes:
        sid = ep.get("SeriesId")
        if not sid:
            continue
        played = cand.parse_dt((ep.get("UserData") or {}).get("LastPlayedDate"))
        if played is None:
            continue
        if sid not in per_series or played > per_series[sid]:
            per_series[sid] = played
    return per_series, True


def _scan(cfg, progress_cb=None):
    """Build the full candidate list. Merges user data across all Jellyfin users.

    `progress_cb`, when given, is called as progress_cb(done, total, phase)
    at cheap, honest checkpoints -- a single counter over the WHOLE scan's
    work, monotonically non-decreasing from 0 up to `total`, which never
    changes once the scan starts. `total` accounts for every phase: the
    Sonarr/Radarr owner-index build, BOTH per-user Jellyfin passes (see
    below), and the final hardlinks pass -- so a caller polling this never
    sees the counter drop, unlike the two-per-user-pass bug this replaced
    (each pass used to report its own 0..total_users, so the second pass
    restarted low and looked like the scan had gone backwards). `phase` is
    a human-readable label naming what's happening right now: "sonarr",
    "jellyfin-played" (the played-episodes pass), "jellyfin-items" (the
    items-fetch/merge pass), or "hardlinks". This is deliberately not a
    percentage of *candidates found*: there is no cheap way to know in
    advance how many will survive the stale filter -- `done`/`total` is
    only ever a fraction of scan *work*, never of results.

    Series episode counts and file sizes come from Sonarr's `statistics`
    object (episodeCount, sizeOnDisk) on the /api/v3/series response --
    already fetched once here to build the owner index -- rather than from
    a per-user, per-series Jellyfin episode fetch. On a 91-user server with
    ~75 stale series, that per-episode fetch was ~6,800 extra HTTP round
    trips (91 users x 75 series); this reads real numbers off data already
    in hand, at zero extra API calls. `JellyfinClient.episodes()` stays
    available on the client for callers that still want it -- it is simply
    no longer called from here.

    A series' last-played date is a separate problem from its episode count:
    Jellyfin never populates UserData.LastPlayedDate on the Series item
    itself (only on the Episode), so it is derived from
    _user_series_last_played -- one extra call per user, not per series.
    """
    from datetime import datetime

    jf = JellyfinClient(cfg["jellyfin_url"], cfg["jellyfin_api_key"])

    # Users are fetched first (a cheap call) purely so total_units -- and
    # therefore every progress report for the rest of this scan -- can be
    # computed once and never change: 1 unit for the sonarr/radarr phase,
    # one unit per user for EACH of the two per-user passes below, and 1
    # unit for the final hardlinks pass.
    users = jf.users()
    total_users = len(users)
    total_units = 2 * total_users + 2

    done = 0

    def _report(phase):
        if progress_cb:
            progress_cb(done, total_units, phase)

    _report("sonarr")
    sonarr_items = []
    radarr_items = []
    if cfg.get("sonarr_url"):
        sonarr_items = ArrClient(cfg["sonarr_url"], cfg["sonarr_api_key"], "sonarr").list_items()
    if cfg.get("radarr_url"):
        radarr_items = ArrClient(cfg["radarr_url"], cfg["radarr_api_key"], "radarr").list_items()
    owner_index = cand.build_owner_index(sonarr_items, radarr_items)
    sonarr_by_id = cand.sonarr_stats_by_id(sonarr_items)
    radarr_by_id = cand.radarr_stats_by_id(radarr_items)
    done += 1
    _report("sonarr")

    # First pass: one played-episodes call per user. Collected up front (not
    # interleaved with the merge loop below) so the scan-wide reliability
    # verdict is fully known -- any_unreliable -- before it is applied to a
    # single candidate; a user whose call fails partway through the merge
    # loop must not leave earlier candidates under-flagged.
    series_last_played_by_user = {}
    any_unreliable = False
    for user in users:
        uid = user["Id"]
        per_series, reliable = _user_series_last_played(jf, uid)
        series_last_played_by_user[uid] = per_series
        if not reliable:
            any_unreliable = True
        done += 1
        _report("jellyfin-played")

    merged = {}
    for user in users:
        uid = user["Id"]
        series_last_played = series_last_played_by_user[uid]
        for item in jf.items(uid, "Series"):
            path = item.get("Path") or ""
            owner, owner_id = cand.match_owner(path, owner_index)
            episodes, size_bytes = cand.sonarr_stats(owner, owner_id, sonarr_by_id)
            episode_last_played = series_last_played.get(item.get("Id"))
            _merge(
                merged,
                cand.from_series(item, owner_index, episodes=episodes,
                                  size_bytes=size_bytes, last_played=episode_last_played),
                last_played_unreliable=any_unreliable,
            )
        for item in jf.items(uid, "Movie"):
            path = item.get("Path") or ""
            owner, owner_id = cand.match_owner(path, owner_index)
            size_bytes = cand.radarr_size(owner, owner_id, radarr_by_id)
            _merge(merged, cand.from_movie(item, owner_index, size_bytes=size_bytes))
        done += 1
        _report("jellyfin-items")

    done += 1
    _report("hardlinks")
    now = datetime.now()
    age = int(cfg["age_days"])
    idle = int(cfg["idle_days"])

    result = []
    for c in merged.values():
        if not cand.is_stale(c.added, c.last_played, now, age, idle):
            continue
        # Hardlink check only runs for titles that actually survive the
        # stale filter, and only stats this one candidate's own path (never
        # the whole library) -- see cand.path_has_hardlink.
        has_hardlink = cand.path_has_hardlink(c.path)
        # quality_flags recomputes the two flags it owns (added-date and
        # hardlink); any other flag already on the candidate (e.g.
        # "episode-data-unavailable") is a distinct data-quality fact that
        # must survive this recompute, not be wiped by it.
        auto_flags = {"added-date-unreliable", "frees-less-than-listed"}
        extra_flags = [f for f in c.flags if f not in auto_flags]
        c.flags = buckets.quality_flags(c.added, c.last_played, has_hardlink) + extra_flags
        result.append(c)

    return result


def _merge(store, candidate, last_played_unreliable=False):
    """Keep the most-watched, most-recently-played view across users, and
    accumulate the per-user viewer breakdown (users_finished/started/dropped)
    behind the compact "12 finished / 3 started / 76 never opened" UI column
    (see candidates.classify_viewer for the exact definitions).

    Data-quality flags set by candidates.from_series (e.g.
    "episode-data-unavailable", "watch-count-unavailable") describe a fact
    about the title itself, not about any one user's view of it, so they
    must survive every merge -- with up to 91 users, losing them on the
    second merge would silently defeat the "never pre-tick on unknown data"
    guarantee the flag exists to uphold.

    `last_played_unreliable` is the scan-wide verdict from
    _user_series_last_played: at least one user's played-episode lookup
    failed or came back unusable this scan. It only matters for a series
    that STILL has no last_played after folding in every user seen so far --
    a series with a real, known last_played is safe regardless of what a
    failed lookup elsewhere might be hiding (missing data can only mean an
    even more recent, i.e. even less stale, play was missed -- never a
    reason to treat a known date as wrong). This flag is therefore
    recomputed on every merge (including the first), not carried forward
    blindly like the manually-set unavailable flags: unlike those, whether
    it applies depends on existing.last_played, which can change on any
    given merge as later users are folded in.
    """
    verdict = cand.classify_viewer(
        candidate.kind, candidate.episodes, candidate.watched, candidate.last_played)

    existing = store.get(candidate.jf_id)
    if existing is None:
        store[candidate.jf_id] = candidate
        existing = candidate
    else:
        if candidate.watched > existing.watched:
            existing.watched = candidate.watched
        if candidate.last_played and (
                existing.last_played is None or candidate.last_played > existing.last_played):
            existing.last_played = candidate.last_played
        existing.progress_pct = max(existing.progress_pct, candidate.progress_pct)
        existing.bucket = buckets.classify(
            existing.kind, existing.episodes, existing.watched,
            existing.last_played, existing.progress_pct)

    auto_flags = {"added-date-unreliable", "frees-less-than-listed", "last-played-unavailable"}
    extra_flags = [f for f in existing.flags if f not in auto_flags]
    existing.flags = buckets.quality_flags(existing.added, existing.last_played, False) + extra_flags
    if existing.kind == "series" and existing.last_played is None and last_played_unreliable:
        existing.flags.append("last-played-unavailable")
    existing.bucket = cand.guard_unavailable_bucket(existing.bucket, existing.flags)

    if verdict == "finished":
        existing.users_finished += 1
    elif verdict == "started":
        existing.users_started += 1
    else:
        existing.users_dropped += 1


@app.route("/library")
def library_page():
    return render_template("library.html")


@app.route("/api/library/scan", methods=["POST"])
def api_start_scan():
    """Kick off a scan on a background thread and return immediately.

    Never blocks the caller for the ~200s a real scan takes: acquires the
    dedicated scan lock (non-blocking) and, on success, hands the actual
    work to _run_scan_job() on a daemon thread before responding. If a scan
    is already running -- in this worker or another gunicorn worker
    process, see _acquire_scan_lock() -- responds 409 with the current
    (reconciled) status instead of starting a second one.
    """
    cfg = cfg_mod.load()
    try:
        _require(cfg, "jellyfin_url", "jellyfin_api_key")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    body = flask_request.get_json(silent=True) or {}
    for key in ("age_days", "idle_days"):
        override = body.get(key)
        if override not in (None, ""):
            try:
                cfg[key] = int(override)
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be an integer"}), 400

    lock_fd = _acquire_scan_lock()
    if lock_fd is None:
        return jsonify(_reconcile_scan_status()), 409

    now = datetime.now().isoformat()
    status = {
        "state": "running", "started_at": now, "finished_at": None,
        "progress": {"done": 0, "total": 0, "phase": "sonarr"},
        "error": None, "heartbeat": now,
    }
    _save_scan_status(status)

    thread = threading.Thread(target=_run_scan_job, args=(cfg, lock_fd), daemon=True)
    thread.start()

    return jsonify(status), 202


@app.route("/api/library/scan/status")
def api_scan_status():
    """Never blocks: reads the status file (reconciling a dead worker's
    stale "running" state, see _reconcile_scan_status) and returns it."""
    return jsonify(_reconcile_scan_status())


@app.route("/api/library/candidates")
def api_candidates():
    """Always serves the cache -- never scans, never blocks. A real scan
    takes ~200s on a 91-user Jellyfin; running it inline here is exactly
    the "looked like the app had died" bug this endpoint must not repeat.
    Use POST /api/library/scan to populate/refresh the cache instead.
    """
    cfg = cfg_mod.load()
    try:
        _require(cfg, "jellyfin_url", "jellyfin_api_key")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    empty_response = {
        "candidates": [],
        "preticked": sorted(buckets.PRETICKED),
        "labels": buckets.LABELS,
        "age_days": cfg["age_days"],
        "idle_days": cfg["idle_days"],
        "cached": False,
        "scanned_at": None,
        "scan_required": True,
    }

    cached = _load_candidates_cache()
    if cached is None:
        return jsonify(empty_response)

    found = _reconstruct_cached_candidates(cached)
    if found is None:
        # Corrupt cache -- treated the same as "no cache" rather than a 502,
        # same fail-open handling _load_candidates_cache already uses.
        return jsonify(empty_response)

    found.sort(key=lambda c: c.size_bytes, reverse=True)
    return jsonify({
        "candidates": [c.to_dict() for c in found],
        "preticked": sorted(buckets.PRETICKED),
        "labels": buckets.LABELS,
        "age_days": cached["age_days"],
        "idle_days": cached["idle_days"],
        "cached": True,
        "scanned_at": cached["scanned_at"],
        "scan_required": False,
    })


def _selected(cfg, jf_ids):
    # Design decision: plan/execute deliberately act on the SAME cached
    # candidate set the user reviewed via GET /api/library/candidates,
    # rather than triggering their own fresh (multi-minute) Jellyfin scan.
    # That means the user acts on exactly the data they saw on screen,
    # instead of a set that could have silently changed between viewing
    # and confirming. This is safe because the pipeline is already
    # idempotent: a title deleted in the meantime returns 404 from
    # Sonarr/Radarr (treated as success) and a missing path frees 0 bytes.
    #
    # Unlike the old scan-with-cache fallback, this NEVER falls back to a
    # fresh _scan(): if there is no usable cache at all, it returns None so
    # the caller can fail clearly (409) rather than kick off a 200-second
    # scan inside a POST request.
    wanted = set(jf_ids)

    cached = _load_candidates_cache()
    found = _reconstruct_cached_candidates(cached) if cached is not None else None
    if found is None:
        return None

    return [c for c in found if c.jf_id in wanted]


def _update_candidates_cache_after_execute(results):
    """Remove successfully-deleted titles from the cache and write it back,
    instead of clearing the whole cache. A wholesale clear meant the very
    next page load paid a full ~200s synchronous-feeling rescan for what
    should be an instant "37 fewer rows" update; titles that came back
    "partial" or "failed" may still exist, so they are deliberately left in
    place rather than removed.
    """
    cached = _load_candidates_cache()
    if cached is None:
        return
    found = _reconstruct_cached_candidates(cached)
    if found is None:
        return

    deleted_ids = {r["jf_id"] for r in results if r.get("status") == "deleted"}
    if not deleted_ids:
        return

    remaining = [c for c in found if c.jf_id not in deleted_ids]
    _save_candidates_cache(remaining, cached["age_days"], cached["idle_days"], cached["scanned_at"])


def _refresh_jellyfin_after_execute(cfg):
    """Ask Jellyfin to refresh its library after a run, so a just-deleted
    title stops reappearing as a candidate on the next scan (Jellyfin's own
    view of the filesystem can lag behind the delete that was just issued).

    Best-effort and never fatal: a refresh failure must not fail an
    otherwise-successful delete run. It is journaled as its own entry
    instead, independent of every title's own result.
    """
    try:
        jf = JellyfinClient(cfg["jellyfin_url"], cfg["jellyfin_api_key"])
        jf.refresh_library()
        journal.append({"kind": "jellyfin_refresh", "status": "ok"})
    except Exception as exc:
        journal.append({"kind": "jellyfin_refresh", "status": "error", "detail": str(exc)})


@app.route("/api/library/plan", methods=["POST"])
def api_plan():
    cfg = cfg_mod.load()
    body = flask_request.get_json(force=True) or {}
    jf_ids = body.get("jf_ids") or []
    if not jf_ids:
        return jsonify({"error": "no titles selected"}), 400

    selection = _selected(cfg, jf_ids)
    if selection is None:
        return jsonify({"error": "no scan available yet; run a scan first"}), 409

    try:
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

        selection = _selected(cfg, jf_ids)
        if selection is None:
            return jsonify({"error": "no scan available yet; run a scan first"}), 409

        try:
            # Note: execute() re-checks the blast-radius caps itself, so a second
            # pipeline.plan() call here would be redundant -- omitted.
            results = pipeline.execute(selection, cfg, _build_clients(cfg))
        except pipeline.BlastRadiusExceeded as e:
            return jsonify({"error": str(e), "blast_radius": True}), 409
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    _clear_last_plan()
    _update_candidates_cache_after_execute(results)
    _refresh_jellyfin_after_execute(cfg)
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
