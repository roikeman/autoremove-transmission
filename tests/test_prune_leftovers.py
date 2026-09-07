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
