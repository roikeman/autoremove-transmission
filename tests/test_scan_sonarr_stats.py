"""Integration coverage for app._scan(), exercising the three owner-requested
improvements together with the performance fix that has to land alongside
them:

1. A series' `episodes` and `size_bytes` come from Sonarr's `statistics`
   object (episodeCount, sizeOnDisk) on the /api/v3/series response --
   already fetched once per scan to build the owner index -- instead of a
   per-user, per-series Jellyfin episode fetch. Jellyfin's Series payload
   carries neither RecursiveItemCount nor MediaSources; the earlier
   watched=0 symptom was always caused by `episodes` being 0, never by
   UnplayedItemCount being absent (see tests/test_candidates.py for the
   from_series-level proof of that). This file's
   test_no_episode_fetch_issued_during_scan is the regression guard for the
   live-server cost this replaces: 91 users x ~75 stale series was ~6,800
   extra HTTP round trips before this change.
2. Per-title viewer breakdown across every Jellyfin user
   (users_finished/users_started/users_dropped).
3. The frees-less-than-listed hardlink flag actually gets wired up during
   a scan (candidates.py previously always passed has_hardlink=False).
"""
import pytest
from cleanup import buckets
from cleanup.candidates import Candidate

OLD_DATE = "2000-01-01T00:00:00.0000000Z"
RECENT_DATE = "2026-08-20T00:00:00.0000000Z"       # inside the age window
PAST_IDLE_DATE = "2026-01-01T00:00:00.0000000Z"     # outside the idle window


def _series_item(series_id, name, path, added=OLD_DATE, last_played=None, unplayed=None):
    user_data = {"Played": False, "LastPlayedDate": last_played}
    if unplayed is not None:
        user_data["UnplayedItemCount"] = unplayed
    return {
        "Id": series_id,
        "Name": name,
        "Path": path,
        "DateCreated": added,
        "DateLastMediaAdded": added,
        "UserData": user_data,
        # Deliberately no RecursiveItemCount, no MediaSources -- matches the
        # real Jellyfin Series payload.
    }


def _sonarr_series(item_id, path, episode_count, size_on_disk):
    return {
        "id": item_id,
        "path": path,
        "statistics": {
            "episodeCount": episode_count,
            "episodeFileCount": episode_count,
            "sizeOnDisk": size_on_disk,
            "totalEpisodeCount": episode_count,
        },
    }


class _FakeJellyfin:
    """Fake JellyfinClient. episodes() is counted so
    test_no_episode_fetch_issued_during_scan can assert it is NEVER invoked
    during a scan -- the whole point of this change."""

    def __init__(self, base_url, api_key):
        pass

    users_list = [{"Id": "u1"}]
    series_by_user = {}
    movie_by_user = {}
    episode_calls = []

    @classmethod
    def reset(cls, series_items=None, movie_items=None, users_list=None,
              series_by_user=None, movie_by_user=None):
        cls.users_list = users_list or [{"Id": "u1"}]
        if series_by_user is not None:
            cls.series_by_user = series_by_user
        else:
            cls.series_by_user = {u["Id"]: list(series_items or []) for u in cls.users_list}
        if movie_by_user is not None:
            cls.movie_by_user = movie_by_user
        else:
            cls.movie_by_user = {u["Id"]: list(movie_items or []) for u in cls.users_list}
        cls.episode_calls = []

    def users(self):
        return list(self.users_list)

    def items(self, user_id, item_type):
        if item_type == "Series":
            return list(self.series_by_user.get(user_id, []))
        if item_type == "Movie":
            return list(self.movie_by_user.get(user_id, []))
        return []

    def episodes(self, user_id, series_id):
        type(self).episode_calls.append((user_id, series_id))
        return []


class _FakeArr:
    sonarr_items = []
    radarr_items = []

    def __init__(self, base_url, api_key, kind):
        self.kind = kind

    def list_items(self):
        return list(self.sonarr_items if self.kind == "sonarr" else self.radarr_items)

    @classmethod
    def reset(cls, sonarr_items=None, radarr_items=None):
        cls.sonarr_items = sonarr_items or []
        cls.radarr_items = radarr_items or []


