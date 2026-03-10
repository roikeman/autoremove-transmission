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
        merged = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
        with open(CONFIG_PATH, "w") as f:
            json.dump(merged, f, indent=2)
        return merged
