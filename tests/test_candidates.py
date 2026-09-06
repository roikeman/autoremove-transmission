import os
from datetime import datetime
import pytest
from cleanup import candidates as C
from cleanup import buckets

NOW = datetime(2026, 9, 6)


def test_parse_dt_handles_jellyfin_iso():
    assert C.parse_dt("2025-08-23T22:44:18.6029234Z") == datetime(2025, 8, 23, 22, 44, 18)


def test_parse_dt_handles_none_and_empty():
    assert C.parse_dt(None) is None
    assert C.parse_dt("") is None


def test_parse_dt_z_suffix_is_naive():
    result = C.parse_dt("2025-08-23T22:44:18Z")
    assert result == datetime(2025, 8, 23, 22, 44, 18)
    assert result.tzinfo is None


def test_parse_dt_positive_offset_is_naive():
    result = C.parse_dt("2025-08-23T22:44:18+03:00")
    assert result == datetime(2025, 8, 23, 22, 44, 18)
    assert result.tzinfo is None


def test_parse_dt_negative_offset_is_naive():
    result = C.parse_dt("2025-08-23T22:44:18-05:00")
    assert result == datetime(2025, 8, 23, 22, 44, 18)
    assert result.tzinfo is None


def test_is_stale_with_negative_offset_parsed_value_does_not_raise():
    added = C.parse_dt("2025-01-01T00:00:00-05:00")
    last_played = C.parse_dt("2025-01-02T00:00:00-05:00")
    # This raised TypeError (naive vs. aware) before the parse_dt fix.
    assert C.is_stale(added, last_played, NOW, 180, 90) is True


def test_stale_requires_both_conditions():
    old = datetime(2025, 1, 1)
    recent = datetime(2026, 8, 1)
    # old enough, idle long enough
    assert C.is_stale(old, datetime(2026, 1, 1), NOW, 180, 90) is True
    # old enough but played recently
    assert C.is_stale(old, recent, NOW, 180, 90) is False
    # never played but added recently
    assert C.is_stale(datetime(2026, 8, 1), None, NOW, 180, 90) is False
    # never played and old
    assert C.is_stale(old, None, NOW, 180, 90) is True


def test_owner_index_maps_paths():
    idx = C.build_owner_index(
        [{"id": 1, "path": "/share/series/Show"}],
        [{"id": 7, "path": "/share/movies/Film"}],
    )
    assert idx["/share/series/Show"] == ("sonarr", 1)
    assert idx["/share/movies/Film"] == ("radarr", 7)


def test_match_owner_exact():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/Show", idx) == ("sonarr", 1)


def test_match_owner_by_prefix():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/Show/Season 1/ep.mkv", idx) == ("sonarr", 1)


def test_match_owner_does_not_match_sibling_prefix():
    idx = {"/share/series/Show": ("sonarr", 1)}
    assert C.match_owner("/share/series/ShowTwo", idx) == (None, None)


def test_match_owner_unowned():
    assert C.match_owner("/share/reality/Thing", {}) == (None, None)


def test_match_owner_picks_most_specific_nested_owner_parent_first():
    # /share/series inserted before /share/series/Show (Sonarr-before-Radarr
    # insertion order). The more specific nested owner must still win.
    idx = {
        "/share/series": ("radarr", 1),
        "/share/series/Show": ("sonarr", 2),
    }
    assert C.match_owner("/share/series/Show/Season 1/ep.mkv", idx) == ("sonarr", 2)


def test_match_owner_picks_most_specific_nested_owner_child_first():
    # Same two owners, reversed insertion order. Result must be identical to
    # the parent-first case above -- proving it no longer depends on dict
    # insertion order.
    idx = {
        "/share/series/Show": ("sonarr", 2),
        "/share/series": ("radarr", 1),
    }
    assert C.match_owner("/share/series/Show/Season 1/ep.mkv", idx) == ("sonarr", 2)


def _series_item(**over):
    item = {
        "Id": "s1",
        "Name": "Test Show",
        "Path": "/share/series/Show",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "DateLastMediaAdded": "2025-02-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None, "UnplayedItemCount": 2},
        "RecursiveItemCount": 10,
    }
    item.update(over)
    return item


