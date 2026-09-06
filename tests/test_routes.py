import fcntl
import json
import os
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    from cleanup import journal
    monkeypatch.setattr(journal, "JOURNAL_PATH", str(tmp_path / "j.jsonl"))
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_library_page_renders(client):
    assert client.get("/library").status_code == 200


def test_settings_never_leaks_secret(client, tmp_path):
    import config
    config.save({"jellyfin_api_key": "super-secret"})
    body = client.get("/api/settings").get_data(as_text=True)
    assert "super-secret" not in body
    assert "••••••••" in body


def test_settings_reports_env_locked_keys(client, monkeypatch):
    monkeypatch.setenv("SONARR_API_KEY", "from-env")
    data = client.get("/api/settings").get_json()
    assert "sonarr_api_key" in data["env_locked"]


def test_candidates_requires_jellyfin_config(client):
    resp = client.get("/api/library/candidates")
    assert resp.status_code == 503
    assert "jellyfin" in resp.get_json()["error"].lower()


def test_execute_rejects_empty_selection(client):
    resp = client.post("/api/library/execute", json={"jf_ids": []})
    assert resp.status_code == 400


def test_execute_rejects_missing_body(client):
    assert client.post("/api/library/execute", json={}).status_code == 400


def test_journal_endpoint_returns_list(client):
    from cleanup import journal
    journal.append({"title": "X", "status": "deleted"})
    assert client.get("/api/library/journal").get_json()[0]["title"] == "X"


def test_test_connection_rejects_unknown_service(client):
    assert client.post("/api/test-connection/lidarr", json={}).status_code == 400


def test_execute_rejects_mismatched_selection(client, monkeypatch):
    """A plan for one selection must not authorize executing a different one
    -- e.g. a stale browser tab left open after the library changed."""
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})

    import app as app_module
    from cleanup.candidates import Candidate

    def fake_scan(cfg):
        return [
            Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                      added=None, last_played=None, episodes=1, watched=0,
                      progress_pct=0.0, owner=None, owner_id=None, bucket="A"),
            Candidate(jf_id="2", kind="movie", title="B", path="/x/b", size_bytes=10,
                      added=None, last_played=None, episodes=1, watched=0,
                      progress_pct=0.0, owner=None, owner_id=None, bucket="A"),
        ]

    monkeypatch.setattr(app_module, "_scan", fake_scan)

    plan_resp = client.post("/api/library/plan", json={"jf_ids": ["1"]})
    assert plan_resp.status_code == 200

    exec_resp = client.post("/api/library/execute", json={"jf_ids": ["2"]})
    assert exec_resp.status_code == 409
    assert "changed" in exec_resp.get_json()["error"].lower()


def test_execute_succeeds_with_matching_selection(client, monkeypatch):
    """A selection that exactly matches the prior /plan response is allowed
    through to pipeline.execute()."""
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})

    import app as app_module
    from cleanup.candidates import Candidate

    candidate = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                           added=None, last_played=None, episodes=1, watched=0,
                           progress_pct=0.0, owner=None, owner_id=None, bucket="A")

    monkeypatch.setattr(app_module, "_scan", lambda cfg: [candidate])
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    assert client.post("/api/library/plan", json={"jf_ids": ["1"]}).status_code == 200

    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["status"] == "deleted"


# --- Cross-process lock (Critical 1) ---------------------------------------
#
# gunicorn runs this app as multiple worker PROCESSES (Dockerfile: --workers
# 2). A threading.Lock is per-process, so two /execute requests routed to
# different workers would each acquire their own uncontended lock and run
# in parallel. These tests hold the lock file from a *separate* file
# descriptor (as a second process would), which a threading.Lock could
# never see contention from -- so they fail against the pre-fix code.

def _plan_a(client, monkeypatch, app_module):
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})
    from cleanup.candidates import Candidate

    candidate = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                           added=None, last_played=None, episodes=1, watched=0,
                           progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    monkeypatch.setattr(app_module, "_scan", lambda cfg: [candidate])
    resp = client.post("/api/library/plan", json={"jf_ids": ["1"]})
    assert resp.status_code == 200


