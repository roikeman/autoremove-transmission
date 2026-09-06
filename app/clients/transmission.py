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
