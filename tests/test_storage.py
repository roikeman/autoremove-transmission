import pytest

import storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_filesystem_usage_reports_used_and_free(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()

    class Usage:
        f_blocks = 100
        f_frsize = 10
        f_bavail = 25

    monkeypatch.setattr(storage.os, "statvfs", lambda path: Usage())
    result = storage.filesystem_usage([str(media)])

    assert len(result) == 1
    assert result[0]["totalBytes"] == 1000
    assert result[0]["usedBytes"] == 750
    assert result[0]["freeBytes"] == 250
    assert result[0]["usedPercent"] == 75.0


def test_filesystem_usage_groups_paths_on_same_device(tmp_path):
    first = tmp_path / "downloads"
    second = tmp_path / "library"
    first.mkdir()
    second.mkdir()

    assert len(storage.filesystem_usage([str(first), str(second)])) == 1


def test_filesystem_usage_accepts_file_and_ignores_missing_path(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"data")

    result = storage.filesystem_usage([str(movie), str(tmp_path / "missing")])
    assert len(result) == 1
    assert result[0]["paths"]


def test_storage_endpoint_uses_normalized_transmission_and_cached_paths(
        client, tmp_path, monkeypatch):
    import app as app_module
    import config

    downloads = tmp_path / "downloads"
    library = tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    movie = library / "movie.mkv"
    movie.write_bytes(b"x")

    config.save({
        "library_roots": [str(library)],
        "path_prefix_map": {"/remote": str(downloads)},
    })
    monkeypatch.setattr(app_module, "get_all_torrents", lambda: [
        {"downloadDir": "/remote"},
    ])
    monkeypatch.setattr(app_module, "_load_candidates_cache", lambda: {
        "candidates": [{"path": str(movie)}],
    })

    body = client.get("/api/storage").get_json()
    assert body["filesystems"]
    filesystem = body["filesystems"][0]
    assert filesystem["usedBytes"] >= 0
    assert filesystem["freeBytes"] >= 0
    assert filesystem["totalBytes"] > 0


def test_storage_endpoint_survives_transmission_failure(client, tmp_path, monkeypatch):
    import app as app_module
    import config

    library = tmp_path / "library"
    library.mkdir()
    config.save({"library_roots": [str(library)]})

    def fail():
        raise RuntimeError("Transmission unavailable")

    monkeypatch.setattr(app_module, "get_all_torrents", fail)
    body = client.get("/api/storage").get_json()
    assert len(body["filesystems"]) == 1