def test_from_series_computes_watched_from_unplayed_count():
    c = C.from_series(_series_item(), {})
    assert c.episodes == 10
    assert c.watched == 8
    assert c.bucket == "C1"


def test_from_series_uses_date_last_media_added():
    c = C.from_series(_series_item(), {})
    assert c.added == datetime(2025, 2, 1)


def test_from_series_falls_back_to_date_created():
    item = _series_item()
    del item["DateLastMediaAdded"]
    assert C.from_series(item, {}).added == datetime(2025, 1, 1)


def test_from_series_assigns_owner():
    idx = {"/share/series/Show": ("sonarr", 4)}
    c = C.from_series(_series_item(), idx)
    assert (c.owner, c.owner_id) == ("sonarr", 4)


def test_from_series_unowned_is_none():
    c = C.from_series(_series_item(), {})
    assert c.owner is None


def test_from_series_flags_unreliable_added_date():
    c = C.from_series(_series_item(
        DateLastMediaAdded="2025-12-29T00:00:00.0000000Z",
        UserData={"Played": False, "LastPlayedDate": "2025-11-21T00:00:00.0000000Z",
                  "UnplayedItemCount": 0},
    ), {})
    assert "added-date-unreliable" in c.flags


def test_from_series_missing_unplayed_count_with_play_event_is_not_preticked():
    # Jellyfin omitted UnplayedItemCount entirely (not confirmed to never
    # happen). There IS a play event, so this must not be silently treated
    # as "0 unplayed" -- that would make an unwatched series compute as
    # fully watched and land pre-ticked for deletion.
    item = _series_item(
        UserData={"Played": False,
                   "LastPlayedDate": "2026-01-01T00:00:00.0000000Z"},
    )
    c = C.from_series(item, {})
    assert c.bucket not in buckets.PRETICKED
    assert "watch-count-unavailable" in c.flags


def test_from_series_explicit_zero_unplayed_count_is_still_fully_watched():
    # The companion case: UnplayedItemCount IS present and explicitly 0 --
    # this is a real signal, not an absent field, and must still classify
    # as fully watched (bucket B), not be treated as unknown.
    item = _series_item(
        UserData={"Played": False,
                   "LastPlayedDate": "2026-01-01T00:00:00.0000000Z",
                   "UnplayedItemCount": 0},
    )
    c = C.from_series(item, {})
    assert c.bucket == "B"
    assert "watch-count-unavailable" not in c.flags


def test_from_series_explicit_null_unplayed_count_is_not_preticked():
    # Jellyfin sent the key with an explicit JSON null rather than omitting
    # it. The key-presence check alone would treat this as "present", and
    # int(None or 0) collapses to 0 -- silently reintroducing the "unwatched
    # series looks fully watched" bug through a different door. An explicit
    # null must be treated the same as an absent key: unknown.
    item = _series_item(
        UserData={"Played": False,
                   "LastPlayedDate": "2026-01-01T00:00:00.0000000Z",
                   "UnplayedItemCount": None},
    )
    c = C.from_series(item, {})
    assert c.bucket not in buckets.PRETICKED
    assert "watch-count-unavailable" in c.flags


def test_from_series_explicit_watched_argument_bypasses_unknown_path():
    # The documented per-episode-query fallback: caller already knows the
    # watched count, so it must not be treated as unknown even though
    # UnplayedItemCount is absent from the payload.
    item = _series_item(UserData={"Played": False, "LastPlayedDate": None})
    c = C.from_series(item, {}, watched=10)
    assert c.watched == 10
    assert c.bucket == "B"
    assert "watch-count-unavailable" not in c.flags


def _movie_item(**over):
    item = {
        "Id": "m1",
        "Name": "Test Film",
        "Path": "/share/movies/Film/film.mkv",
        "DateCreated": "2025-03-01T00:00:00.0000000Z",
        "UserData": {"Played": True, "LastPlayedDate": "2026-01-01T00:00:00.0000000Z",
                     "PlayedPercentage": 100.0},
        "MediaSources": [{"Size": 4294967296}],
    }
    item.update(over)
    return item


