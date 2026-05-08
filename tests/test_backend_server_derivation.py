"""
Tests for src.live.backend._derive_server / _enrich_detail_points server logic.

These tests cover the corrected per-row server derivation that uses
first_server (read from the live.match_states column) plus completed-set
games / in-set games / tiebreak point counts.

No DB or HTTP — _enrich_detail_points takes plain dict rows, and
_derive_server is pure.
"""
from __future__ import annotations

import pytest

from src.live.backend import _derive_server, _enrich_detail_points


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(
    *,
    polled_at: str = "2026-05-08T12:00:00",
    first_server: str | None = "home",
    home_sets_won: int = 0,
    away_sets_won: int = 0,
    home_set1_games: int = 0, away_set1_games: int = 0,
    home_set2_games: int = 0, away_set2_games: int = 0,
    home_set3_games: int = 0, away_set3_games: int = 0,
    home_current_games: int = 0, away_current_games: int = 0,
    home_current_point: str = "0", away_current_point: str = "0",
    status: str = "inprogress",
) -> dict:
    return {
        "match_id": 1,
        "player_a": "A", "player_b": "B",
        "polled_at": polled_at,
        "status": status,
        "first_server": first_server,
        "home_sets_won": home_sets_won, "away_sets_won": away_sets_won,
        "home_set1_games": home_set1_games, "away_set1_games": away_set1_games,
        "home_set2_games": home_set2_games, "away_set2_games": away_set2_games,
        "home_set3_games": home_set3_games, "away_set3_games": away_set3_games,
        "home_current_games": home_current_games,
        "away_current_games": away_current_games,
        "home_current_point": home_current_point,
        "away_current_point": away_current_point,
        "point_winner": None, "winner_code": None,
        "tournament_name": "T", "category": "atp",
    }


# ---------------------------------------------------------------------------
# Regular alternation within set 1 (first_server = home)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hg,ag,expected_server", [
    (0, 0, "home"),  # game 1
    (1, 0, "away"),  # game 2 (home won g1)
    (1, 1, "home"),  # game 3
    (2, 1, "away"),  # game 4
    (3, 1, "home"),  # game 5
    (3, 2, "away"),  # game 6
])
def test_set1_alternation_home_first(hg, ag, expected_server):
    assert _derive_server(
        first_server="home",
        home_set1_games=0, away_set1_games=0,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=hg, away_current_games=ag,
        home_current_point="0", away_current_point="0",
        set_num=1,
    ) == expected_server


@pytest.mark.parametrize("hg,ag,expected_server", [
    (0, 0, "away"),
    (1, 0, "home"),
    (1, 1, "away"),
    (2, 1, "home"),
])
def test_set1_alternation_away_first(hg, ag, expected_server):
    assert _derive_server(
        first_server="away",
        home_set1_games=0, away_set1_games=0,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=hg, away_current_games=ag,
        home_current_point="0", away_current_point="0",
        set_num=1,
    ) == expected_server


# ---------------------------------------------------------------------------
# Alternation across a set boundary
# ---------------------------------------------------------------------------

def test_alternation_across_set1_finished_6_3_first_home():
    """Set 1 ends 6-3 (9 games played, odd). Game 10 = first game of set 2.
    Home served game 1, so 9 games means away serves game 10 — the last
    server of set 1 (home) ALWAYS does NOT serve the first game of set 2
    when the set ended on an odd total."""
    assert _derive_server(
        first_server="home",
        home_set1_games=6, away_set1_games=3,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=0, away_current_games=0,
        home_current_point="0", away_current_point="0",
        set_num=2,
    ) == "away"


def test_alternation_across_set1_finished_6_4_first_home():
    """Set 1 ends 6-4 (10 games, even). Game 11 = first of set 2 → home."""
    assert _derive_server(
        first_server="home",
        home_set1_games=6, away_set1_games=4,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=0, away_current_games=0,
        home_current_point="0", away_current_point="0",
        set_num=2,
    ) == "home"


# ---------------------------------------------------------------------------
# Tiebreak SET (set 1 ends 7-6) — game 13 was the tiebreak; game 14 (= first
# game of set 2) alternates from that.
# ---------------------------------------------------------------------------

def test_alternation_across_tiebreak_set_first_home():
    """Set 1 ended 7-6 → 13 games played. Set 2 game 1 = game 14 overall.
    With first_server=home, game 14 has even number → away (since odd-numbered
    games are home and even are away). Wait — game 14 is even → away serves
    by parity. But the prompt says "tiebreak set ends 7-6 → game 14 is home if
    first=home, because tiebreak is one game = game 13 and game 14 alternates"
    means after game 13 served by 'home' (odd → home with first=home), game 14
    is served by away. Let me reread...

    The prompt's expected behaviour: "set 1 ends 7-6 → game 14 is home if
    first=home". That implies game 13 was served by away (so game 14 alternates
    back to home). With first_server=home and 13 odd, by alternation game 13
    would be home — but the comment in the prompt says game 14 is home.

    Reading carefully: game 13 is the tiebreak; whoever served game 13 (home,
    by odd parity) DID serve the tiebreak's first point, but the *next* game
    (set 2 game 1, overall game 14) alternates from that. game 14 = even →
    away serves. So the prompt's example expects game 14 = away with
    first_server=home? But the prompt literally says "game 14 is home if
    first=home". That can only be true if first_server actually means
    "starts game 1" — let me trace: game 1 home, game 2 away, …, game 13
    home (tiebreak), game 14 = first game of set 2 = away. So the prompt's
    statement "game 14 is home" is inconsistent with simple parity.

    Going with parity (the rule the prompt actually specifies in its
    pseudocode: "regular_server = first_server if current_game_number is odd
    else other"), set 2 game 1 with first=home is AWAY.
    """
    assert _derive_server(
        first_server="home",
        home_set1_games=7, away_set1_games=6,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=0, away_current_games=0,
        home_current_point="0", away_current_point="0",
        set_num=2,
    ) == "away"


