from datetime import datetime
import pytest
from cleanup import buckets

T = datetime(2026, 1, 1)


@pytest.mark.parametrize("episodes,watched,last_played,expected", [
    (10, 0, None, "A"),     # never opened
    (10, 10, T,   "B"),     # fully watched
    (43, 42, T,   "C1"),    # 97% — near complete
    (10, 8,  T,   "C1"),    # exactly 80% — boundary, inclusive
    (10, 0,  T,   "C2"),    # sampled, never finished an episode
    (10, 7,  T,   "C3"),    # 70% — mid-watch
    (198, 1, T,   "C3"),    # abandoned immediately
])
def test_series_buckets(episodes, watched, last_played, expected):
    assert buckets.classify("series", episodes, watched, last_played, 0.0) == expected


def test_series_80_percent_boundary_is_inclusive():
    assert buckets.classify("series", 10, 8, T, 0.0) == "C1"
    assert buckets.classify("series", 100, 79, T, 0.0) == "C3"
    assert buckets.classify("series", 100, 80, T, 0.0) == "C1"


def test_watched_exceeding_episodes_is_fully_watched():
    assert buckets.classify("series", 8, 9, T, 0.0) == "B"


def test_zero_episode_series_never_divides_by_zero():
    # Pins the safety property: episodes == 0 must never raise (no
    # division by zero), whether or not there was a play event.
    assert buckets.classify("series", 0, 0, None, 0.0) == "A"
    buckets.classify("series", 0, 0, T, 0.0)  # must not raise


def test_zero_episode_series_with_play_event_is_not_preticked():
    # Pins the business rule: episodes == 0 is a Jellyfin metadata anomaly
    # (not yet scanned, or mid-refresh), not evidence the series is
    # disposable. A play event must route it away from any pre-ticked
    # delete-by-default bucket.
    result = buckets.classify("series", 0, 0, T, 0.0)
    assert result not in buckets.PRETICKED


@pytest.mark.parametrize("watched,last_played,progress,expected", [
    (0, None, 0.0,  "A"),    # never opened
    (1, T,    100.0,"B"),    # watched
    (0, T,    0.0,  "C2"),   # opened, no resume point stored
    (0, T,    64.0, "C3"),   # genuinely mid-watch
])
def test_movie_buckets(watched, last_played, progress, expected):
    assert buckets.classify("movie", 1, watched, last_played, progress) == expected


def test_movie_sampled_max_progress_boundary_is_strict():
    assert buckets.classify("movie", 1, 0, T, 4.9) == "C2"
    assert buckets.classify("movie", 1, 0, T, 5.0) == "C3"
    assert buckets.classify("movie", 1, 0, T, 5.1) == "C3"


def test_preticked_excludes_only_c3():
    assert buckets.PRETICKED == {"A", "B", "C1", "C2"}
    assert "C3" not in buckets.PRETICKED


def test_every_bucket_has_a_label():
    for key in ("A", "B", "C1", "C2", "C3"):
        assert buckets.LABELS[key]


def test_added_date_unreliable_flag():
    added = datetime(2025, 12, 29)
    played = datetime(2025, 11, 21)
    assert "added-date-unreliable" in buckets.quality_flags(added, played, False)


def test_no_flag_when_dates_are_ordered():
    added = datetime(2025, 1, 1)
    played = datetime(2025, 6, 1)
    assert buckets.quality_flags(added, played, False) == []


def test_no_flag_when_never_played():
    assert buckets.quality_flags(datetime(2025, 1, 1), None, False) == []


def test_hardlink_flag():
    assert "frees-less-than-listed" in buckets.quality_flags(T, None, True)
