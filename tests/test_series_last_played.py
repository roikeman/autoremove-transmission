"""Coverage for deriving a series' last-played date from per-user episode
playback.

Jellyfin never populates UserData.LastPlayedDate on a Series item -- only
on the Episode item -- so before this change `from_series` always saw
`last_played = None` for every series. Consequences fixed here:

1. The idle half of the staleness rule was inert for series: is_stale()
   returned True on age alone, so a series someone finished last week was
   still a deletion candidate (test_series_played_recently_by_any_user_is_
   not_stale below).
2. Bucket C2 ("sampled, dropped": watched == 0 AND a play event exists) was
   unreachable for series (test_series_watched_zero_with_play_event_
   classifies_c2).
3. Series that were actually finished/started sat in a PRE-TICKED bucket
   displayed as "never played".

The fix (app._user_series_last_played / app._scan) issues one extra call
per user -- JellyfinClient.played_episodes() -- not one call per (user,
series) pair; test_played_episodes_called_once_per_user_not_per_series is
the regression guard for that.

Safety rule: when the per-user played-episode lookup fails or comes back
unusable, and no other source establishes a real last-played date, the
series must not be silently treated as "never played" -- see
test_series_last_played_lookup_failure_is_not_treated_as_never_played.
"""
from datetime import datetime, timedelta

from cleanup import buckets
from cleanup.candidates import Candidate

OLD_DATE = "2000-01-01T00:00:00.0000000Z"
PAST_IDLE_DATE = "2026-01-01T00:00:00.0000000Z"  # outside the default 90-day idle window


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
    """Fake JellyfinClient with a played_episodes() this file can make
    return canned data or raise, per user -- so tests can simulate both the
    "real playback found" and "lookup failed/unusable" paths without a live
    Jellyfin server."""

    def __init__(self, base_url, api_key):
        pass

    users_list = [{"Id": "u1"}]
    series_by_user = {}
    movie_by_user = {}
    played_by_user = {}
    fail_played_for = frozenset()
    played_calls = []

    @classmethod
    def reset(cls, series_items=None, movie_items=None, users_list=None,
              series_by_user=None, movie_by_user=None, played_by_user=None,
              fail_played_for=None):
        cls.users_list = users_list or [{"Id": "u1"}]
        if series_by_user is not None:
            cls.series_by_user = series_by_user
        else:
            cls.series_by_user = {u["Id"]: list(series_items or []) for u in cls.users_list}
        if movie_by_user is not None:
            cls.movie_by_user = movie_by_user
        else:
            cls.movie_by_user = {u["Id"]: list(movie_items or []) for u in cls.users_list}
        cls.played_by_user = played_by_user or {}
        cls.fail_played_for = frozenset(fail_played_for or ())
        cls.played_calls = []

    def users(self):
        return list(self.users_list)

    def items(self, user_id, item_type):
        if item_type == "Series":
            return list(self.series_by_user.get(user_id, []))
        if item_type == "Movie":
            return list(self.movie_by_user.get(user_id, []))
        return []

    def played_episodes(self, user_id, limit=2000):
        type(self).played_calls.append(user_id)
        if user_id in type(self).fail_played_for:
            raise RuntimeError("simulated Jellyfin failure")
        return list(self.played_by_user.get(user_id, []))


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


def _played(series_id, date_str):
    return {"Id": f"ep-{series_id}-{date_str}", "SeriesId": series_id,
            "UserData": {"LastPlayedDate": date_str}}


# --- last_played derived from episode playback, max across users -----------

