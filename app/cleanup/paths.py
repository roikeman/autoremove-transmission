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
        if not root:
            continue
        real_root = os.path.realpath(root)
        if resolved == real_root or resolved.startswith(real_root + os.sep):
            return os.path.normpath(resolved)

    raise PathOutsideRoots(f"path outside configured roots: {path!r}")


def delete_file(path, roots):
    """Delete one file inside the roots. Returns bytes freed (0 if absent).

    Operates on `path` itself via os.lstat/os.remove -- never on the
    symlink-resolved form -- so that if `path` names a symlink, the link
    node itself is what gets measured and removed, not whatever it points
    at. This matters most for a dangling symlink (target doesn't exist):
    os.stat follows the link and raises FileNotFoundError against the
    *target*, which used to make this function report "nothing to delete"
    while the link itself sat there un-removed, permanently blocking its
    directory from ever emptying. It also means a symlink is credited its
    own (tiny) size, never the target's.

    assert_within_roots still resolves symlinks for the containment check
    below -- a link pointing outside every configured root is still
    rejected -- only the actual file-system operations changed to act on
    the link itself rather than its resolved target.
    """
    assert_within_roots(path, roots)
    target = os.path.normpath(path)

    try:
        size = os.lstat(target).st_size
    except FileNotFoundError:
        return 0

    os.remove(target)

    parent = os.path.dirname(target)
    real_roots = {os.path.realpath(root) for root in roots or [] if root}
    try:
        if (
            os.path.isdir(parent)
            and os.path.realpath(parent) not in real_roots
            and not os.listdir(parent)
        ):
            os.rmdir(parent)
    except OSError:
        pass

    return size
