"""Defect 2 (path-prefix normalization) and Defect 3 (duplicate orphan
rows from nested scan dirs), both rooted in the same production bug:
Transmission's container has two overlapping bind mounts
(/share/downloads -> /downloads and /share -> /share), so a torrent's
downloadDir can come back under either prefix even though the app only
ever sees the data at /share/...
"""
import os

from clients.transmission import is_deletable, normalize_path

DEFAULT_MAP = {"/downloads": "/share/downloads"}


# --- normalize_path (Defect 2) ---------------------------------------

def test_normalize_path_maps_downloads_prefix():
    cfg = {"path_prefix_map": DEFAULT_MAP}
    assert (normalize_path("/downloads/complete/radarr/x.mkv", cfg)
            == "/share/downloads/complete/radarr/x.mkv")


def test_normalize_path_leaves_share_prefix_unchanged():
    cfg = {"path_prefix_map": DEFAULT_MAP}
    p = "/share/downloads/complete/radarr/x.mkv"
    assert normalize_path(p, cfg) == p


def test_normalize_path_respects_segment_boundary():
    """/downloadsfoo/x.mkv must NOT be treated as under the /downloads
    prefix just because it starts with the same characters."""
    cfg = {"path_prefix_map": DEFAULT_MAP}
    p = "/downloadsfoo/x.mkv"
    assert normalize_path(p, cfg) == p


def test_normalize_path_longest_prefix_wins():
    cfg = {"path_prefix_map": {
        "/downloads": "/share/downloads",
        "/downloads/complete": "/share/other",
    }}
    assert (normalize_path("/downloads/complete/radarr/x.mkv", cfg)
            == "/share/other/radarr/x.mkv")


def test_normalize_path_no_match_returned_as_is():
    """An unmapped path must be returned unchanged, never rewritten to a
    guess -- the fail-closed logic in is_deletable relies on this."""
    cfg = {"path_prefix_map": {}}
    assert normalize_path("/downloads/x.mkv", cfg) == "/downloads/x.mkv"


def test_normalize_path_empty_input():
    assert normalize_path("", {"path_prefix_map": DEFAULT_MAP}) == ""


# --- end-to-end: is_deletable across the mount-prefix bug (Defect 2) --

def test_is_deletable_recognises_downloads_prefixed_hardlink(tmp_tree):
    """The production bug, reproduced end-to-end: Transmission reports
    downloadDir as "/downloads" (a path that doesn't exist on this
    machine), but once normalized through path_prefix_map it resolves to
    the real hardlinked file -- so this must come back not-deletable,
    never fail open just because the raw path didn't stat."""
    a = tmp_tree("a.mkv", linked=True)
    real_dir = os.path.dirname(a)
    name = os.path.basename(a)
    cfg = {"path_prefix_map": {"/downloads": real_dir}}
    torrent = {"downloadDir": "/downloads", "files": [{"name": name}]}
    assert is_deletable(torrent, cfg) is False


def test_is_deletable_downloads_prefixed_unlinked_still_deletable(tmp_tree):
    a = tmp_tree("a.mkv")
    real_dir = os.path.dirname(a)
    name = os.path.basename(a)
    cfg = {"path_prefix_map": {"/downloads": real_dir}}
    torrent = {"downloadDir": "/downloads", "files": [{"name": name}]}
    assert is_deletable(torrent, cfg) is True


# --- orphan scan dedup (Defect 3) --------------------------------------

def _configured_app(tmp_path, monkeypatch, path_prefix_map=None):
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    if path_prefix_map is not None:
        config.save({"path_prefix_map": path_prefix_map})
    import app as app_module
    return app_module


def test_orphan_file_under_nested_scan_dir_appears_once(tmp_path, monkeypatch):
    app_module = _configured_app(tmp_path, monkeypatch)

    complete = tmp_path / "share" / "downloads" / "complete"
    radarr = complete / "radarr"
    radarr.mkdir(parents=True)
    orphan_file = radarr / "orphan.mkv"
    orphan_file.write_bytes(b"x" * 100)

    torrents = [
        {"downloadDir": str(complete), "files": []},
        {"downloadDir": str(radarr), "files": []},
    ]
    monkeypatch.setattr(app_module, "get_all_torrents", lambda: torrents)

    orphans, _total = app_module.get_orphan_files()
    matches = [o for o in orphans if os.path.normpath(o["path"]) == str(orphan_file)]
    assert len(matches) == 1


def test_downloads_prefixed_torrent_file_not_reported_as_orphan(tmp_path, monkeypatch):
    real_base = tmp_path / "share" / "downloads"
    app_module = _configured_app(
        tmp_path, monkeypatch, path_prefix_map={"/downloads": str(real_base)})

    real_dir = real_base / "radarr"
    real_dir.mkdir(parents=True)
    (real_dir / "movie.mkv").write_bytes(b"x" * 50)

    torrents = [
        {"downloadDir": "/downloads/radarr", "files": [{"name": "movie.mkv"}]},
    ]
    monkeypatch.setattr(app_module, "get_all_torrents", lambda: torrents)

    orphans, total_bytes = app_module.get_orphan_files()
    assert orphans == []
    assert total_bytes == 0


def test_collapse_nested_scan_dirs_respects_segment_boundary(tmp_path, monkeypatch):
    """A sibling dir that merely shares a string prefix (not a real path
    segment) must not be collapsed away."""
    app_module = _configured_app(tmp_path, monkeypatch)

    downloads = tmp_path / "share" / "downloads"
    downloads_extra = tmp_path / "share" / "downloads-extra"
    downloads.mkdir(parents=True)
    downloads_extra.mkdir(parents=True)
    (downloads_extra / "orphan.mkv").write_bytes(b"x" * 10)

    torrents = [
        {"downloadDir": str(downloads), "files": []},
        {"downloadDir": str(downloads_extra), "files": []},
    ]
    monkeypatch.setattr(app_module, "get_all_torrents", lambda: torrents)

    orphans, _total = app_module.get_orphan_files()
    assert any(o["name"] == "orphan.mkv" for o in orphans)
