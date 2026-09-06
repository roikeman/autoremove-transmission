import json
import config


def _cfg_file(tmp_path, monkeypatch, data=None):
    path = tmp_path / "config.json"
    if data is not None:
        path.write_text(json.dumps(data))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    return path


def test_defaults_include_new_services(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    cfg = config.load()
    assert cfg["age_days"] == 180
    assert cfg["idle_days"] == 90
    assert cfg["seed_guard"] is True
    assert cfg["jellyfin_url"] == ""
    assert cfg["library_roots"] == ["/share"]


def test_env_overrides_api_key(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "from-file"})
    monkeypatch.setenv("JELLYFIN_API_KEY", "from-env")
    cfg = config.load()
    assert cfg["jellyfin_api_key"] == "from-env"
    assert "jellyfin_api_key" in config.env_locked()


def test_env_locked_empty_without_env(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    assert config.env_locked() == set()


def test_mask_hides_all_secrets(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "abc", "sonarr_api_key": ""})
    masked = config.mask(config.load())
    assert masked["jellyfin_api_key"] == "••••••••"
    assert masked["sonarr_api_key"] == ""
    assert masked["age_days"] == 180


def test_save_preserves_masked_secret(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"jellyfin_api_key": "real-key"})
    config.save({"jellyfin_api_key": "••••••••", "age_days": 30})
    cfg = config.load()
    assert cfg["jellyfin_api_key"] == "real-key"
    assert cfg["age_days"] == 30


def test_save_sets_owner_only_permissions(tmp_path, monkeypatch):
    import os
    path = _cfg_file(tmp_path, monkeypatch)
    config.save({"jellyfin_api_key": "s3cret"})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_save_rejects_unknown_keys(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch)
    saved = config.save({"nonsense": 1, "age_days": 45})
    assert "nonsense" not in saved
    assert saved["age_days"] == 45
