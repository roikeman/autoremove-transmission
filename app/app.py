import os
import threading
import requests
from flask import Flask, jsonify, render_template, request as flask_request
import config as cfg_mod

_session    = requests.Session()
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
        "fields": ["id", "name", "totalSize", "downloadDir", "files", "addedDate", "trackers"]
    })
    return result["arguments"]["torrents"]


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


app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    cfg = _cfg()
    # Never expose password in GET
    safe = {k: v for k, v in cfg.items()}
    safe["transmission_pass"] = "••••••••" if cfg["transmission_pass"] else ""
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    global _session_id
    data = flask_request.get_json(force=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    # If password placeholder was sent back, keep existing password
    current = cfg_mod.load()
    if data.get("transmission_pass", "").startswith("••"):
        data["transmission_pass"] = current["transmission_pass"]

    saved = cfg_mod.save(data)
    # Reset RPC session so next call re-authenticates with new settings
    with _session_lock:
        _session_id = None

    return jsonify({"status": "ok", "settings": {k: v for k, v in saved.items() if k != "transmission_pass"}})


@app.route("/api/health")
def health():
    try:
        cfg = _cfg()
        rpc_call("session-get", {"fields": ["version"]})
        return jsonify({"status": "ok", "transmission": f"{cfg['transmission_host']}:{cfg['transmission_port']}"})
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
