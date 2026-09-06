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
