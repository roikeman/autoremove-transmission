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


# seedRatioMode: 0 = defer to the session's global ratio limit,
# 1 = use this torrent's own limit, 2 = seed forever.
_MODE_GLOBAL = 0
_MODE_PER_TORRENT = 1
_MODE_UNLIMITED = 2


def should_keep_seeding(torrent, session_ratio_limit=None, min_seed_seconds=0):
    """True when the torrent has not met its seed ratio OR its minimum seed
    time and must be kept.

    `session_ratio_limit` is the session's global seedRatioLimit, resolved
    once by the caller (session-get RPC) and passed in so this function
    stays a pure, unit-testable predicate. Pass None when the session limit
    is unknown or disabled (mode 0 then has no target, so the torrent may
    be swept).

    `min_seed_seconds` is a simple, configured floor on `secondsSeeding`
    (already fetched by clients/transmission.py, previously unused). This
    is deliberately NOT a full emulation of Transmission's per-torrent
    seedIdleMode/seedIdleLimit semantics -- just one extra, always-applied
    minimum. Pass 0 (the falsy default) to disable it.
    """
    mode = torrent.get("seedRatioMode", _MODE_GLOBAL)
    if mode == _MODE_UNLIMITED:
        return True

    if min_seed_seconds and float(torrent.get("secondsSeeding") or 0) < min_seed_seconds:
        return True

    if mode == _MODE_PER_TORRENT:
        limit = float(torrent.get("seedRatioLimit") or 0)
        if limit <= 0:
            return False
        return float(torrent.get("uploadRatio") or 0) < limit

    # mode 0 (global): the per-torrent seedRatioLimit field is meaningless
    # here (Transmission typically reports 0 for it under this mode) -- the
    # session's own limit is the real target.
    if session_ratio_limit is None:
        return False
    return float(torrent.get("uploadRatio") or 0) < float(session_ratio_limit)


def _steps_for(candidate):
    steps = []
    if candidate.owner:
        steps.append(f"{candidate.owner}:delete/{candidate.owner_id}")
    else:
        steps.append(f"files:delete/{candidate.path}")
    steps.append(f"jellyfin:delete/{candidate.jf_id}")
    return steps


def _check_blast_radius(candidates, cfg):
    """Raise BlastRadiusExceeded if the selection exceeds the configured
    caps. Returns the total byte count so callers can reuse it."""
    max_titles = int(cfg.get("max_titles_per_run") or 0)
    max_bytes = int(cfg.get("max_bytes_per_run") or 0)
    total_bytes = sum(c.size_bytes for c in candidates)

    if max_titles and len(candidates) > max_titles:
        raise BlastRadiusExceeded(
            f"{len(candidates)} titles exceeds the cap of {max_titles}")
    if max_bytes and total_bytes > max_bytes:
        raise BlastRadiusExceeded(
            f"{total_bytes} bytes exceeds the cap of {max_bytes}")
    return total_bytes


def plan(candidates, cfg):
    """Describe what execute() would do. Deletes nothing."""
    total_bytes = _check_blast_radius(candidates, cfg)

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


def _capture_inode_keys(path):
    """Stat every file under `path` and return its {(st_dev, st_ino)} set.

    Must be called BEFORE the owner/file deletion step runs: once that step
    removes the files, they can no longer be stat'd. A missing or
    unreadable path yields an empty set rather than raising, so a
    candidate whose files are already gone never blocks the run.
    """
    keys = set()
    try:
        if os.path.isfile(path):
            files = [path]
        elif os.path.isdir(path):
            files = []
            for dirpath, _dirs, names in os.walk(path):
                files.extend(os.path.join(dirpath, n) for n in names)
        else:
            return keys
    except OSError:
        return keys

    for file_path in files:
        try:
            info = os.stat(file_path)
        except OSError:
            continue
        keys.add((info.st_dev, info.st_ino))
    return keys


def execute(candidates, cfg, clients):
    """Run the pipeline. Each title is independent; one failure never aborts
    the rest. Re-checks the blast-radius caps itself before deleting
    anything -- it must not rely on the caller having run plan() first.
    """
    _check_blast_radius(candidates, cfg)

    results = []
    inode_keys = set()
    for candidate in candidates:
        result, keys = _execute_one(candidate, cfg, clients)
        results.append(result)
        inode_keys |= keys

    _run_sweep(cfg, clients, inode_keys)
    return results


def _journal_step(candidate, step):
    """Journal one destructive step immediately, independent of the
    per-title completion entry written by _finish().

    A hard process death (OOM kill, power loss) between two successful
    destructive steps -- e.g. after the Sonarr/Radarr delete but before the
    Jellyfin delete -- would otherwise leave no record that anything
    happened at all, even though files are already gone. `kind: "step"`
    keeps these distinguishable from the title-completion (`kind: "title"`)
    and sweep (`kind: "transmission_sweep"`) entries for read_recent
    consumers and the UI.
    """
    journal.append({
        "kind": "step",
        "jf_id": candidate.jf_id,
        "title": candidate.title,
        "step": step["step"],
        "status": step["status"],
        "detail": step["detail"],
    })


