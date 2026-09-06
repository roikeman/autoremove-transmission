import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cleanup import buckets


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
        }


def parse_dt(value):
    """Parse a Jellyfin timestamp. Sub-second precision exceeds datetime's range."""
    if not value:
        return None
    text = str(value).replace("Z", "").split("+")[0]
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
    """Exact match, else the nearest ancestor directory. Never a sibling prefix."""
    if not path:
        return (None, None)
    norm = os.path.normpath(path)
    if norm in index:
        return index[norm]
    for owned_path, owner in index.items():
        if norm.startswith(owned_path + os.sep):
            return owner
    return (None, None)


def _size_of(item):
    for source in item.get("MediaSources") or []:
        if source.get("Size"):
            return int(source["Size"])
    return int(item.get("Size") or 0)


def from_series(item, owner_index, episodes=None, watched=None):
    user = item.get("UserData") or {}
    total = episodes if episodes is not None else int(item.get("RecursiveItemCount") or 0)
    if watched is None:
        unplayed = int(user.get("UnplayedItemCount") or 0)
        watched = max(total - unplayed, 0)

    added = parse_dt(item.get("DateLastMediaAdded")) or parse_dt(item.get("DateCreated"))
    last_played = parse_dt(user.get("LastPlayedDate"))
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)

    return Candidate(
        jf_id=item.get("Id"),
        kind="series",
        title=item.get("Name") or "",
        path=path,
        size_bytes=_size_of(item),
        added=added,
        last_played=last_played,
        episodes=total,
        watched=watched,
        progress_pct=0.0,
        owner=owner,
        owner_id=owner_id,
        bucket=buckets.classify("series", total, watched, last_played, 0.0),
        flags=buckets.quality_flags(added, last_played, False),
    )


def from_movie(item, owner_index):
    user = item.get("UserData") or {}
    watched = 1 if user.get("Played") else 0
    progress = float(user.get("PlayedPercentage") or 0.0)

    added = parse_dt(item.get("DateCreated"))
    last_played = parse_dt(user.get("LastPlayedDate"))
    path = item.get("Path") or ""
    owner, owner_id = match_owner(path, owner_index)

    return Candidate(
        jf_id=item.get("Id"),
        kind="movie",
        title=item.get("Name") or "",
        path=path,
        size_bytes=_size_of(item),
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