def test_lock_file_created_and_blocks_concurrent_execute(client, monkeypatch):
    import app as app_module

    _plan_a(client, monkeypatch, app_module)
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    lock_path = app_module._lock_path()

    # Hold the lock the way a second gunicorn worker process would: a
    # wholly separate open file descriptor on the same path.
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert os.path.exists(lock_path)
        resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
        assert resp.status_code == 409
        assert "already in progress" in resp.get_json()["error"].lower()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # Once the other "worker" releases it, the same request succeeds and
    # the app's own lock is released afterward too.
    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200

    check_fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(check_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
    finally:
        fcntl.flock(check_fd, fcntl.LOCK_UN)
        os.close(check_fd)


def test_lock_released_even_when_execute_raises(client, monkeypatch):
    import app as app_module

    _plan_a(client, monkeypatch, app_module)

    def boom(selection, cfg, clients):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(app_module.pipeline, "execute", boom)

    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 502

    lock_path = app_module._lock_path()
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- Persisted plan across workers (Important 2) ----------------------------
#
# _last_plan used to be a module-level dict, invisible to a request handled
# by a different worker process. These tests clear the in-process attribute
# a naive fix might still rely on (harmless no-op against the file-backed
# implementation) to prove the check really goes through disk.

def test_persisted_plan_survives_different_worker(client, monkeypatch):
    import app as app_module

    _plan_a(client, monkeypatch, app_module)
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    # Simulate the execute request landing on a different gunicorn worker:
    # its module-level Python state starts fresh. Reset whatever in-process
    # bookkeeping might still exist -- fatal to the old in-memory dict,
    # a no-op against the persisted-file implementation.
    monkeypatch.setattr(app_module, "_last_plan", {"ids": set()}, raising=False)

    # Confirm the persisted file itself is what carries the selection.
    with open(app_module._plan_path()) as f:
        assert json.load(f)["ids"] == ["1"]

    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["status"] == "deleted"


def test_missing_plan_file_fails_closed(client):
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})

    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 409


def test_corrupt_plan_file_fails_closed(client, monkeypatch):
    import app as app_module

    _plan_a(client, monkeypatch, app_module)

    with open(app_module._plan_path(), "w") as f:
        f.write("{not valid json")

    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 409


# --- Scan cache (fix/scan-cache) --------------------------------------------
#
# A 91-user Jellyfin makes _scan() take ~5 minutes (one HTTP round-trip per
# user per item type). These tests use a counting fake JellyfinClient so an
# unexpected re-scan is caught directly, not inferred from timing.

class _CountingJellyfin:
    """Fake JellyfinClient that counts every items() call, so tests can
    assert a request served from cache never touches it again."""
    calls = 0

    def __init__(self, base_url, api_key):
        pass

    def users(self):
        return [{"Id": "u1"}]

    def items(self, user_id, item_type):
        _CountingJellyfin.calls += 1
        if item_type != "Movie":
            return []
        return [{
            "Id": "m1",
            "Name": "Old Movie",
            "Path": "/x/old-movie.mkv",
            "DateCreated": "2015-01-01T00:00:00.0000000Z",
            "MediaSources": [{"Size": 12345}],
            "UserData": {
                "Played": True,
                "LastPlayedDate": "2015-06-01T00:00:00.0000000Z",
            },
        }]


def _setup_counting_jellyfin(client, monkeypatch):
    import config
    import app as app_module

    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})
    _CountingJellyfin.calls = 0
    monkeypatch.setattr(app_module, "JellyfinClient", _CountingJellyfin)
    return app_module


