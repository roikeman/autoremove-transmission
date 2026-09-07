import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cleanup import buckets

_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")


@dataclass
class Candidate:
    jf_id: str
    kind: str
    title: str
    path: str
    size_bytes: int
    added: datetime
    last_played: datetime
    episodes: int
    watched: int
    progress_pct: float
    owner: str
    owner_id: int
    bucket: str
    flags: list = field(default_factory=list)
    # Per-title viewer breakdown across every Jellyfin user (see
    # classify_viewer below for the exact finished/started/never-opened
    # definitions). Populated by app._merge() while folding in each user's
    # view; defaulted to 0 here only so direct Candidate(...) construction
    # (tests, the cache-miss path) never needs to pass them explicitly.
    users_finished: int = 0
    users_started: int = 0
    users_dropped: int = 0

    def to_dict(self):
        return {
            "jf_id": self.jf_id,
            "kind": self.kind,
            "title": self.title,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "added": self.added.isoformat() if self.added else None,
            "last_played": self.last_played.isoformat() if self.last_played else None,
            "episodes": self.episodes,
            "watched": self.watched,
            "progress_pct": self.progress_pct,
            "owner": self.owner,
            "owner_id": self.owner_id,
            "bucket": self.bucket,
            "flags": list(self.flags),
            "users_finished": self.users_finished,
            "users_started": self.users_started,
            "users_dropped": self.users_dropped,
        }


def parse_dt(value):
    """Parse a Jellyfin timestamp. Sub-second precision exceeds datetime's range.

    Always returns a naive datetime (or None): any Z/+HH:MM/-HH:MM offset
    suffix is stripped, not applied, so the result is consistent with every
    other (naive) datetime in this codebase.
    """
    if not value:
        return None
    text = _OFFSET_RE.sub("", str(value))
    if "." in text:
        text = text.split(".")[0]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_stale(added, last_played, now, age_days, idle_days):
    if added is None or added > now - timedelta(days=age_days):
        return False
    if last_played is None:
        return True
    return last_played < now - timedelta(days=idle_days)


def build_owner_index(sonarr_items, radarr_items):
    index = {}
    for item in sonarr_items or []:
        if item.get("path"):
            index[os.path.normpath(item["path"])] = ("sonarr", item["id"])
    for item in radarr_items or []:
        if item.get("path"):
            index[os.path.normpath(item["path"])] = ("radarr", item["id"])
    return index


def match_owner(path, index):
    """Exact match, else the most specific (longest) matching ancestor
    directory. Never a sibling prefix. The result must not depend on the
    index's insertion order when multiple ancestors match (e.g. nested
    library roots)."""
    if not path:
        return (None, None)
    norm = os.path.normpath(path)
    if norm in index:
        return index[norm]
    best_path = None
    best_owner = (None, None)
    for owned_path, owner in index.items():
        if norm.startswith(owned_path + os.sep):
            if best_path is None or len(owned_path) > len(best_path):
                best_path = owned_path
                best_owner = owner
    return best_owner


def _size_of(item):
    for source in item.get("MediaSources") or []:
        if source.get("Size"):
            return int(source["Size"])
    return int(item.get("Size") or 0)


def sonarr_stats_by_id(sonarr_items):
    """Index Sonarr's /api/v3/series response by id, for O(1) lookup of a
    matched series' `statistics` -- no extra API call, this is the same
    response already fetched once per scan to build the owner index."""
    return {item["id"]: item for item in sonarr_items or [] if "id" in item}


def radarr_stats_by_id(radarr_items):
    """Same idea as sonarr_stats_by_id, for Radarr's /api/v3/movie response."""
    return {item["id"]: item for item in radarr_items or [] if "id" in item}


def sonarr_stats(owner, owner_id, sonarr_by_id):
    """Return (episodes, size_bytes) from a matched Sonarr series'
    `statistics` object, or (None, None) when there is no Sonarr owner or
    the owner has no usable statistics. Never raises."""
    if owner != "sonarr" or not sonarr_by_id:
        return None, None
    item = sonarr_by_id.get(owner_id)
    if not item:
        return None, None
    stats = item.get("statistics") or {}
    episodes = stats.get("episodeCount")
    size_bytes = stats.get("sizeOnDisk")
    if isinstance(episodes, bool) or not isinstance(episodes, int):
        episodes = None
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)):
        size_bytes = None
    return episodes, size_bytes