def test_from_movie_watched():
    c = C.from_movie(_movie_item(), {})
    assert c.kind == "movie"
    assert c.watched == 1
    assert c.bucket == "B"
    assert c.size_bytes == 4294967296


def test_from_movie_sampled_has_no_resume_point():
    c = C.from_movie(_movie_item(
        UserData={"Played": False, "LastPlayedDate": "2026-01-01T00:00:00.0000000Z"},
    ), {})
    assert c.progress_pct == 0.0
    assert c.bucket == "C2"


def test_from_movie_handles_missing_media_sources():
    item = _movie_item()
    del item["MediaSources"]
    assert C.from_movie(item, {}).size_bytes == 0


def test_from_movie_size_bytes_override_from_radarr():
    c = C.from_movie(_movie_item(), {}, size_bytes=999)
    assert c.size_bytes == 999


# --- Sonarr statistics as the source of truth (Change 1) -------------------

def test_from_series_episodes_and_size_from_sonarr_override():
    # No RecursiveItemCount/MediaSources on the item at all -- matches the
    # real Jellyfin Series payload -- so both values must come purely from
    # the explicit Sonarr-derived overrides, not from the Jellyfin item.
    item = {
        "Id": "s1", "Name": "Show", "Path": "/share/series/Show",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None, "UnplayedItemCount": 6},
    }
    c = C.from_series(item, {}, episodes=16, size_bytes=32_000_000_000)
    assert c.episodes == 16
    assert c.size_bytes == 32_000_000_000
    assert c.watched == 10  # 16 - 6 unplayed, using the Sonarr-provided total


def test_from_series_8_of_16_watched_is_c3_not_a():
    item = {
        "Id": "bridgerton", "Name": "Bridgerton", "Path": "/share/series/Bridgerton",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None, "UnplayedItemCount": 8},
    }
    c = C.from_series(item, {}, episodes=16)
    assert c.watched == 8
    assert c.bucket == "C3"
    assert c.bucket not in buckets.PRETICKED


def test_from_series_no_sonarr_episodes_and_no_recursive_count_is_unavailable():
    # No `episodes` override (no Sonarr owner) and no RecursiveItemCount on
    # the Jellyfin item (the real-world case) -- the episode count itself is
    # unknown, which must flag the title and keep it out of any pre-ticked
    # bucket, distinct from (and regardless of) UnplayedItemCount.
    item = {
        "Id": "orphan", "Name": "Orphan Show", "Path": "/share/series/Orphan",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None, "UnplayedItemCount": 3},
    }
    c = C.from_series(item, {})
    assert c.bucket not in buckets.PRETICKED
    assert "episode-data-unavailable" in c.flags


def test_from_series_no_sonarr_owner_falls_back_to_jellyfin_size():
    item = {
        "Id": "orphan2", "Name": "Orphan Show 2", "Path": "/share/series/Orphan2",
        "DateCreated": "2025-01-01T00:00:00.0000000Z",
        "UserData": {"Played": False, "LastPlayedDate": None},
        "MediaSources": [{"Size": 555}],
    }
    c = C.from_series(item, {})
    assert c.size_bytes == 555


def test_sonarr_stats_returns_none_without_matching_owner():
    by_id = C.sonarr_stats_by_id([
        {"id": 1, "path": "/share/series/Show", "statistics": {"episodeCount": 10, "sizeOnDisk": 5}}
    ])
    assert C.sonarr_stats(None, None, by_id) == (None, None)
    assert C.sonarr_stats("radarr", 1, by_id) == (None, None)


def test_sonarr_stats_returns_episode_count_and_size_on_disk():
    by_id = C.sonarr_stats_by_id([
        {"id": 4, "path": "/share/series/Show", "statistics": {"episodeCount": 12, "sizeOnDisk": 777}}
    ])
    assert C.sonarr_stats("sonarr", 4, by_id) == (12, 777)


