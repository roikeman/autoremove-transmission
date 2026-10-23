"""Explainable, configuration-driven cleanup recommendation scores.

The score is deliberately calculated when candidates are served, not when a
library scan is cached.  Changing a weight in Settings therefore updates the
existing table immediately without another multi-minute Jellyfin scan.

Higher scores mean "recommend deleting sooner".  Age and idle windows remain
hard eligibility filters and are intentionally not mixed into this score.
"""

from cleanup import buckets


BUCKET_SCORE_KEYS = {
    buckets.NEVER_OPENED: "score_never_opened",
    buckets.FULLY_WATCHED: "score_fully_watched",
    buckets.NEAR_COMPLETE: "score_near_complete",
    buckets.SAMPLED: "score_sampled",
    buckets.MID_WATCH: "score_mid_watch",
}


def _number(value):
    """Return compact JSON numbers (80 instead of 80.0, 12.5 unchanged)."""
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def recommendation(candidate, cfg):
    """Return the score and an ordered, user-facing contribution breakdown."""
    bucket_key = BUCKET_SCORE_KEYS.get(candidate.bucket, "score_mid_watch")
    base = float(cfg[bucket_key])
    parts = [{
        "key": "watch_state",
        "label": buckets.LABELS.get(candidate.bucket, candidate.bucket),
        "points": _number(base),
    }]
    total = base

    rating = candidate.community_rating
    if rating is not None:
        pivot = float(cfg["score_rating_pivot"])
        weight = float(cfg["score_rating_points_per_point"])
        adjustment = (pivot - float(rating)) * weight
        total += adjustment
        parts.append({
            "key": "audience_rating",
            "label": f"Audience rating {float(rating):g}",
            "points": _number(adjustment),
        })
    else:
        parts.append({
            "key": "audience_rating",
            "label": "No audience rating",
            "points": 0,
        })

    if candidate.is_seeding:
        adjustment = float(cfg["score_seed_adjustment"])
        total += adjustment
        parts.append({
            "key": "seeding",
            "label": "Currently in seed",
            "points": _number(adjustment),
        })

    if candidate.users_favorite > 0:
        adjustment = float(cfg["score_favorite_adjustment"])
        total += adjustment
        parts.append({
            "key": "favorite",
            "label": f"Favorited by {candidate.users_favorite}",
            "points": _number(adjustment),
        })

    return {
        "recommendation_score": _number(total),
        "recommendation_breakdown": parts,
    }