def radarr_size(owner, owner_id, radarr_by_id):
    """Return sizeOnDisk for a matched Radarr movie, or None when
    unavailable. Radarr carries sizeOnDisk both at the top level and inside
    `statistics`; either is accepted."""
    if owner != "radarr" or not radarr_by_id:
        return None
    item = radarr_by_id.get(owner_id)
    if not item:
        return None
    size_bytes = item.get("sizeOnDisk")
    if size_bytes is None:
        size_bytes = (item.get("statistics") or {}).get("sizeOnDisk")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)):
        return None
    return size_bytes


def path_has_hardlink(path):
    """True if any file under `path` has extra hardlinks (st_nlink > 1) --
    i.e. a Transmission torrent still holds a copy of the data, so deleting
    the library copy would free less than its listed size.

    Kept cheap and safe: only files under this one candidate's own path are
    stat'd (never the whole library), and any missing/unreadable path or
    stat error is treated as False rather than raised -- a title whose files
    vanished mid-scan must not abort the scan.
    """
    if not path:
        return False
    try:
        if os.path.isfile(path):
            return os.stat(path).st_nlink > 1
        if not os.path.isdir(path):
            return False
        for dirpath, _dirs, filenames in os.walk(path):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    if os.stat(fp).st_nlink > 1:
                        return True
                except OSError:
                    continue
        return False
    except OSError:
        return False


def guard_unavailable_bucket(bucket, flags):
    """Never let an unavailable episode/watched count land in a pre-ticked
    bucket.

    Shared between from_series (the first user folded into a title) and
    app._merge (every subsequent user merged in): the bucket gets
    recomputed on every merge as watched/last_played change, so the guard
    has to be re-applied every time too, not just once up front -- with up
    to 91 users, a title merged 91 times must stay just as protected after
    the 91st merge as it was after the 1st.

    The two unavailable flags get different treatment:
      episode-data-unavailable: neither Sonarr nor Jellyfin produced a real
          episode count, so there is no reliable signal at all for this
          series -- not even "no play event ever happened" -- so it must
          never be pre-ticked, bucket A (never opened) included. Never
          silently produce "never opened" from missing data.
      watch-count-unavailable: the episode count IS known; only this
          particular user's watched count is missing. Bucket A only
          requires last_played to be genuinely absent, which doesn't
          depend on the watched count being accurate, so it's exempt.
      last-played-unavailable: the per-user played-episode lookup that
          derives a series' last-played date (Jellyfin never populates it on
          the Series item itself) failed or came back unusable this scan,
          AND no play event could be established from any other source
          either. That combination means "never played" cannot be trusted
          -- the missing lookup could be hiding a recent play -- so, same as
          episode-data-unavailable, bucket A gets no exemption here either.
    """
    if bucket not in buckets.PRETICKED:
        return bucket
    if "episode-data-unavailable" in flags or "last-played-unavailable" in flags:
        return buckets.MID_WATCH
    if "watch-count-unavailable" in flags and bucket != buckets.NEVER_OPENED:
        return buckets.MID_WATCH
    return bucket


def classify_viewer(kind, episodes, watched, last_played):
    """Classify ONE user's view of a title for the users_finished /
    users_started / users_dropped breakdown.

    Definitions:
      finished:     that user's watched count reached the full episode
                    count (movie: the `Played` flag is true -- which is
                    exactly what from_movie's watched=1 already encodes).
      started:      a play event exists (there's a last-played date, or a
                    partial watched count) but the title wasn't finished
                    -- series: 0 < watched < episodes; movie: played but
                    not `Played`.
      never opened: no play event at all and watched == 0.
    """
    if kind == "movie":
        if watched >= 1:
            return "finished"
        if last_played is not None:
            return "started"
        return "never_opened"

    if episodes > 0 and watched >= episodes:
        return "finished"
    if watched > 0 or last_played is not None:
        return "started"
    return "never_opened"


