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
    "min_seed_seconds":      86400,
    "max_titles_per_run":    50,
    "max_bytes_per_run":     1099511627776,
    # Cleanup recommendation score. Higher means "delete sooner". These are
    # presentation/ranking weights only; age_days and idle_days remain hard
    # eligibility gates and deletion is still always explicitly confirmed.
    "score_never_opened":            60,
    "score_fully_watched":           10,
    "score_near_complete":           20,
    "score_sampled":                 80,
    "score_mid_watch":               70,
    "score_seed_adjustment":         -5,
    "score_favorite_adjustment":     -100,
    "score_rating_pivot":            7,
    "score_rating_points_per_point": 5,
    "library_roots":         ["/share"],
    # Transmission's container has overlapping bind mounts (e.g.
    # /downloads and /share/downloads both resolve to the same data), so
    # a torrent's reported downloadDir/file paths aren't reliably rooted
    # at the prefix this app sees on disk. Each entry rewrites a source
    # prefix to the destination prefix the app actually sees; see
    # clients.transmission.normalize_path for the matching rules.
    "path_prefix_map":       {"/downloads": "/share/downloads"},
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

_INT_KEYS = {"age_days", "idle_days", "min_seed_seconds", "max_titles_per_run", "max_bytes_per_run"}
_FLOAT_KEYS = {
    "score_never_opened",
    "score_fully_watched",
    "score_near_complete",
    "score_sampled",
    "score_mid_watch",
    "score_seed_adjustment",
    "score_favorite_adjustment",
    "score_rating_pivot",
    "score_rating_points_per_point",
}

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

        for key in _FLOAT_KEYS:
            if key in incoming:
                incoming[key] = float(incoming[key])

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