def test_last_played_derived_from_episode_playback_max_across_users(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Show"
    item_u1 = _series_item("s1", "Show", path, unplayed=0)
    item_u2 = _series_item("s1", "Show", path, unplayed=0)
    _FakeJellyfin.reset(
        users_list=[{"Id": "u1"}, {"Id": "u2"}],
        series_by_user={"u1": [item_u1], "u2": [item_u2]},
        played_by_user={
            "u1": [_played("s1", "2020-01-01T00:00:00.0000000Z")],
            "u2": [_played("s1", "2020-06-01T00:00:00.0000000Z")],
        },
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=10, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    assert result[0].last_played == datetime(2020, 6, 1)


# --- consequence 1: idle rule must no longer be inert for series -----------

def test_series_played_recently_by_any_user_is_not_stale(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/RecentShow"
    item = _series_item("recent1", "Recent Show", path, unplayed=5)
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    _FakeJellyfin.reset(
        users_list=[{"Id": "u1"}],
        series_by_user={"u1": [item]},
        played_by_user={"u1": [_played("recent1", recent)]},
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=10, size_on_disk=1)])

    # idle_days=90 (default); a play 5 days ago must make this NOT stale, so
    # it must not appear as a deletion candidate at all -- before the fix,
    # last_played stayed None here and is_stale() returned True on age alone.
    result = app_module._scan(_base_cfg())
    assert result == []


# --- consequence 2: bucket C2 (sampled, dropped) reachable again -----------

def test_series_watched_zero_with_play_event_classifies_c2(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Dropped"
    item = _series_item("dropped1", "Dropped Show", path, unplayed=10)  # 10-10=0, known
    _FakeJellyfin.reset(
        users_list=[{"Id": "u1"}],
        series_by_user={"u1": [item]},
        played_by_user={"u1": [_played("dropped1", PAST_IDLE_DATE)]},
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=10, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.watched == 0
    assert c.last_played == datetime(2026, 1, 1)
    assert c.bucket == buckets.SAMPLED


# --- safety rule: an unresolvable lookup must never masquerade as "never
#     played" -----------------------------------------------------------

def test_series_last_played_lookup_failure_is_not_treated_as_never_played(monkeypatch):
    app_module = _patch(monkeypatch)
    path = "/share/series/Unknown"
    item = _series_item("unknown1", "Unknown Show", path, unplayed=10)  # watched == 0, known
    _FakeJellyfin.reset(
        users_list=[{"Id": "u1"}],
        series_by_user={"u1": [item]},
        played_by_user={},
        fail_played_for={"u1"},
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=10, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.watched == 0
    assert c.last_played is None
    # Would otherwise classify as NEVER_OPENED (bucket A) and be pre-ticked --
    # exactly the bug being fixed. The lookup failure must keep it out of
    # every pre-ticked bucket and carry a visible flag instead.
    assert c.bucket != buckets.NEVER_OPENED
    assert c.bucket not in buckets.PRETICKED
    assert "last-played-unavailable" in c.flags


def test_series_last_played_unavailable_shape_missing_series_id_falls_back(monkeypatch):
    """SeriesId absent from the response entirely (older/other Jellyfin
    version not honouring the Fields param) must be treated the same as a
    failed call, not as "no plays found"."""
    app_module = _patch(monkeypatch)
    path = "/share/series/Shapeless"
    item = _series_item("shapeless1", "Shapeless Show", path, unplayed=10)
    _FakeJellyfin.reset(
        users_list=[{"Id": "u1"}],
        series_by_user={"u1": [item]},
        played_by_user={"u1": [{"Id": "ep1", "UserData": {"LastPlayedDate": PAST_IDLE_DATE}}]},
    )
    _FakeArr.reset(sonarr_items=[_sonarr_series(1, path, episode_count=10, size_on_disk=1)])

    result = app_module._scan(_base_cfg())
    assert len(result) == 1
    c = result[0]
    assert c.last_played is None
    assert c.bucket not in buckets.PRETICKED
    assert "last-played-unavailable" in c.flags


# --- performance regression guard: one call per user, not per series ------

def test_played_episodes_called_once_per_user_not_per_series(monkeypatch):
    app_module = _patch(monkeypatch)
    users_list = [{"Id": f"u{i}"} for i in range(6)]
    series_items = [_series_item(f"s{i}", f"Show {i}", f"/share/series/Show{i}", unplayed=1)
                    for i in range(5)]
    series_by_user = {u["Id"]: list(series_items) for u in users_list}
    _FakeJellyfin.reset(series_by_user=series_by_user, users_list=users_list)
    _FakeArr.reset(sonarr_items=[
        _sonarr_series(i, f"/share/series/Show{i}", episode_count=10, size_on_disk=1)
        for i in range(5)
    ])

    app_module._scan(_base_cfg())
    assert len(_FakeJellyfin.played_calls) == len(users_list)
    assert sorted(_FakeJellyfin.played_calls) == sorted(u["Id"] for u in users_list)


# --- cache round-trip for the new flag -------------------------------------

def _series_candidate(**over):
    base = dict(
        jf_id="s1", kind="series", title="Show", path="/share/series/Show",
        size_bytes=10, added=None, last_played=None, episodes=10, watched=0,
        progress_pct=0.0, owner=None, owner_id=None, bucket="C3", flags=[],
    )
    base.update(over)
    return Candidate(**base)


def test_cache_round_trip_preserves_last_played_unavailable_flag():
    import app as app_module
    d = _series_candidate(flags=["last-played-unavailable"]).to_dict()
    restored = app_module._candidate_from_cached_dict(d)
    assert "last-played-unavailable" in restored.flags


def test_cache_missing_flags_key_entirely_does_not_raise():
    """An older cache written before this flag existed simply has no
    "flags" key at all -- must default to empty, not raise."""
    import app as app_module
    d = _series_candidate().to_dict()
    del d["flags"]
    restored = app_module._candidate_from_cached_dict(d)  # must not raise
    assert restored.flags == []
