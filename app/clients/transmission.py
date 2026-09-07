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


def normalize_path(path, cfg=None):
    """Rewrite a Transmission-reported path onto the mount view this app
    actually sees on disk.

    The Transmission container has overlapping bind mounts (e.g.
    /downloads and /share/downloads both resolve to the same data), so a
    torrent's downloadDir/file paths aren't reliably rooted at the same
    prefix the app itself sees. `cfg["path_prefix_map"]` (see
    config.DEFAULTS) lists source-prefix -> destination-prefix rewrites;
    this applies the longest matching source prefix, matched only on a
    path-segment boundary -- "/downloads/x" matches the prefix
    "/downloads", but "/downloadsfoo/x" does not.

    Purely lexical: this never touches the filesystem. A path that
    matches no configured prefix -- including one already under the
    app's own view (e.g. already "/share/...") -- is returned unchanged
    rather than guessed at, so callers that stat the result (is_deletable)
    still see a real, honest failure for anything this mapping can't
    resolve, instead of a silently-wrong rewrite.
    """
    if not path:
        return path

    if cfg is None:
        cfg = _cfg()
    prefix_map = cfg.get("path_prefix_map") or {}

    best_src = None
    for src in prefix_map:
        if not src:
            continue
        src_norm = src.rstrip("/") or "/"
        if path == src_norm or path.startswith(src_norm + "/"):
            if best_src is None or len(src_norm) > len(best_src):
                best_src = src_norm

    if best_src is None:
        return path

    dst = prefix_map[best_src].rstrip("/")
    remainder = path[len(best_src):]
    return dst + remainder


def is_deletable(torrent, cfg=None):
    """Return True only if every one of the torrent's files was verified
    on disk and none of them has extra hardlinks (st_nlink > 1).

    Fails CLOSED: if any file's stat cannot be resolved -- missing,
    permission denied, or any other OSError, most commonly because
    Transmission reported a path under a mount prefix this app doesn't
    also see -- this returns False instead of skipping that file. Files
    it never managed to examine cannot be used as evidence of "no
    hardlink found"; the entire purpose of this guard is to protect data
    this app cannot positively confirm is safe to delete, and skipping
    unverifiable files defeats that guard at precisely the moment it
    cannot verify anything. A torrent with no files listed is likewise
    not deletable, for the same "cannot verify => not deletable" reason,
    not because it's "safe" by default.
    """
    files = torrent.get("files", [])
    download_dir = torrent.get("downloadDir", "")

    if not files:
        return False

    if cfg is None:
        cfg = _cfg()
    normalized_dir = normalize_path(download_dir, cfg)

    for file_entry in files:
        path = os.path.join(normalized_dir, file_entry["name"])
        try:
            if os.stat(path).st_nlink > 1:
                return False
        except (FileNotFoundError, PermissionError, OSError):
            return False

    return True
