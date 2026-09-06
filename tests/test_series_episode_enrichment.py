"""Regression coverage for the live-server defect: Jellyfin's Series payload
carries neither RecursiveItemCount nor MediaSources -- episode counts and
file sizes live on the Episode items beneath it. Before the fix, every
series came back with episodes=0/watched=0/size_bytes=0 and, with no play
event, landed in the pre-ticked "never opened" bucket -- including a series
the owner was midway through.

These tests exercise app._scan()/_enrich_series_with_episodes() directly
against a fake JellyfinClient, mirroring the counting-fake pattern already
used in tests/conftest.py for the scan cache.
"""
from cleanup import buckets

OLD_DATE = "2000-01-01T00:00:00.0000000Z"
RECENT_DATE = "2026-08-20T00:00:00.0000000Z"  # well inside the idle/age window


def _series_item(series_id, name, added=OLD_DATE, last_played=None):
    return {
        "Id": series_id,
        "Name": name,
        "Path": f"/share/series/{name}",
        "DateCreated": added,
        "DateLastMediaAdded": added,
        "UserData": {"Played": False, "LastPlayedDate": last_played},
        # Deliberately no RecursiveItemCount, no MediaSources -- matches the
        # real Jellyfin Series payload.
    }


def _episode(ep_id, played, size, date=OLD_DATE):
    return {
        "Id": ep_id,
        "DateCreated": date,
        "MediaSources": [{"Size": size}],
        "UserData": {"Played": played},
    }


class _FakeJellyfin:
    """Fake JellyfinClient: Series/Movie items come from fixed lists, and
    episodes() is driven by per-series-id fixtures the test installs, with
    every call counted so tests can assert exactly when it fires."""

    def __init__(self, base_url, api_key):
        pass

    series_items = []
    movie_items = []
    episodes_by_series = {}
    fail_series_ids = set()
    empty_series_ids = set()
    episode_calls = []  # list of (user_id, series_id) for every call

    @classmethod
    def reset(cls, series_items=None, movie_items=None, episodes_by_series=None,
              fail_series_ids=None, empty_series_ids=None):
        cls.series_items = series_items or []
        cls.movie_items = movie_items or []
        cls.episodes_by_series = episodes_by_series or {}
        cls.fail_series_ids = fail_series_ids or set()
        cls.empty_series_ids = empty_series_ids or set()
        cls.episode_calls = []

    def users(self):
        return [{"Id": "u1"}]

    def items(self, user_id, item_type):
        if item_type == "Series":
            return list(self.series_items)
        if item_type == "Movie":
            return list(self.movie_items)
        return []

    def episodes(self, user_id, series_id):
        type(self).episode_calls.append((user_id, series_id))
        if series_id in type(self).fail_series_ids:
            raise RuntimeError("simulated Jellyfin episode-fetch failure")
        if series_id in type(self).empty_series_ids:
            return []
        return list(type(self).episodes_by_series.get(series_id, []))


def _base_cfg(**overrides):
    cfg = {
        "jellyfin_url": "https://jf",
        "jellyfin_api_key": "key",
        "age_days": 180,
        "idle_days": 90,
    }
    cfg.update(overrides)
    return cfg


def _patch(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "JellyfinClient", _FakeJellyfin)
    return app_module


def test_series_without_recursive_count_gets_real_numbers_from_episodes(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(
        series_items=[_series_item("s1", "Show")],
        episodes_by_series={
            "s1": [
                _episode("e1", True, 1_000_000_000),
                _episode("e2", False, 2_000_000_000),
            ]
        },
    )

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.episodes == 2
    assert c.watched == 1
    assert c.size_bytes == 3_000_000_000


def test_bridgertona_case_8_of_16_watched_is_c3_not_a(monkeypatch):
    app_module = _patch(monkeypatch)
    # No LastPlayedDate on the Series item -- matches the real Jellyfin
    # payload where a mid-watch series can still show no recent play event
    # at the Series level. Pre-fix, with episodes=0/watched=0 this collapsed
    # to "no evidence" and landed in bucket A; the real per-episode watched
    # count must move it to C3 instead.
    episodes = [_episode(f"e{i}", i < 8, 500_000_000) for i in range(16)]
    _FakeJellyfin.reset(
        series_items=[_series_item("bridgerton", "Bridgerton")],
        episodes_by_series={"bridgerton": episodes},
    )

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.episodes == 16
    assert c.watched == 8
    assert c.bucket == "C3"
    assert c.bucket not in buckets.PRETICKED


def test_episode_fetch_failure_is_not_preticked_and_is_flagged(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(
        series_items=[_series_item("broken", "Broken Show")],
        fail_series_ids={"broken"},
    )

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.bucket not in buckets.PRETICKED
    assert "episode-data-unavailable" in c.flags


def test_episode_fetch_empty_response_is_not_preticked_and_is_flagged(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(
        series_items=[_series_item("empty", "Empty Show")],
        empty_series_ids={"empty"},
    )

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.bucket not in buckets.PRETICKED
    assert "episode-data-unavailable" in c.flags


def test_episode_sizes_sum_for_byte_cap(monkeypatch):
    app_module = _patch(monkeypatch)
    episodes = [_episode(f"e{i}", False, 10_000_000_000) for i in range(5)]
    _FakeJellyfin.reset(
        series_items=[_series_item("s1", "Show")],
        episodes_by_series={"s1": episodes},
    )

    result = app_module._scan(_base_cfg())
    assert result[0].size_bytes == 50_000_000_000


def test_episode_fetch_only_issued_for_series_that_pass_date_filter(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(
        series_items=[
            _series_item("stale", "Stale Show", added=OLD_DATE),
            _series_item("fresh", "Fresh Show", added=RECENT_DATE),
        ],
        episodes_by_series={
            "stale": [_episode("e1", True, 1)],
            "fresh": [_episode("e1", True, 1)],
        },
    )

    result = app_module._scan(_base_cfg())

    # Only the stale series survives the date filter and only it should
    # have triggered an episode fetch.
    fetched_series_ids = {series_id for (_, series_id) in _FakeJellyfin.episode_calls}
    assert fetched_series_ids == {"stale"}
    assert len(_FakeJellyfin.episode_calls) == 1
    assert [c.jf_id for c in result] == ["stale"]
