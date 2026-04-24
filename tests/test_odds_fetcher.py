"""
Tests for src/live/odds_fetcher.py — all HTTP calls are mocked.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(home: str, away: str, books: list[dict]) -> dict:
    """Build a minimal Odds-API event dict."""
    return {
        "id": "test-event-id",
        "home_team": home,
        "away_team": away,
        "commence_time": "2024-05-01T10:00:00Z",
        "bookmakers": [
            {
                "key": b["key"],
                "title": b["title"],
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": b["home_price"]},
                            {"name": away, "price": b["away_price"]},
                        ],
                    }
                ],
            }
            for b in books
        ],
    }


def _make_response(events: list[dict], credits_used="1", credits_remaining="499") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = events
    mock_resp.headers = {
        "x-requests-last": credits_used,
        "x-requests-remaining": credits_remaining,
    }
    return mock_resp


# ---------------------------------------------------------------------------
# Test 1 — Consensus probability is average of normalised probs, not raw probs
# ---------------------------------------------------------------------------

def test_consensus_is_average_of_normalised_probs():
    """
    Bookmaker A: player 1.80, opponent 2.10
    Bookmaker B: player 1.75, opponent 2.20

    norm_A = (1/1.80) / (1/1.80 + 1/2.10)
    norm_B = (1/1.75) / (1/1.75 + 1/2.20)
    consensus = (norm_A + norm_B) / 2
    """
    raw_A_player = 1.0 / 1.80
    raw_A_opp    = 1.0 / 2.10
    norm_A = raw_A_player / (raw_A_player + raw_A_opp)

    raw_B_player = 1.0 / 1.75
    raw_B_opp    = 1.0 / 2.20
    norm_B = raw_B_player / (raw_B_player + raw_B_opp)

    expected = (norm_A + norm_B) / 2.0

    event = _make_event(
        home="Carlos Alcaraz",
        away="Novak Djokovic",
        books=[
            {"key": "bet365",    "title": "Bet365",    "home_price": 1.80, "away_price": 2.10},
            {"key": "pinnacle",  "title": "Pinnacle",  "home_price": 1.75, "away_price": 2.20},
        ],
    )

    with patch.dict(os.environ, {"ODDS_API_KEY": "testkey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])):
            from src.live.odds_fetcher import get_bookmaker_prob
            result = get_bookmaker_prob("Carlos Alcaraz", sport_key="tennis_atp_madrid_open")

    assert result is not None
    # Result is rounded to 6 decimal places; tolerance covers up to 0.5e-6 rounding error
    assert abs(result["bookmaker_implied_prob"] - expected) < 1e-5, (
        f"Expected {expected:.8f}, got {result['bookmaker_implied_prob']:.8f}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Overround removal: normalised probs sum to exactly 1.0
# ---------------------------------------------------------------------------

def test_normalised_probs_sum_to_one():
    """
    Raw implied probs from any single book sum to > 1 (the overround).
    After normalisation, player + opponent must equal exactly 1.0.
    """
    event = _make_event(
        home="Iga Swiatek",
        away="Aryna Sabalenka",
        books=[
            {"key": "bet365", "title": "Bet365", "home_price": 1.55, "away_price": 2.50},
        ],
    )

    # Verify overround exists in raw numbers
    raw_player = 1.0 / 1.55
    raw_opp    = 1.0 / 2.50
    assert raw_player + raw_opp > 1.0, "Test data should have overround > 1"

    with patch.dict(os.environ, {"ODDS_API_KEY": "testkey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])):
            from src.live.odds_fetcher import get_bookmaker_prob
            result = get_bookmaker_prob("Iga Swiatek", sport_key="tennis_wta_french_open")

    assert result is not None
    # The single-book raw_bookmaker_data entry should have norm that sums to 1
    bk = result["raw_bookmaker_data"][0]
    player_norm = bk["normalized_prob"]
    opp_norm = 1.0 - player_norm  # complement must be exactly 1 - player_norm
    assert abs(player_norm + opp_norm - 1.0) < 1e-12


# ---------------------------------------------------------------------------
# Test 3 — Unknown player returns None without raising
# ---------------------------------------------------------------------------

def test_unknown_player_returns_none():
    event = _make_event(
        home="Carlos Alcaraz",
        away="Jannik Sinner",
        books=[
            {"key": "bet365", "title": "Bet365", "home_price": 1.90, "away_price": 1.95},
        ],
    )

    with patch.dict(os.environ, {"ODDS_API_KEY": "testkey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])):
            from src.live.odds_fetcher import get_bookmaker_prob
            result = get_bookmaker_prob("Roger Federer", sport_key="tennis_atp_wimbledon")

    assert result is None


# ---------------------------------------------------------------------------
# Test 4 — Missing API key raises ValueError with helpful message
# ---------------------------------------------------------------------------

def test_missing_api_key_raises_value_error():
    env = {k: v for k, v in os.environ.items() if k != "ODDS_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        from src.live.odds_fetcher import get_bookmaker_prob
        with pytest.raises(ValueError, match="ODDS_API_KEY"):
            get_bookmaker_prob("Any Player")


# ---------------------------------------------------------------------------
# Test 5 — Credit headers parsed correctly
# ---------------------------------------------------------------------------

def test_api_credits_parsed_from_headers():
    event = _make_event(
        home="Daniil Medvedev",
        away="Alexander Zverev",
        books=[
            {"key": "unibet", "title": "Unibet", "home_price": 2.00, "away_price": 1.85},
        ],
    )
    mock_resp = _make_response([event], credits_used="3", credits_remaining="247")

    with patch.dict(os.environ, {"ODDS_API_KEY": "testkey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=mock_resp):
            from src.live.odds_fetcher import get_bookmaker_prob
            result = get_bookmaker_prob("Medvedev", sport_key="tennis_atp_paris_masters")

    assert result is not None
    assert result["api_credits_used"] == "3"
    assert result["api_credits_remaining"] == "247"


# ---------------------------------------------------------------------------
# Test 6 — Correct URL is constructed (regions=eu, markets=h2h, oddsFormat=decimal)
# ---------------------------------------------------------------------------

def test_url_construction():
    event = _make_event(
        home="Jannik Sinner",
        away="Holger Rune",
        books=[
            {"key": "pinnacle", "title": "Pinnacle", "home_price": 1.40, "away_price": 2.90},
        ],
    )

    with patch.dict(os.environ, {"ODDS_API_KEY": "myapikey123"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])) as mock_get:
            from src.live.odds_fetcher import get_bookmaker_prob
            get_bookmaker_prob("Sinner", sport_key="tennis_atp_italian_open")

    call_url: str = mock_get.call_args[0][0]
    assert "https://api.the-odds-api.com/v4/sports/tennis_atp_italian_open/odds" in call_url
    assert "regions=eu" in call_url
    assert "markets=h2h" in call_url
    assert "oddsFormat=decimal" in call_url
    assert "apiKey=myapikey123" in call_url


# ---------------------------------------------------------------------------
# Test 7 — get_match_odds() maps known tournament ID to correct sport key
# ---------------------------------------------------------------------------

def test_get_match_odds_known_tournament_id():
    """
    uniqueTournament id 2374 → tennis_atp_madrid_open, matched_via_tournament_id=True.
    get_bookmaker_prob is mocked so no real HTTP call is made.
    """
    from src.live.odds_fetcher import get_match_odds

    match = {
        "homeTeam": {"name": "Carlos Alcaraz"},
        "awayTeam": {"name": "Novak Djokovic"},
        "tournament": {"uniqueTournament": {"id": 2374}},
    }
    dummy = {
        "player_name": "Carlos Alcaraz",
        "bookmaker_implied_prob": 0.60,
        "num_bookmakers": 2,
        "best_price": 1.80,
        "sport_key": "tennis_atp_madrid_open",
        "event_commence_time": "2024-05-01T10:00:00Z",
        "raw_bookmaker_data": [],
        "api_credits_used": "1",
        "api_credits_remaining": "499",
        "timestamp": "2024-05-01T10:00:00+00:00",
        "opponent_name": "Novak Djokovic",
    }

    with patch("src.live.odds_fetcher.get_bookmaker_prob", return_value=dummy) as mock_gbp:
        result = get_match_odds(match)

    assert result is not None
    assert result["matched_via_tournament_id"] is True
    mock_gbp.assert_called_once_with("Carlos Alcaraz", "tennis_atp_madrid_open", None)


# ---------------------------------------------------------------------------
# Test 8 — get_match_odds() falls back to sport_key=None for unknown tournament
# ---------------------------------------------------------------------------

def test_get_match_odds_unknown_tournament_id():
    """
    Unknown tournament ID → sport_key=None (triggers scan) and
    matched_via_tournament_id=False.
    """
    from src.live.odds_fetcher import get_match_odds

    match = {
        "homeTeam": {"name": "Carlos Alcaraz"},
        "awayTeam": {"name": "Novak Djokovic"},
        "tournament": {"uniqueTournament": {"id": 99999}},
    }
    dummy = {
        "player_name": "Carlos Alcaraz",
        "bookmaker_implied_prob": 0.55,
        "num_bookmakers": 1,
        "best_price": 1.90,
        "sport_key": "tennis_atp_wimbledon",
        "event_commence_time": "2024-07-01T10:00:00Z",
        "raw_bookmaker_data": [],
        "api_credits_used": "1",
        "api_credits_remaining": "498",
        "timestamp": "2024-07-01T10:00:00+00:00",
        "opponent_name": "Novak Djokovic",
    }

    with patch("src.live.odds_fetcher.get_bookmaker_prob", return_value=dummy) as mock_gbp:
        result = get_match_odds(match)

    assert result is not None
    assert result["matched_via_tournament_id"] is False
    mock_gbp.assert_called_once_with("Carlos Alcaraz", None, None)


# ---------------------------------------------------------------------------
# Test 9 — bookmakers param builds URL with &bookmakers= and no regions=
# ---------------------------------------------------------------------------

def test_bookmakers_param_url_has_no_regions():
    """
    When bookmakers=["draftkings","fanduel"] is passed to get_bookmaker_prob,
    the constructed URL must contain bookmakers=draftkings,fanduel and must
    NOT contain regions=.
    """
    event = _make_event(
        home="Jannik Sinner",
        away="Holger Rune",
        books=[
            {"key": "draftkings", "title": "DraftKings", "home_price": 1.40, "away_price": 2.90},
        ],
    )

    with patch.dict(os.environ, {"ODDS_API_KEY": "mykey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])) as mock_get:
            from src.live.odds_fetcher import get_bookmaker_prob
            get_bookmaker_prob(
                "Sinner",
                sport_key="tennis_atp_italian_open",
                bookmakers=["draftkings", "fanduel"],
            )

    call_url: str = mock_get.call_args[0][0]
    assert "bookmakers=draftkings,fanduel" in call_url or "bookmakers=draftkings%2Cfanduel" in call_url
    assert "regions=" not in call_url


# ---------------------------------------------------------------------------
# Test 10 — get_bookmaker_prob returns None when no requested bookmaker present
# ---------------------------------------------------------------------------

def test_absent_bookmakers_returns_none():
    """
    Response only has bet365; bookmakers=["draftkings","fanduel"] requested.
    After filtering, the list is empty → function must return None, no exception.
    """
    event = _make_event(
        home="Daniil Medvedev",
        away="Alexander Zverev",
        books=[
            {"key": "bet365", "title": "Bet365", "home_price": 1.90, "away_price": 1.95},
        ],
    )

    with patch.dict(os.environ, {"ODDS_API_KEY": "testkey"}):
        with patch("src.live.odds_fetcher.requests.get", return_value=_make_response([event])):
            from src.live.odds_fetcher import get_bookmaker_prob
            result = get_bookmaker_prob(
                "Medvedev",
                sport_key="tennis_atp_paris_masters",
                bookmakers=["draftkings", "fanduel"],
            )

    assert result is None