def test_candidates_served_from_cache_without_rescanning(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    first = client.get("/api/library/candidates")
    assert first.status_code == 200
    first_body = first.get_json()
    assert first_body["cached"] is False
    assert first_body["scanned_at"]
    calls_after_first = _CountingJellyfin.calls
    assert calls_after_first > 0

    second = client.get("/api/library/candidates")
    assert second.status_code == 200
    second_body = second.get_json()
    assert second_body["cached"] is True
    assert second_body["scanned_at"] == first_body["scanned_at"]
    assert second_body["candidates"] == first_body["candidates"]
    # The Jellyfin client was not called again -- served entirely from cache.
    assert _CountingJellyfin.calls == calls_after_first


def test_candidates_refresh_param_forces_rescan(client, monkeypatch):
    _setup_counting_jellyfin(client, monkeypatch)

    client.get("/api/library/candidates")
    calls_after_first = _CountingJellyfin.calls

    resp = client.get("/api/library/candidates?refresh=1")
    assert resp.status_code == 200
    assert resp.get_json()["cached"] is False
    assert _CountingJellyfin.calls > calls_after_first


def test_candidates_changed_age_days_invalidates_cache(client, monkeypatch):
    _setup_counting_jellyfin(client, monkeypatch)

    client.get("/api/library/candidates")
    calls_after_first = _CountingJellyfin.calls

    resp = client.get("/api/library/candidates?age_days=30")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["cached"] is False
    assert body["age_days"] == 30
    assert _CountingJellyfin.calls > calls_after_first


def test_candidates_changed_idle_days_invalidates_cache(client, monkeypatch):
    _setup_counting_jellyfin(client, monkeypatch)

    client.get("/api/library/candidates")
    calls_after_first = _CountingJellyfin.calls

    resp = client.get("/api/library/candidates?idle_days=5")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["cached"] is False
    assert body["idle_days"] == 5
    assert _CountingJellyfin.calls > calls_after_first


def test_candidates_missing_cache_file_falls_back_to_scan(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    assert not os.path.exists(app_module._candidates_cache_path())
    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    assert resp.get_json()["cached"] is False


def test_candidates_corrupt_cache_file_falls_back_to_scan(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    os.makedirs(os.path.dirname(app_module._candidates_cache_path()), exist_ok=True)
    with open(app_module._candidates_cache_path(), "w") as f:
        f.write("{not valid json")

    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["cached"] is False
    assert _CountingJellyfin.calls > 0


def test_candidates_response_shape_unchanged_plus_cache_fields(client, monkeypatch):
    _setup_counting_jellyfin(client, monkeypatch)

    resp = client.get("/api/library/candidates")
    body = resp.get_json()
    for key in ("candidates", "preticked", "labels", "age_days", "idle_days",
                "cached", "scanned_at"):
        assert key in body


def test_plan_and_execute_reuse_cache_without_rescanning(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "m1", "title": "Old Movie", "status": "deleted"}])

    warm = client.get("/api/library/candidates")
    assert warm.status_code == 200
    calls_after_scan = _CountingJellyfin.calls
    assert calls_after_scan > 0

    plan_resp = client.post("/api/library/plan", json={"jf_ids": ["m1"]})
    assert plan_resp.status_code == 200
    assert _CountingJellyfin.calls == calls_after_scan  # plan reused the cache

    exec_resp = client.post("/api/library/execute", json={"jf_ids": ["m1"]})
    assert exec_resp.status_code == 200
    assert _CountingJellyfin.calls == calls_after_scan  # execute reused the cache too


def test_cache_cleared_after_successful_execute(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "m1", "title": "Old Movie", "status": "deleted"}])

    client.get("/api/library/candidates")
    assert os.path.exists(app_module._candidates_cache_path())

    client.post("/api/library/plan", json={"jf_ids": ["m1"]})
    exec_resp = client.post("/api/library/execute", json={"jf_ids": ["m1"]})
    assert exec_resp.status_code == 200

    assert not os.path.exists(app_module._candidates_cache_path())

    # A subsequent candidates fetch must re-scan, not resurrect stale data.
    calls_before = _CountingJellyfin.calls
    resp = client.get("/api/library/candidates")
    assert resp.get_json()["cached"] is False
    assert _CountingJellyfin.calls > calls_before
