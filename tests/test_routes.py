import json
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
