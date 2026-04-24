"""
Tests for TennisFeed.translate_to_engine_format using hardcoded fixture.
No live API calls are made.
"""

import pytest
from src.live.tennis_feed import TennisFeed

# ---------------------------------------------------------------------------
# Fixture — match 14232981
# Set 1, Game 1: home serving (serving=1), home wins game (scoring=1)
#   Point 0: home wins on an ace           (pointDescription=1)
#   Point 1: away wins on a double fault   (pointDescription=2)
#   Point 2: home wins normally            (pointDescription=0)
#   Point 3: home wins normally            (pointDescription=0)
# Set 2, Game 1: away serving (serving=2), away wins game (scoring=2)
#   Point 0: away wins normally            (pointDescription=0)
# ---------------------------------------------------------------------------

FIXTURE_14232981 = {
    "pointByPoint": [
        {
            "set": 2,
            "games": [
                {
                    "game": 1,
                    "score": {"serving": 2, "scoring": 2},
                    "points": [
                        {
                            "homePoint": "0",
                            "awayPoint": "15",
                            "homePointType": 5,
                            "awayPointType": 1,
                            "pointDescription": 0,
                        },
                    ],
                }
            ],
        },
        {
            "set": 1,
            "games": [
                {
                    "game": 1,
                    "score": {"serving": 1, "scoring": 1},
                    "points": [
                        {
                            "homePoint": "15",
                            "awayPoint": "0",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 1,
                        },
                        {
                            "homePoint": "15",
                            "awayPoint": "15",
                            "homePointType": 5,
                            "awayPointType": 1,
                            "pointDescription": 2,
                        },
                        {
                            "homePoint": "30",
                            "awayPoint": "15",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 0,
                        },
                        {
                            "homePoint": "40",
                            "awayPoint": "15",
                            "homePointType": 1,
                            "awayPointType": 5,
                            "pointDescription": 0,
                        },
                    ],
                }
            ],
        },
    ]
}


@pytest.fixture
def feed():
    # api_key won't be used — no HTTP calls in these tests
    return TennisFeed(api_key="test-key")


@pytest.fixture
def points(feed):
    return feed.translate_to_engine_format(FIXTURE_14232981)


# ---------------------------------------------------------------------------
# Chronological ordering
# ---------------------------------------------------------------------------

def test_set1_comes_before_set2(points):
    set_numbers = [p["set_number"] for p in points]
    assert set_numbers[0] == 1
    assert set_numbers[-1] == 2


def test_total_point_count(points):
    assert len(points) == 5  # 4 in set1/game1 + 1 in set2/game1


# ---------------------------------------------------------------------------
# Set 1, Game 1 — first point
# ---------------------------------------------------------------------------

def test_first_point_server_is_home(points):
    assert points[0]["server"] == "home"


def test_first_point_winner_is_home(points):
    assert points[0]["point_winner"] == "home"


def test_first_point_scores(points):
    assert points[0]["home_point_score"] == "15"
    assert points[0]["away_point_score"] == "0"


def test_first_point_set_and_game_numbers(points):
    assert points[0]["set_number"] == 1
    assert points[0]["game_number"] == 1


# ---------------------------------------------------------------------------
# Ace detection
# ---------------------------------------------------------------------------

def test_ace_on_first_point(points):
    assert points[0]["is_ace"] is True
    assert points[0]["is_double_fault"] is False


def test_no_false_ace_on_normal_point(points):
    assert points[2]["is_ace"] is False


# ---------------------------------------------------------------------------
# Double-fault detection
# ---------------------------------------------------------------------------

def test_double_fault_on_second_point(points):
    assert points[1]["is_double_fault"] is True
    assert points[1]["is_ace"] is False


def test_double_fault_point_winner_is_away(points):
    # Server (home) committed the double fault — away wins the point
    assert points[1]["point_winner"] == "away"


# ---------------------------------------------------------------------------
# Set 2 point
# ---------------------------------------------------------------------------

def test_set2_server_is_away(points):
    set2_point = points[4]
    assert set2_point["server"] == "away"
    assert set2_point["set_number"] == 2
    assert set2_point["point_winner"] == "away"