def from_series(item, owner_index, episodes=None, watched=None, size_bytes=None,
                 last_played=None):
    """`last_played` (when given) is the series' last-played date derived by
    the caller from per-user episode playback -- Jellyfin populates
    UserData.LastPlayedDate on the Episode item, never on the Series item,
    so the Series payload's own field (read below into
    `series_level_last_played`) is expected to always be None on real
    Jellyfin. It is still read and folded in defensively (whichever of the
    two is later wins) in case some Jellyfin version ever does populate it,
    rather than the caller's value silently overriding a real signal.
    """
    user = item.get("UserData") or {}

    # `episodes` (when given) is the real count from Sonarr's `statistics`
    # -- the source of truth, and free (no extra API call): it's already
    # part of the /api/v3/series response fetched once per scan to build
    # the owner index. Falling back to RecursiveItemCount preserves the
    # (never actually populated by real Jellyfin) fallback used before
    # Sonarr stats existed, and keeps this function testable in isolation.
    episodes_known = True
    if episodes is not None:
        total = episodes
    else:
        total = int(item.get("RecursiveItemCount") or 0)
        episodes_known = total > 0

    # "UnplayedItemCount" absent from the payload is NOT the same as "0
    # unplayed episodes" -- nobody has confirmed Jellyfin always sends this
    # field. When it's missing (and no caller passed an explicit `watched`,
    # e.g. via the per-episode-query fallback), the watched count is
    # UNKNOWN and must not be inferred as "fully watched". Likewise, without
    # a real episode count there is no valid denominator to subtract
    # UnplayedItemCount from, so watched is unknown too.
    watched_unknown = False
    if watched is None:
        if not episodes_known:
            watched_unknown = True
            watched = 0
        elif user.get("UnplayedItemCount") is not None:
            unplayed = int(user.get("UnplayedItemCount") or 0)
            watched = max(total - unplayed, 0)
        else:
            watched_unknown = True
            watched = 0

    added = parse_dt(item.get("DateLastMediaAdded")) or parse_dt(item.get("DateCreated"))
    series_level_last_played = parse_dt(user.get("LastPlayedDate"))
    if series_level_last_played is not None and (
            last_played is None or series_level_last_played > last_played):
        last_played = series_level_last_played
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)
    size = size_bytes if size_bytes is not None else _size_of(item)

    bucket = buckets.classify("series", total, watched, last_played, 0.0)
    flags = buckets.quality_flags(added, last_played, False)
    if not episodes_known:
        flags.append("episode-data-unavailable")
    elif watched_unknown:
        flags.append("watch-count-unavailable")
    bucket = guard_unavailable_bucket(bucket, flags)

    return Candidate(
        jf_id=item.get("Id"),
        kind="series",
        title=item.get("Name") or "",
        path=path,
        size_bytes=size,
        added=added,
        last_played=last_played,
        episodes=total,
        watched=watched,
        progress_pct=0.0,
        owner=owner,
        owner_id=owner_id,
        bucket=bucket,
        flags=flags,
    )


def from_movie(item, owner_index, size_bytes=None):
    user = item.get("UserData") or {}
    watched = 1 if user.get("Played") else 0
    progress = float(user.get("PlayedPercentage") or 0.0)

    added = parse_dt(item.get("DateCreated"))
    last_played = parse_dt(user.get("LastPlayedDate"))
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)
    size = size_bytes if size_bytes is not None else _size_of(item)

    return Candidate(
        jf_id=item.get("Id"),
        kind="movie",
        title=item.get("Name") or "",
        path=path,
        size_bytes=size,
        added=added,
        last_played=last_played,
        episodes=1,
        watched=watched,
        progress_pct=progress,
        owner=owner,
        owner_id=owner_id,
        bucket=buckets.classify("movie", 1, watched, last_played, progress),
        flags=buckets.quality_flags(added, last_played, False),
    )
