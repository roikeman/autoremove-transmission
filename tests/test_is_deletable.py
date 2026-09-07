import os
from clients.transmission import is_deletable


def _torrent(paths):
    return {
        "downloadDir": os.path.dirname(paths[0]) if paths else "",
        "files": [{"name": os.path.basename(p)} for p in paths],
    }


def test_unlinked_files_are_deletable(tmp_tree):
    a = tmp_tree("a.mkv")
    assert is_deletable(_torrent([a])) is True


def test_hardlinked_file_blocks_deletion(tmp_tree):
    a = tmp_tree("a.mkv", linked=True)
    assert is_deletable(_torrent([a])) is False


def test_any_hardlinked_file_blocks_deletion(tmp_tree):
    a = tmp_tree("a.mkv")
    b = tmp_tree("b.mkv", linked=True)
    assert is_deletable(_torrent([a, b])) is False


def test_torrent_with_no_files_is_not_deletable():
    assert is_deletable({"downloadDir": "/nope", "files": []}) is False


def test_missing_file_fails_closed(tmp_tree):
    """Defect 1 (fail-open hardlink guard): this replaces the old
    test_missing_file_is_skipped_not_fatal, which pinned the previous
    (fail-open) behaviour -- a torrent with one resolvable, unlinked file
    plus one file that couldn't be stat-ed used to return True ("safe to
    delete") purely because the unresolvable file's stat error was
    swallowed and skipped. That's backwards for a guard whose entire job
    is to protect still-hardlinked data: a file this app cannot verify
    must never count as "no hardlink found". It now fails closed and
    returns False instead.
    """
    a = tmp_tree("a.mkv")
    t = _torrent([a])
    t["files"].append({"name": "ghost.mkv"})
    assert is_deletable(t) is False


def test_all_files_unstatable_fails_closed():
    """A torrent whose files cannot be resolved at all (e.g. every path
    reported under a mount prefix this app doesn't see) must fail closed,
    not be treated as deletable by default."""
    t = {
        "downloadDir": "/nope/does/not/exist",
        "files": [{"name": "a.mkv"}, {"name": "b.mkv"}],
    }
    assert is_deletable(t) is False


def test_all_files_statable_and_unlinked_still_deletable(tmp_tree):
    """No regression: when every file resolves and none is hardlinked,
    the torrent is still deletable."""
    a = tmp_tree("a.mkv")
    b = tmp_tree("b.mkv")
    assert is_deletable(_torrent([a, b])) is True