def test_radarr_size_returns_size_on_disk():
    by_id = C.radarr_stats_by_id([{"id": 9, "path": "/share/movies/Film", "sizeOnDisk": 42}])
    assert C.radarr_size("radarr", 9, by_id) == 42


def test_radarr_size_returns_none_without_matching_owner():
    by_id = C.radarr_stats_by_id([{"id": 9, "path": "/share/movies/Film", "sizeOnDisk": 42}])
    assert C.radarr_size("sonarr", 9, by_id) is None
    assert C.radarr_size("radarr", 1, by_id) is None


# --- guard_unavailable_bucket ------------------------------------------------

def test_guard_unavailable_bucket_downgrades_preticked_bucket():
    result = C.guard_unavailable_bucket(buckets.FULLY_WATCHED, ["episode-data-unavailable"])
    assert result == buckets.MID_WATCH


def test_guard_unavailable_bucket_downgrades_never_opened_when_episodes_unavailable():
    # Unlike watch-count-unavailable, an unknown episode count means there
    # is no reliable evidence at all -- not even "no play event" -- so
    # bucket A gets no exemption here.
    result = C.guard_unavailable_bucket(buckets.NEVER_OPENED, ["episode-data-unavailable"])
    assert result == buckets.MID_WATCH


def test_guard_unavailable_bucket_leaves_never_opened_alone_for_watch_count_only():
    # Episodes ARE known here; only the watched count is missing.
    # NEVER_OPENED is driven by last_played being genuinely absent, which
    # doesn't depend on the watched count being accurate.
    result = C.guard_unavailable_bucket(buckets.NEVER_OPENED, ["watch-count-unavailable"])
    assert result == buckets.NEVER_OPENED


def test_guard_unavailable_bucket_leaves_bucket_alone_without_flag():
    result = C.guard_unavailable_bucket(buckets.FULLY_WATCHED, [])
    assert result == buckets.FULLY_WATCHED


# --- classify_viewer (Change 2) ---------------------------------------------

def test_classify_viewer_series_finished():
    assert C.classify_viewer("series", 16, 16, datetime(2026, 1, 1)) == "finished"
    assert C.classify_viewer("series", 16, 20, datetime(2026, 1, 1)) == "finished"


def test_classify_viewer_series_started():
    assert C.classify_viewer("series", 16, 6, datetime(2026, 1, 1)) == "started"
    # A play event with a watched count still 0 (e.g. episode data was
    # unavailable for this user's pass) counts as started, not never-opened.
    assert C.classify_viewer("series", 16, 0, datetime(2026, 1, 1)) == "started"


def test_classify_viewer_series_never_opened():
    assert C.classify_viewer("series", 16, 0, None) == "never_opened"


def test_classify_viewer_movie_finished():
    assert C.classify_viewer("movie", 1, 1, datetime(2026, 1, 1)) == "finished"


def test_classify_viewer_movie_started():
    assert C.classify_viewer("movie", 1, 0, datetime(2026, 1, 1)) == "started"


def test_classify_viewer_movie_never_opened():
    assert C.classify_viewer("movie", 1, 0, None) == "never_opened"


# --- path_has_hardlink (Change 3) -------------------------------------------

def test_path_has_hardlink_true_for_linked_file(tmp_tree):
    linked = tmp_tree("a.mkv", linked=True)
    assert C.path_has_hardlink(linked) is True


def test_path_has_hardlink_false_for_unlinked_file(tmp_tree):
    plain = tmp_tree("b.mkv", linked=False)
    assert C.path_has_hardlink(plain) is False


def test_path_has_hardlink_true_when_any_file_in_directory_is_linked(tmp_tree):
    plain = tmp_tree("c.mkv", linked=False)
    tmp_tree("d.mkv", linked=True)
    directory = os.path.dirname(plain)
    assert C.path_has_hardlink(directory) is True


def test_path_has_hardlink_false_for_missing_path():
    assert C.path_has_hardlink("/no/such/path/at/all") is False


def test_path_has_hardlink_false_for_empty_path():
    assert C.path_has_hardlink("") is False