def _execute_one(candidate, cfg, clients):
    steps = []
    freed = 0

    # Capture inode identity before anything is deleted -- this is how the
    # (single, end-of-run) transmission sweep later recognizes which
    # torrents belong to titles this run actually deleted.
    inode_keys = _capture_inode_keys(candidate.path)

    # 1. Remove from the *arr that owns it, or delete files directly.
    try:
        if candidate.owner == "sonarr":
            clients.sonarr.delete_item(candidate.owner_id)
            step = {"step": f"sonarr:delete/{candidate.owner_id}", "status": "ok", "detail": ""}
        elif candidate.owner == "radarr":
            clients.radarr.delete_item(candidate.owner_id)
            step = {"step": f"radarr:delete/{candidate.owner_id}", "status": "ok", "detail": ""}
        else:
            freed += _delete_tree(candidate.path, cfg.get("library_roots") or [])
            step = {"step": "files:delete", "status": "ok", "detail": str(freed)}
        steps.append(step)
        _journal_step(candidate, step)
    except Exception as exc:
        steps.append({"step": "owner:delete", "status": "error", "detail": str(exc)})
        return _finish(candidate, "failed", steps, freed), inode_keys

    # 2. Drop the Jellyfin entry and its metadata. A title is "deleted" once
    # this and the owner step above have both succeeded -- status no
    # longer depends on the (now run-wide, not per-title) transmission
    # sweep.
    status = "deleted"
    try:
        clients.jellyfin.delete_item(candidate.jf_id)
        step = {"step": f"jellyfin:delete/{candidate.jf_id}", "status": "ok", "detail": ""}
        steps.append(step)
        _journal_step(candidate, step)
    except Exception as exc:
        steps.append({"step": "jellyfin:delete", "status": "error", "detail": str(exc)})
        status = "partial"

    return _finish(candidate, status, steps, freed), inode_keys


def _delete_tree(path, roots):
    safe = assert_within_roots(path, roots)
    if os.path.isfile(safe):
        return delete_file(safe, roots)

    real_roots = {os.path.realpath(root) for root in roots or [] if root}

    freed = 0
    for dirpath, _dirs, files in os.walk(safe, topdown=False):
        for name in files:
            freed += delete_file(os.path.join(dirpath, name), roots)
        if os.path.realpath(dirpath) in real_roots:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return freed


def _torrent_inode_keys(torrent):
    """The {(st_dev, st_ino)} set for a torrent's on-disk files."""
    download_dir = torrent.get("downloadDir", "")
    keys = set()
    for file_entry in torrent.get("files", []) or []:
        name = file_entry.get("name") or ""
        if not name:
            continue
        try:
            info = os.stat(os.path.join(download_dir, name))
        except OSError:
            continue
        keys.add((info.st_dev, info.st_ino))
    return keys


def _session_ratio_limit(clients):
    """Fetch the session's global seed ratio limit, or None if disabled."""
    response = clients.transmission.rpc_call(
        "session-get", {"fields": ["seedRatioLimit", "seedRatioLimited"]})
    args = (response or {}).get("arguments", {})
    if not args.get("seedRatioLimited"):
        return None
    return float(args.get("seedRatioLimit") or 0)


def _sweep_torrents(cfg, clients, inode_keys):
    """Remove torrents belonging to this run's deleted titles that have no
    remaining hardlink. Returns (removed_count, kept_for_seeding_count).

    Scoped to `inode_keys`: a torrent is only ever touched if at least one
    of its files shares an inode with a file this run actually deleted.
    A torrent unrelated to the selection -- however orphaned it may
    independently be -- is never removed here.
    """
    removed = 0
    kept = 0
    session_limit = _session_ratio_limit(clients) if cfg.get("seed_guard") else None
    min_seed_seconds = int(cfg.get("min_seed_seconds") or 0)

    for torrent in clients.transmission.get_all_torrents():
        if not clients.transmission.is_deletable(torrent):
            continue
        if not (inode_keys & _torrent_inode_keys(torrent)):
            continue
        if cfg.get("seed_guard") and should_keep_seeding(torrent, session_limit, min_seed_seconds):
            kept += 1
            continue
        clients.transmission.remove_torrent(torrent["id"], delete_data=True)
        removed += 1
    return removed, kept


def _run_sweep(cfg, clients, inode_keys):
    """Run the transmission sweep once for the whole run and journal the
    outcome as its own entry, independent of any title's status."""
    try:
        removed, kept = _sweep_torrents(cfg, clients, inode_keys)
        journal.append({"kind": "transmission_sweep", "status": "ok",
                         "removed": removed, "kept": kept})
    except Exception as exc:
        journal.append({"kind": "transmission_sweep", "status": "error",
                         "detail": str(exc)})


def _finish(candidate, status, steps, freed):
    result = {
        "jf_id": candidate.jf_id,
        "title": candidate.title,
        "owner": candidate.owner,
        "status": status,
        "steps": steps,
        "bytes_freed": freed,
    }
    journal.append({"kind": "title", **result})
    return result
