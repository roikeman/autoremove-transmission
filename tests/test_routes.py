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


def _seed_cache(app_module, candidates, age_days=180, idle_days=90):
    """Seed the on-disk candidates cache directly, standing in for a
    completed POST /api/library/scan -- /plan and /execute now read only
    from this cache and never scan themselves."""
    from datetime import datetime
    app_module._save_candidates_cache(candidates, age_days, idle_days, datetime.now().isoformat())


def test_execute_rejects_mismatched_selection(client, monkeypatch):
    """A plan for one selection must not authorize executing a different one
    -- e.g. a stale browser tab left open after the library changed."""
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})

    import app as app_module
    from cleanup.candidates import Candidate

    _seed_cache(app_module, [
        Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                  added=None, last_played=None, episodes=1, watched=0,
                  progress_pct=0.0, owner=None, owner_id=None, bucket="A"),
        Candidate(jf_id="2", kind="movie", title="B", path="/x/b", size_bytes=10,
                  added=None, last_played=None, episodes=1, watched=0,
                  progress_pct=0.0, owner=None, owner_id=None, bucket="A"),
    ])

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

    _seed_cache(app_module, [candidate])
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
    _seed_cache(app_module, [candidate])
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


# --- Async scan (feat/async-scan) -------------------------------------------
#
# A 91-user Jellyfin makes a real scan take ~200s (one HTTP round-trip per
# user per item type). GET /api/library/candidates must NEVER trigger one
# and must NEVER block -- scanning only happens via the background-thread
# POST /api/library/scan. These tests use a counting fake JellyfinClient so
# an unexpected scan is caught directly (call count), not inferred from
# timing.

class _CountingJellyfin:
    """Fake JellyfinClient that counts every items() call, so tests can
    assert whether a scan actually ran."""
    calls = 0
    refresh_calls = 0

    def __init__(self, base_url, api_key):
        pass

    def users(self):
        return [{"Id": "u1"}]

    def played_episodes(self, user_id, limit=2000):
        return []

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

    def refresh_library(self):
        _CountingJellyfin.refresh_calls += 1


def _setup_counting_jellyfin(client, monkeypatch):
    import config
    import app as app_module

    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})
    _CountingJellyfin.calls = 0
    _CountingJellyfin.refresh_calls = 0
    monkeypatch.setattr(app_module, "JellyfinClient", _CountingJellyfin)
    return app_module


def _wait_for_scan_to_finish(client, timeout=5.0):
    import time
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = client.get("/api/library/scan/status").get_json()
        if status["state"] in ("done", "error"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"scan did not finish within {timeout}s (last status: {status})")