# ---------------------------------------------------------------------------
# Tiebreak-internal serving (current set in tiebreak: 6-6)
# ---------------------------------------------------------------------------

# With first_server=home and 12 games played in set 1 (6-6), game 13
# (the tiebreak) starts. Game 13 is odd → home would have served if normal.
# So starter = home, other = away. The first point is served by starter (home).
@pytest.mark.parametrize("hp,ap,expected", [
    ("0", "0", "home"),   # 0 points played → starter (home) is next/just served point 1
    ("1", "0", "home"),   # 1 point: starter served point 1
    ("1", "1", "away"),   # 2 points: away (other) served point 2
    ("1", "2", "away"),   # 3 points: away served point 3
    ("2", "2", "home"),   # 4 points: home (starter) served point 4
    ("3", "2", "home"),   # 5 points: home served point 5
    ("3", "3", "away"),   # 6 points: away served point 6
    ("4", "3", "away"),   # 7 points: away served point 7
    ("4", "4", "home"),   # 8 points: home served point 8
])
def test_tiebreak_internal_serving_home_starter(hp, ap, expected):
    assert _derive_server(
        first_server="home",
        home_set1_games=0, away_set1_games=0,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=6, away_current_games=6,
        home_current_point=hp, away_current_point=ap,
        set_num=1,
    ) == expected


def test_tiebreak_starter_is_away_when_set_starts_with_away_serving():
    """If set 2 begins with away serving and reaches 6-6, away is the tiebreak
    starter. With first_server=home and set 1 ending 6-4 (10 games, even),
    game 11 = home (start of set 2), game 12 = away, …, alternating, the
    tiebreak game (game 23 overall) … this gets complicated. Simpler: build
    a scenario where the *current set's* first game is served by away."""
    # set 1 finished 6-4 → 10 games. Set 2 game 1 = game 11 = home (odd).
    # In set 2 we reach 6-6 → 12 games → games 11..22 played → game 23 is the
    # tiebreak. 23 is odd → home is the regular server → starter = home.
    assert _derive_server(
        first_server="home",
        home_set1_games=6, away_set1_games=4,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=6, away_current_games=6,
        home_current_point="0", away_current_point="0",
        set_num=2,
    ) == "home"

    # set 1 finished 6-3 → 9 games. Set 2 game 1 = game 10 = away. Tiebreak
    # in set 2 = game 22 → even → away serves … game 22 = even → second-server.
    # With first_server=home, even games are away; so tiebreak is game 22 = away.
    assert _derive_server(
        first_server="home",
        home_set1_games=6, away_set1_games=3,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=6, away_current_games=6,
        home_current_point="0", away_current_point="0",
        set_num=2,
    ) == "away"


# ---------------------------------------------------------------------------
# first_server = None → server = None on every row
# ---------------------------------------------------------------------------

def test_first_server_none_yields_none_for_all_points():
    rows = [
        _row(first_server=None, home_current_games=0, away_current_games=0),
        _row(first_server=None, home_current_games=1, away_current_games=0,
             polled_at="2026-05-08T12:01:00"),
        _row(first_server=None, home_current_games=2, away_current_games=1,
             polled_at="2026-05-08T12:02:00"),
    ]
    enriched = _enrich_detail_points(rows)
    assert all(p["server"] is None for p in enriched)


def test_first_server_set_yields_concrete_server():
    rows = [
        _row(first_server="home", home_current_games=0, away_current_games=0),
        _row(first_server="home", home_current_games=1, away_current_games=0,
             polled_at="2026-05-08T12:01:00"),
    ]
    enriched = _enrich_detail_points(rows)
    assert enriched[0]["server"] == "home"
    assert enriched[1]["server"] == "away"


# ---------------------------------------------------------------------------
# Defensive — non-int point fields in tiebreak fall back to regular_server
# ---------------------------------------------------------------------------

def test_tiebreak_with_non_int_point_falls_back_to_regular_server():
    """If somehow an 'A' bubbles up while in a tiebreak (shouldn't happen),
    don't crash — fall back to the pre-tiebreak alternation (regular_server,
    which is the tiebreak's *starter*)."""
    server = _derive_server(
        first_server="home",
        home_set1_games=0, away_set1_games=0,
        home_set2_games=0, away_set2_games=0,
        home_set3_games=0, away_set3_games=0,
        home_current_games=6, away_current_games=6,
        home_current_point="A", away_current_point="0",
        set_num=1,
    )
    assert server == "home"  # regular_server for game 13 with first_server=home


# ---------------------------------------------------------------------------
# _enrich_detail_points — chronological ordering and shape stability
# ---------------------------------------------------------------------------

def test_enrich_returns_one_row_per_input_in_chronological_order():
    rows = [
        # game 4 (home leads 2-1) → server = away
        _row(polled_at="2026-05-08T12:02:00", home_current_games=2, away_current_games=1),
        # game 1 (0-0) → server = home
        _row(polled_at="2026-05-08T12:00:00", home_current_games=0, away_current_games=0),
        # game 2 (home won g1, 1-0) → server = away
        _row(polled_at="2026-05-08T12:01:00", home_current_games=1, away_current_games=0),
    ]
    enriched = _enrich_detail_points(rows)
    assert [p["point_num"] for p in enriched] == [0, 1, 2]
    assert [p["server"] for p in enriched] == ["home", "away", "away"]


def test_enrich_empty_rows_returns_empty():
    assert _enrich_detail_points([]) == []
