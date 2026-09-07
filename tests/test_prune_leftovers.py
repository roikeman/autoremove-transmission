"""Fix 1: prune leftover metadata/artwork under a deleted title's path.

The fail-safe rule is the load-bearing behaviour here: if ANY file remains
under the title's path that isn't positively recognised as prunable
metadata/artwork, nothing is pruned and the whole tree is left alone. These
tests hammer that rule directly against cleanup.pipeline.prune_leftovers();
tests/test_pipeline.py covers the same rule wired into execute().
"""
import os
import pytest
from cleanup import pipeline, journal


@pytest.fixture(autouse=True)
def _journal_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "j.jsonl"))


def test_prune_removes_lone_nfo_and_directory(tmp_path):
    show_dir = tmp_path / "share" / "Wednesday"
    show_dir.mkdir(parents=True)
    (show_dir / "tvshow.nfo").write_bytes(b"x" * 2260)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert freed == 2260
    assert count == 1
    assert not show_dir.exists()


def test_prune_leaves_tree_untouched_when_unrecognised_video_file_present(tmp_path):
    # THE FAIL-SAFE RULE: a single unrecognised file anywhere under the tree
    # means hands off the *entire* tree, including the files that would
    # otherwise have been pruned.
    show_dir = tmp_path / "share" / "Wednesday"
    show_dir.mkdir(parents=True)
    nfo = show_dir / "tvshow.nfo"
    nfo.write_bytes(b"x" * 100)
    leftover = show_dir / "episode.mkv"
    leftover.write_bytes(b"y" * 500)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert (freed, count) == (0, 0)
    assert nfo.exists()       # the .nfo must survive too
    assert leftover.exists()
    assert show_dir.exists()


def test_prune_leaves_tree_untouched_for_unrecognised_extension(tmp_path):
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    (show_dir / "tvshow.nfo").write_bytes(b"x")
    stray = show_dir / "notes.txt"
    stray.write_text("hello")

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert (freed, count) == (0, 0)
    assert stray.exists()
    assert (show_dir / "tvshow.nfo").exists()


def test_prune_leaves_tree_untouched_for_file_with_no_extension(tmp_path):
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    (show_dir / "tvshow.nfo").write_bytes(b"x")
    stray = show_dir / "README"
    stray.write_text("hi")

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert (freed, count) == (0, 0)
    assert stray.exists()


def test_prune_recurses_into_season_subdirectories(tmp_path):
    show_dir = tmp_path / "share" / "Show"
    season1 = show_dir / "Season 01"
    season1.mkdir(parents=True)
    (show_dir / "poster.jpg").write_bytes(b"a" * 10)
    (season1 / "episode.nfo").write_bytes(b"b" * 20)
    (season1 / "episode.srt").write_bytes(b"c" * 5)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert freed == 35
    assert count == 3
    assert not show_dir.exists()


def test_prune_never_removes_configured_library_root(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "tvshow.nfo").write_bytes(b"x" * 50)

    freed, count = pipeline.prune_leftovers(str(root), [str(root)])

    assert freed == 50
    assert count == 1
    assert root.exists()  # the root itself must survive even when emptied
    assert not (root / "tvshow.nfo").exists()


def test_prune_extension_match_is_case_insensitive(tmp_path):
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    (show_dir / "TVSHOW.NFO").write_bytes(b"x" * 10)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert freed == 10
    assert count == 1
    assert not show_dir.exists()


def test_prune_missing_path_is_noop(tmp_path):
    freed, count = pipeline.prune_leftovers(
        str(tmp_path / "share" / "Gone"), [str(tmp_path / "share")])
    assert (freed, count) == (0, 0)


def test_prune_path_outside_roots_is_noop(tmp_path):
    outside = tmp_path / "elsewhere" / "Show"
    outside.mkdir(parents=True)
    (outside / "tvshow.nfo").write_bytes(b"x")

    freed, count = pipeline.prune_leftovers(str(outside), [str(tmp_path / "share")])

    assert (freed, count) == (0, 0)
    assert (outside / "tvshow.nfo").exists()


