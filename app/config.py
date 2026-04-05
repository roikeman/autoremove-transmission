import json
import os
import threading

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")

DEFAULTS = {
    "transmission_host":     "192.168.1.132",
    "transmission_port":     "9091",
    "transmission_user":     "",
    "transmission_pass":     "",
    "transmission_rpc_path": "/transmission/rpc",
    "exclude_paths":         [],
}

_lock = threading.Lock()


def load():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            return dict(DEFAULTS)
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return {**DEFAULTS, **data}


def save(data):
    with _lock:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        incoming = {k: v for k, v in data.items() if k in DEFAULTS}
        # Ensure exclude_paths is always a list of non-empty stripped strings
        if "exclude_paths" in incoming:
            raw = incoming["exclude_paths"]
            if isinstance(raw, str):
                raw = [p for p in raw.splitlines() if p.strip()]
            incoming["exclude_paths"] = [p.strip() for p in raw if str(p).strip()]
        merged = {**DEFAULTS, **incoming}
        with open(CONFIG_PATH, "w") as f:
            json.dump(merged, f, indent=2)
        return merged
