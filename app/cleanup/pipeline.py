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
