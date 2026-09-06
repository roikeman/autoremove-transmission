"""Pure classification of cleanup candidates. No I/O belongs in this module."""

NEVER_OPENED = "A"
FULLY_WATCHED = "B"
NEAR_COMPLETE = "C1"
SAMPLED = "C2"
MID_WATCH = "C3"

PRETICKED = {NEVER_OPENED, FULLY_WATCHED, NEAR_COMPLETE, SAMPLED}

LABELS = {
    NEVER_OPENED:  "Never opened",
    FULLY_WATCHED: "Fully watched",
    NEAR_COMPLETE: "Near complete",
    SAMPLED:       "Sampled, dropped",
    MID_WATCH:     "Mid-watch",
}

NEAR_COMPLETE_RATIO = 0.8

# Jellyfin stores no resume point for the first few minutes of playback, so a
# movie with a play date but no stored position was opened and abandoned early.
MOVIE_SAMPLED_MAX_PROGRESS = 5.0


def classify(kind, episodes, watched, last_played, progress_pct):
    """Return the bucket identifier for one candidate.

    kind:         'series' or 'movie'
    episodes:     total episodes (1 for a movie)
    watched:      episodes played (0 or 1 for a movie)
    last_played:  datetime or None
    progress_pct: movie resume position, 0-100
    """
    if last_played is None and watched <= 0:
        return NEVER_OPENED

    if kind == "movie":
        if watched >= 1:
            return FULLY_WATCHED
        if progress_pct < MOVIE_SAMPLED_MAX_PROGRESS:
            return SAMPLED
        return MID_WATCH

    if episodes > 0 and watched >= episodes:
        return FULLY_WATCHED
    if watched <= 0:
        return SAMPLED
    if episodes > 0 and (watched / episodes) >= NEAR_COMPLETE_RATIO:
        return NEAR_COMPLETE
    return MID_WATCH


def quality_flags(added, last_played, has_hardlink):
    """Data-quality warnings surfaced in the UI."""
    flags = []
    if last_played is not None and added is not None and last_played < added:
        flags.append("added-date-unreliable")
    if has_hardlink:
        flags.append("frees-less-than-listed")
    return flags