def test_race_file_appearing_after_approval_walk_is_never_pruned(tmp_path, monkeypatch):
    """THE CRITICAL FIX's regression guard.

    The validating walk approves a tree containing only a .nfo. Immediately
    after that walk is drained -- before deletion actually runs -- a .mkv
    appears in the same directory: a concurrent Sonarr import, an
    in-flight transcode, whatever. The old implementation delegated
    deletion to a second, independent os.walk (_delete_tree) which would
    discover and delete the newly-arrived .mkv unconditionally, because it
    re-scans the directory from scratch rather than reusing the approved
    list. The fix must delete only the exact paths the approval walk
    collected, so a file that walk never saw must survive no matter what
    shows up on disk afterward.

    The race is simulated at the os.walk level (not at delete_file) because
    a single, sub-directory-less tree's directory listing is captured
    eagerly by the first os.walk call, before its per-file loop runs --
    injecting the file from inside a delete_file patch would arrive too
    late to be picked up by that same walk, and would not exercise the
    real bug (a *second*, independent walk started after the first one
    already approved the tree).
    """
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    nfo = show_dir / "tvshow.nfo"
    nfo.write_bytes(b"x" * 10)
    mkv = show_dir / "episode.mkv"

    real_walk = os.walk
    state = {"calls": 0}

    def racing_walk(top, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            # Drain the approval walk's results before injecting -- this
            # is the file "appearing" strictly after that walk completed.
            result = list(real_walk(top, *args, **kwargs))
            mkv.write_bytes(b"y" * 500)
            return iter(result)
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", racing_walk)

    pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert mkv.exists()
    assert mkv.read_bytes() == b"y" * 500
    # The directory can't have been emptied/removed either, since the
    # never-approved .mkv is still sitting in it.
    assert show_dir.exists()


def test_collected_path_revalidated_before_delete_and_skipped_if_no_longer_prunable(tmp_path, monkeypatch):
    """Belt and braces: even a path the approval walk collected gets its
    extension re-checked immediately before deletion. If that re-check
    ever fails, the path is skipped, not deleted."""
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    nfo = show_dir / "tvshow.nfo"
    nfo.write_bytes(b"x" * 10)

    real_is_prunable = pipeline._is_prunable
    calls = {"n": 0}

    def flaky_is_prunable(filename):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_is_prunable(filename)  # the approval-walk check
        return False  # the deletion-time re-check: no longer validates

    monkeypatch.setattr(pipeline, "_is_prunable", flaky_is_prunable)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert (freed, count) == (0, 0)
    assert nfo.exists()


def test_prune_per_file_failure_still_prunes_remaining_and_reports_truthful_count(tmp_path, monkeypatch):
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    nfo = show_dir / "tvshow.nfo"
    nfo.write_bytes(b"x" * 10)
    jpg = show_dir / "poster.jpg"
    jpg.write_bytes(b"y" * 20)

    real_delete_file = pipeline.delete_file

    def flaky_delete_file(path, roots):
        if path.endswith("tvshow.nfo"):
            raise OSError("disk error")
        return real_delete_file(path, roots)

    monkeypatch.setattr(pipeline, "delete_file", flaky_delete_file)

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert count == 1
    assert freed == 20
    assert nfo.exists()
    assert not jpg.exists()


def test_prune_removes_broken_symlink_and_directory_empties(tmp_path):
    show_dir = tmp_path / "share" / "Show"
    show_dir.mkdir(parents=True)
    link = show_dir / "poster.jpg"
    os.symlink(str(show_dir / "ghost.jpg"), str(link))

    freed, count = pipeline.prune_leftovers(str(show_dir), [str(tmp_path / "share")])

    assert count == 1
    assert not show_dir.exists()
