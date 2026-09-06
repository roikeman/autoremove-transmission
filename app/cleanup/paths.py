import os


class PathOutsideRoots(Exception):
    """Raised when a path resolves outside every configured root."""


def assert_within_roots(path, roots):
    """Return the resolved path, or raise if it escapes every root.

    Symlinks are resolved before the check, so a link inside a root that
    points outside it is rejected.
    """
    if not path or not os.path.isabs(path):
        raise PathOutsideRoots(f"not an absolute path: {path!r}")

    resolved = os.path.realpath(path)

    for root in roots or []:
        real_root = os.path.realpath(root)
        if resolved == real_root or resolved.startswith(real_root + os.sep):
            return os.path.normpath(resolved)

    raise PathOutsideRoots(f"path outside configured roots: {path!r}")


def delete_file(path, roots):
    """Delete one file inside the roots. Returns bytes freed (0 if absent)."""
    safe = assert_within_roots(path, roots)

    try:
        size = os.stat(safe).st_size
    except FileNotFoundError:
        return 0

    os.remove(safe)

    parent = os.path.dirname(safe)
    try:
        if os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass

    return size