def test_candidates_never_scans_and_reports_scan_required(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scan_required"] is True
    assert body["candidates"] == []
    assert body["cached"] is False
    # The whole point: no cache yet must never fall back to scanning inline.
    assert _CountingJellyfin.calls == 0


def test_candidates_refresh_param_no_longer_scans(client, monkeypatch):
    """?refresh=1 used to force an inline rescan; that behaviour is gone --
    scanning only ever happens via POST /api/library/scan now."""
    _setup_counting_jellyfin(client, monkeypatch)

    resp = client.get("/api/library/candidates?refresh=1")
    assert resp.status_code == 200
    assert resp.get_json()["scan_required"] is True
    assert _CountingJellyfin.calls == 0


def test_candidates_serves_existing_cache_without_scanning(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    candidate = Candidate(jf_id="m1", kind="movie", title="Old Movie", path="/x/old-movie.mkv",
                           size_bytes=12345, added=None, last_played=None, episodes=1, watched=1,
                           progress_pct=100.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [candidate])

    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scan_required"] is False
    assert body["cached"] is True
    assert len(body["candidates"]) == 1
    assert _CountingJellyfin.calls == 0


def test_candidates_corrupt_cache_file_returns_scan_required(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    os.makedirs(os.path.dirname(app_module._candidates_cache_path()), exist_ok=True)
    with open(app_module._candidates_cache_path(), "w") as f:
        f.write("{not valid json")

    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scan_required"] is True
    assert _CountingJellyfin.calls == 0


def test_candidates_malformed_cache_entry_returns_scan_required(client, monkeypatch):
    """A cache written by an older version -- missing a field on a
    candidate entry, e.g. owner_id -- must degrade to scan_required (200),
    not raise a KeyError that surfaces as a 502, and must never scan."""
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    os.makedirs(os.path.dirname(app_module._candidates_cache_path()), exist_ok=True)
    with open(app_module._candidates_cache_path(), "w") as f:
        json.dump({
            "candidates": [{
                "jf_id": "m1", "kind": "movie", "title": "Old Movie",
                "path": "/x/old-movie.mkv", "size_bytes": 12345,
                "added": "2015-01-01T00:00:00", "last_played": "2015-06-01T00:00:00",
                "episodes": 1, "watched": 1, "progress_pct": 100.0,
                "owner": None,
                # "owner_id" deliberately missing -- simulates an older cache format.
                "bucket": "A", "flags": [],
            }],
            "age_days": 30, "idle_days": 14, "scanned_at": "2026-01-01T00:00:00",
        }, f)

    resp = client.get("/api/library/candidates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scan_required"] is True
    assert _CountingJellyfin.calls == 0


def test_candidates_response_shape_unchanged_plus_scan_required(client, monkeypatch):
    _setup_counting_jellyfin(client, monkeypatch)

    resp = client.get("/api/library/candidates")
    body = resp.get_json()
    for key in ("candidates", "preticked", "labels", "age_days", "idle_days",
                "cached", "scanned_at", "scan_required"):
        assert key in body


def test_scan_endpoint_returns_202_and_populates_cache_in_background(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    resp = client.post("/api/library/scan", json={})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["state"] == "running"
    assert body["started_at"]

    status = _wait_for_scan_to_finish(client)
    assert status["state"] == "done"
    assert status["finished_at"]
    assert _CountingJellyfin.calls > 0

    candidates_resp = client.get("/api/library/candidates")
    body = candidates_resp.get_json()
    assert body["scan_required"] is False
    assert body["cached"] is True
    assert len(body["candidates"]) == 1
    # The GET itself never touched Jellyfin -- all the calls above came from
    # the background scan job, not from serving the cache.
    calls_after_scan = _CountingJellyfin.calls
    client.get("/api/library/candidates")
    assert _CountingJellyfin.calls == calls_after_scan


def test_scan_endpoint_reports_progress_and_heartbeat(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    resp = client.post("/api/library/scan", json={})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["progress"]["phase"] in (
        "sonarr", "jellyfin-played", "jellyfin-items", "hardlinks")
    assert body["heartbeat"]

    status = _wait_for_scan_to_finish(client)
    assert status["state"] == "done"
    assert status["progress"] is not None
    # The final report must reach its own declared total, never stop short.
    assert status["progress"]["done"] == status["progress"]["total"]
    assert status["heartbeat"]


def test_scan_endpoint_returns_409_for_concurrent_second_scan(client, monkeypatch):
    """Holds the scan lock the way a second gunicorn worker process would --
    a wholly separate open file descriptor on the same path -- and confirms
    a second POST /api/library/scan is refused rather than starting a
    second background scan."""
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    lock_path = app_module._scan_lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        resp = client.post("/api/library/scan", json={})
        assert resp.status_code == 409
        assert "state" in resp.get_json()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # Once released, a scan can actually start.
    resp = client.post("/api/library/scan", json={})
    assert resp.status_code == 202
    _wait_for_scan_to_finish(client)


def test_scan_status_idle_by_default(client):
    resp = client.get("/api/library/scan/status")
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "idle"


def test_scan_status_reports_running_while_lock_is_actually_held(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    app_module._save_scan_status({
        "state": "running", "started_at": "2026-01-01T00:00:00", "finished_at": None,
        "progress": {"done": 1, "total": 5, "phase": "jellyfin-played"},
        "error": None, "heartbeat": "2026-01-01T00:00:01",
    })
    lock_path = app_module._scan_lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        resp = client.get("/api/library/scan/status")
        assert resp.get_json()["state"] == "running"
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_scan_status_reconciles_dead_worker_to_error(client, monkeypatch):
    """A status file left saying "running" with NOBODY holding the scan
    lock means the worker that was running it died mid-scan -- this must
    report "error", not "running" forever."""
    app_module = _setup_counting_jellyfin(client, monkeypatch)

    app_module._save_scan_status({
        "state": "running", "started_at": "2026-01-01T00:00:00", "finished_at": None,
        "progress": {"done": 1, "total": 5, "phase": "jellyfin-played"},
        "error": None, "heartbeat": "2026-01-01T00:00:01",
    })

    resp = client.get("/api/library/scan/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["state"] == "error"
    assert body["error"]

    # The reconciled state is persisted, not just returned once.
    resp2 = client.get("/api/library/scan/status")
    assert resp2.get_json()["state"] == "error"


def test_plan_fails_clearly_without_cache(client):
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})

    resp = client.post("/api/library/plan", json={"jf_ids": ["1"]})
    assert resp.status_code == 409


def test_execute_fails_clearly_when_cache_missing(client, monkeypatch):
    """A matching persisted plan exists, but the candidates cache does not
    (e.g. it was never populated) -- execute must fail with a clear 409,
    never kick off its own scan inside this POST."""
    import config
    config.save({"jellyfin_url": "https://jf", "jellyfin_api_key": "secret-key"})
    import app as app_module

    app_module._save_last_plan({"1"})
    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 409


def test_plan_uses_cache_regardless_of_config_defaults(client, monkeypatch):
    """/plan reads whatever is cached, regardless of whether its age/idle
    thresholds match cfg_mod.load()'s current defaults -- and never
    touches Jellyfin itself."""
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    candidate = Candidate(jf_id="m1", kind="movie", title="Old Movie", path="/x/old-movie.mkv",
                           size_bytes=12345, added=None, last_played=None, episodes=1, watched=1,
                           progress_pct=100.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [candidate], age_days=999, idle_days=999)

    import config
    cfg = config.load()
    assert cfg["age_days"] != 999
    assert cfg["idle_days"] != 999

    resp = client.post("/api/library/plan", json={"jf_ids": ["m1"]})
    assert resp.status_code == 200
    assert _CountingJellyfin.calls == 0


def test_plan_and_execute_never_touch_jellyfin_scan(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "m1", "title": "Old Movie", "status": "deleted"}])

    candidate = Candidate(jf_id="m1", kind="movie", title="Old Movie", path="/x/old-movie.mkv",
                           size_bytes=12345, added=None, last_played=None, episodes=1, watched=1,
                           progress_pct=100.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [candidate])

    plan_resp = client.post("/api/library/plan", json={"jf_ids": ["m1"]})
    assert plan_resp.status_code == 200
    assert _CountingJellyfin.calls == 0

    exec_resp = client.post("/api/library/execute", json={"jf_ids": ["m1"]})
    assert exec_resp.status_code == 200
    assert _CountingJellyfin.calls == 0


# --- Post-execute cache update (Change 3) ------------------------------------

def test_execute_updates_cache_removing_deleted_keeping_others(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    c1 = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                   added=None, last_played=None, episodes=1, watched=0,
                   progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    c2 = Candidate(jf_id="2", kind="movie", title="B", path="/x/b", size_bytes=10,
                   added=None, last_played=None, episodes=1, watched=0,
                   progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [c1, c2])
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    client.post("/api/library/plan", json={"jf_ids": ["1"]})
    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200

    # The cache still exists (not wiped wholesale) and now only has "2".
    assert os.path.exists(app_module._candidates_cache_path())
    candidates_resp = client.get("/api/library/candidates")
    remaining_ids = {c["jf_id"] for c in candidates_resp.get_json()["candidates"]}
    assert remaining_ids == {"2"}


def test_execute_keeps_partial_and_failed_titles_in_cache(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    c1 = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                   added=None, last_played=None, episodes=1, watched=0,
                   progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    c2 = Candidate(jf_id="2", kind="movie", title="B", path="/x/b", size_bytes=10,
                   added=None, last_played=None, episodes=1, watched=0,
                   progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [c1, c2])
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [
            {"jf_id": "1", "title": "A", "status": "partial"},
            {"jf_id": "2", "title": "B", "status": "failed"},
        ])

    client.post("/api/library/plan", json={"jf_ids": ["1", "2"]})
    resp = client.post("/api/library/execute", json={"jf_ids": ["1", "2"]})
    assert resp.status_code == 200

    candidates_resp = client.get("/api/library/candidates")
    remaining_ids = {c["jf_id"] for c in candidates_resp.get_json()["candidates"]}
    assert remaining_ids == {"1", "2"}


def test_execute_refreshes_jellyfin_library(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    candidate = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                           added=None, last_played=None, episodes=1, watched=0,
                           progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [candidate])
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    client.post("/api/library/plan", json={"jf_ids": ["1"]})
    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200
    assert _CountingJellyfin.refresh_calls == 1


def test_execute_journals_jellyfin_refresh_failure_without_failing_run(client, monkeypatch):
    app_module = _setup_counting_jellyfin(client, monkeypatch)
    from cleanup.candidates import Candidate

    def boom(self):
        raise RuntimeError("jellyfin unreachable")
    monkeypatch.setattr(_CountingJellyfin, "refresh_library", boom)

    candidate = Candidate(jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
                           added=None, last_played=None, episodes=1, watched=0,
                           progress_pct=0.0, owner=None, owner_id=None, bucket="A")
    _seed_cache(app_module, [candidate])
    monkeypatch.setattr(
        app_module.pipeline, "execute",
        lambda selection, cfg, clients: [{"jf_id": "1", "title": "A", "status": "deleted"}])

    client.post("/api/library/plan", json={"jf_ids": ["1"]})
    resp = client.post("/api/library/execute", json={"jf_ids": ["1"]})
    assert resp.status_code == 200  # the run itself still succeeds

    from cleanup import journal
    entries = journal.read_recent()
    refresh_entries = [e for e in entries if e.get("kind") == "jellyfin_refresh"]
    assert refresh_entries[0]["status"] == "error"
    assert "jellyfin unreachable" in refresh_entries[0]["detail"]
