import os


def _existing_directory(path):
    """Return an existing directory suitable for statvfs, or None.

    Library candidates may point at either a directory (series) or a file
    (movie), while Transmission reports directories. Resolve both forms to
    the directory that actually owns the data. Missing paths are ignored so
    the container's root filesystem is never reported as a misleading
    fallback for an unmapped media path.
    """
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    if os.path.isdir(resolved):
        return resolved
    if os.path.isfile(resolved):
        return os.path.dirname(resolved)
    return None


def _filesystem_root(path, device):
    """Walk upward to the visible root of the filesystem containing path."""
    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return current
        try:
            if os.stat(parent).st_dev != device:
                return current
        except OSError:
            return current
        current = parent


def filesystem_usage(paths):
    """Return one capacity record per filesystem represented by paths.

    st_dev performs the important deduplication: several download/library
    paths backed by one volume produce one card, while paths on distinct
    mounted filesystems are all returned. `freeBytes` uses f_bavail (space
    available to this process), which is more useful than space reserved for
    root and unavailable to the application.
    """
    grouped = {}
    for raw_path in paths:
        directory = _existing_directory(raw_path)
        if directory is None:
            continue
        try:
            stat = os.stat(directory)
            usage = os.statvfs(directory)
        except OSError:
            continue

        device = stat.st_dev
        root = _filesystem_root(directory, device)
        total = usage.f_blocks * usage.f_frsize
        free = usage.f_bavail * usage.f_frsize
        record = grouped.setdefault(device, {
            "paths": set(),
            "totalBytes": total,
            "usedBytes": max(total - free, 0),
            "freeBytes": free,
        })
        record["paths"].add(root)

    result = []
    for record in grouped.values():
        total = record["totalBytes"]
        used = record["usedBytes"]
        result.append({
            "paths": sorted(record["paths"]),
            "totalBytes": total,
            "usedBytes": used,
            "freeBytes": record["freeBytes"],
            "usedPercent": round((used / total * 100) if total else 0, 1),
        })
    return sorted(result, key=lambda item: item["paths"])
