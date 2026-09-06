import os
from app import is_deletable


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


def test_missing_file_is_skipped_not_fatal(tmp_tree):
    a = tmp_tree("a.mkv")
    t = _torrent([a])
    t["files"].append({"name": "ghost.mkv"})
    assert is_deletable(t) is True
