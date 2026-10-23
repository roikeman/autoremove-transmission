from types import SimpleNamespace

import config
from cleanup import scoring


def _candidate(**overrides):
    values = {
        "bucket": "C2",
        "community_rating": None,
        "is_seeding": False,
        "users_favorite": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sampled_default_score_is_high_deletion_recommendation():
    result = scoring.recommendation(_candidate(), config.DEFAULTS)
    assert result["recommendation_score"] == 80
    assert result["recommendation_breakdown"][0]["key"] == "watch_state"


def test_high_audience_rating_lowers_deletion_recommendation():
    result = scoring.recommendation(
        _candidate(community_rating=9), config.DEFAULTS)
    # (7 pivot - 9 rating) * 5 points = -10
    assert result["recommendation_score"] == 70
    assert result["recommendation_breakdown"][1]["points"] == -10


def test_seed_and_favorite_adjustments_are_applied_and_explained():
    result = scoring.recommendation(
        _candidate(is_seeding=True, users_favorite=2), config.DEFAULTS)
    assert result["recommendation_score"] == -25
    assert [part["key"] for part in result["recommendation_breakdown"]] == [
        "watch_state", "audience_rating", "seeding", "favorite",
    ]


def test_every_arbitrary_weight_can_be_overridden():
    cfg = dict(config.DEFAULTS)
    cfg.update({
        "score_sampled": 12.5,
        "score_seed_adjustment": -3.5,
        "score_favorite_adjustment": -20,
        "score_rating_pivot": 8,
        "score_rating_points_per_point": 2,
    })
    result = scoring.recommendation(_candidate(
        community_rating=6, is_seeding=True, users_favorite=1), cfg)
    assert result["recommendation_score"] == -7