def _base_cfg(**overrides):
    cfg = {
        "jellyfin_url": "https://jf",
        "jellyfin_api_key": "key",
        "sonarr_url": "https://sonarr",
        "sonarr_api_key": "key",
        "age_days": 180,
        "idle_days": 90,
    }
    cfg.update(overrides)
    return cfg


def _patch(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "JellyfinClient", _FakeJellyfin)
    monkeypatch.setattr(app_module, "ArrClient", _FakeArr)
    _FakeJellyfin.reset()
    _FakeArr.reset()
    return app_module


# --- Change 1: Sonarr statistics, not a Jellyfin episode fetch -------------

def test_episodes_and_size_come_from_sonarr_statistics_not_jellyfin(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Show"
    _FakeJellyfin.reset(series_items=[_series_item("s1", "Show", path, unplayed=6)])
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=16, size_on_disk=32_000_000_000)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.episodes == 16
    assert c.size_bytes == 32_000_000_000
    assert c.watched == 10  # 16 - 6 unplayed


def test_bridgerton_case_8_of_16_watched_is_c3_not_a(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Bridgerton"
    _FakeJellyfin.reset(series_items=[_series_item("bridgerton", "Bridgerton", path, unplayed=8)])
    _FakeArr.reset(sonarr_items=[_sonarr_series(2, path, episode_count=16, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.episodes == 16
    assert c.watched == 8
    assert c.bucket == "C3"
    assert c.bucket not in buckets.PRETICKED


def test_no_episode_fetch_issued_during_scan(monkeypatch):
    """The performance-regression guard: a scan across several users and
    several stale series must never call JellyfinClient.episodes()."""
    app_module = _patch(monkeypatch)
    users_list = [{"Id": f"u{i}"} for i in range(6)]
    series_items = [_series_item(f"s{i}", f"Show {i}", f"/share/series/Show{i}", unplayed=1)
                    for i in range(5)]
    _FakeJellyfin.reset(series_items=series_items, users_list=users_list)
    _FakeArr.reset(sonarr_items=[
        _sonarr_series(i, f"/share/series/Show{i}", episode_count=10, size_on_disk=1)
        for i in range(5)
    ])

    result = app_module._scan(_base_cfg())
    assert len(result) == 5
    assert _FakeJellyfin.episode_calls == []


def test_no_sonarr_stats_and_no_jellyfin_count_is_unavailable_and_not_preticked(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(series_items=[
        _series_item("orphan", "Orphan Show", "/share/series/Orphan")
    ])
    _FakeArr.reset(sonarr_items=[])  # no matching Sonarr owner at all

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.bucket not in buckets.PRETICKED
    assert "episode-data-unavailable" in c.flags


def test_series_with_no_sonarr_owner_falls_back_to_jellyfin_size(monkeypatch):
    app_module = _patch(monkeypatch)
    item = _series_item("orphan2", "Orphan Show 2", "/share/series/Orphan2")
    item["MediaSources"] = [{"Size": 555}]
    _FakeJellyfin.reset(series_items=[item])
    _FakeArr.reset(sonarr_items=[])

    result = app_module._scan(_base_cfg())
    assert result[0].size_bytes == 555


# --- Change 2: per-title viewer breakdown -----------------------------------

def test_viewer_breakdown_counts_finished_started_dropped(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Show"
    finisher = _series_item("s1", "Show", path, unplayed=0)                       # 16-0=16 -> finished
    starter = _series_item("s1", "Show", path, unplayed=10, last_played=PAST_IDLE_DATE)  # 16-10=6 -> started
    ghost = _series_item("s1", "Show", path)                                      # no UnplayedItemCount, no play -> never opened

    _FakeJellyfin.reset(
        users_list=[{"Id": "finisher"}, {"Id": "starter"}, {"Id": "ghost"}],
        series_by_user={"finisher": [finisher], "starter": [starter], "ghost": [ghost]},
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=16, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.users_finished == 1
    assert c.users_started == 1
    assert c.users_dropped == 1


def test_viewer_breakdown_survives_flags_across_many_merges(monkeypatch):
    """91-user regression guard: the episode-data-unavailable flag (and the
    pre-tick guard it drives) must survive every merge, not just the
    first."""
    app_module = _patch(monkeypatch)
    path = "/share/series/NoOwner"
    users_list = [{"Id": f"u{i}"} for i in range(10)]
    items = [_series_item("noown", "No Owner Show", path, unplayed=i % 3) for i in range(10)]
    _FakeJellyfin.reset(
        users_list=users_list,
        series_by_user={u["Id"]: [it] for u, it in zip(users_list, items)},
    )
    _FakeArr.reset(sonarr_items=[])  # no Sonarr owner -> episodes stay unavailable

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert "episode-data-unavailable" in c.flags
    assert c.bucket not in buckets.PRETICKED


# --- Change 3: hardlink / still-in-Transmission indicator -------------------

def _movie_item(item_id, name, path, size=123):
    return {
        "Id": item_id,
        "Name": name,
        "Path": path,
        "DateCreated": OLD_DATE,
        "UserData": {"Played": False, "LastPlayedDate": None},
        "MediaSources": [{"Size": size}],
    }


def test_hardlink_flag_wired_into_scan_result(tmp_tree, monkeypatch):
    app_module = _patch(monkeypatch)
    linked_file = tmp_tree("movie.mkv", linked=True)  # nlink == 2
    _FakeJellyfin.reset(movie_items=[_movie_item("m1", "Held Movie", linked_file)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    assert "frees-less-than-listed" in result[0].flags


def test_no_hardlink_flag_when_nlink_is_one(tmp_tree, monkeypatch):
    app_module = _patch(monkeypatch)
    plain_file = tmp_tree("movie2.mkv", linked=False)  # nlink == 1
    _FakeJellyfin.reset(movie_items=[_movie_item("m2", "Free Movie", plain_file)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    assert "frees-less-than-listed" not in result[0].flags


def test_missing_path_does_not_raise_and_has_no_hardlink_flag(monkeypatch):
    app_module = _patch(monkeypatch)
    _FakeJellyfin.reset(movie_items=[_movie_item("m3", "Gone Movie", "/no/such/path.mkv")])

    result = app_module._scan(_base_cfg())  # must not raise
    assert len(result) == 1
    assert "frees-less-than-listed" not in result[0].flags


# --- Cache round-trip for the new viewer-breakdown fields -------------------

def _dict_candidate(**over):
    base = dict(
        jf_id="1", kind="movie", title="A", path="/x/a", size_bytes=10,
        added=None, last_played=None, episodes=1, watched=1,
        progress_pct=100.0, owner=None, owner_id=None, bucket="B",
        users_finished=3, users_started=2, users_dropped=1,
    )
    base.update(over)
    return Candidate(**base)


def test_cache_round_trip_preserves_viewer_breakdown_fields():
    import app as app_module
    d = _dict_candidate().to_dict()
    restored = app_module._candidate_from_cached_dict(d)
    assert (restored.users_finished, restored.users_started, restored.users_dropped) == (3, 2, 1)


def test_cache_missing_viewer_breakdown_fields_defaults_to_zero_not_raise():
    import app as app_module
    d = _dict_candidate().to_dict()
    del d["users_finished"]
    del d["users_started"]
    del d["users_dropped"]
    restored = app_module._candidate_from_cached_dict(d)  # must not raise
    assert (restored.users_finished, restored.users_started, restored.users_dropped) == (0, 0, 0)


def test_cache_wrong_typed_viewer_breakdown_field_still_raises():
    import app as app_module
    d = _dict_candidate(users_finished="not-an-int").to_dict()
    with pytest.raises(TypeError):
        app_module._candidate_from_cached_dict(d)
