"""
Live bookmaker odds fetcher using The Odds API (api.the-odds-api.com).

Fetches h2h decimal odds for a named player from European bookmakers,
removes the overround, and returns a consensus implied probability.
Not used for backtesting — live/upcoming matches only.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Global rate limiter — serialises all Odds API HTTP calls across threads so
# concurrent MatchWorkers don't burst past the per-second frequency limit.
_ODDS_LOCK = threading.Lock()
_ODDS_LAST_CALL: float = 0.0
_MIN_ODDS_INTERVAL = 1.5  # seconds between successive API requests

_BASE_URL = "https://api.the-odds-api.com/v4"
_TIMEOUT = 10

# Ordered scan list used when no sport_key is provided.
_TENNIS_SPORT_KEYS: list[str] = [
    # ATP slams & masters
    "tennis_atp_aus_open_singles",
    "tennis_atp_french_open",
    "tennis_atp_wimbledon",
    "tennis_atp_us_open",
    "tennis_atp_indian_wells",
    "tennis_atp_miami_open",
    "tennis_atp_madrid_open",
    "tennis_atp_italian_open",
    "tennis_atp_canadian_open",
    "tennis_atp_cincinnati_open",
    "tennis_atp_shanghai_masters",
    "tennis_atp_paris_masters",
    "tennis_atp_monte_carlo_masters",
    "tennis_atp_barcelona_open",
    "tennis_atp_dubai",
    "tennis_atp_qatar_open",
    "tennis_atp_china_open",
    "tennis_atp_munich",
    # WTA slams & tournaments
    "tennis_wta_aus_open_singles",
    "tennis_wta_french_open",
    "tennis_wta_wimbledon",
    "tennis_wta_us_open",
    "tennis_wta_indian_wells",
    "tennis_wta_miami_open",
    "tennis_wta_madrid_open",
    "tennis_wta_italian_open",
    "tennis_wta_canadian_open",
    "tennis_wta_cincinnati_open",
    "tennis_wta_charleston_open",
    "tennis_wta_dubai",
    "tennis_wta_qatar_open",
    "tennis_wta_china_open",
    "tennis_wta_stuttgart_open",
    "tennis_wta_wuhan_open",
]

_LOW_CREDITS_THRESHOLD = 50


def _get_api_key() -> str:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise ValueError(
            "ODDS_API_KEY environment variable is not set. "
            "Add it to your .env file at the project root or export it in your shell."
        )
    return key


def _fetch_odds_for_sport(
    sport_key: str, api_key: str, bookmakers: list[str] | None = None
) -> tuple[list[dict], dict[str, str]]:
    """
    Call the Odds API for one sport key.

    Returns (events, response_headers).
    When bookmakers is provided the request targets those books directly
    (no regions param); otherwise defaults to regions=eu.
    """
    if bookmakers:
        bk_param = ",".join(bookmakers)
        url = (
            f"{_BASE_URL}/sports/{sport_key}/odds"
            f"?apiKey={api_key}&bookmakers={bk_param}&markets=h2h&oddsFormat=decimal"
        )
    else:
        url = (
            f"{_BASE_URL}/sports/{sport_key}/odds"
            f"?apiKey={api_key}&regions=eu&markets=h2h&oddsFormat=decimal"
        )
    global _ODDS_LAST_CALL
    with _ODDS_LOCK:
        wait = _MIN_ODDS_INTERVAL - (time.monotonic() - _ODDS_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _ODDS_LAST_CALL = time.monotonic()
        resp = requests.get(url, timeout=_TIMEOUT)
    headers = dict(resp.headers)

    if resp.status_code != 200:
        logger.error(
            "Odds API returned %s for sport_key=%s: %s",
            resp.status_code,
            sport_key,
            resp.text[:300],
        )
        return [], headers

    remaining = headers.get("x-requests-remaining", "?")
    if remaining != "?" and int(remaining) < _LOW_CREDITS_THRESHOLD:
        logger.warning(
            "Odds API credits running low: %s remaining", remaining
        )

    return resp.json(), headers


def _player_in_event(event: dict, player_name: str) -> bool:
    needle = player_name.lower()
    return needle in event.get("home_team", "").lower() or needle in event.get(
        "away_team", ""
    ).lower()


def _compute_consensus(
    event: dict, player_name: str
) -> dict[str, Any] | None:
    """
    Build the consensus bookmaker probability from all h2h bookmakers in an event.
    Returns None if no usable bookmaker data is found.
    """
    needle = player_name.lower()
    home_team: str = event.get("home_team", "")
    away_team: str = event.get("away_team", "")

    # Determine which slot (home/away) the queried player occupies.
    if needle in home_team.lower():
        player_slot = "home"
        opponent_name = away_team
    else:
        player_slot = "away"
        opponent_name = home_team

    raw_bookmaker_data: list[dict] = []
    normalized_probs: list[float] = []
    best_price: float = 0.0

    for bookie in event.get("bookmakers", []):
        bookie_name: str = bookie.get("title", bookie.get("key", "unknown"))
        for market in bookie.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            home_odds: float | None = None
            away_odds: float | None = None
            for o in outcomes:
                name_lower = o.get("name", "").lower()
                price = float(o.get("price", 0))
                if price <= 1.0:
                    continue
                if name_lower in home_team.lower() or home_team.lower() in name_lower:
                    home_odds = price
                elif name_lower in away_team.lower() or away_team.lower() in name_lower:
                    away_odds = price

            if home_odds is None or away_odds is None:
                continue

            raw_home = 1.0 / home_odds
            raw_away = 1.0 / away_odds
            total = raw_home + raw_away
            norm_home = raw_home / total
            norm_away = raw_away / total

            player_odds = home_odds if player_slot == "home" else away_odds
            norm_player = norm_home if player_slot == "home" else norm_away

            if player_odds > best_price:
                best_price = player_odds

            normalized_probs.append(norm_player)
            raw_bookmaker_data.append(
                {
                    "bookmaker": bookie_name,
                    "player_odds": player_odds,
                    "opponent_odds": away_odds if player_slot == "home" else home_odds,
                    "normalized_prob": round(norm_player, 6),
                }
            )

    if not normalized_probs:
        return None

    consensus = sum(normalized_probs) / len(normalized_probs)
    return {
        "player_slot": player_slot,
        "opponent_name": opponent_name,
        "bookmaker_implied_prob": round(consensus, 6),
        "num_bookmakers": len(normalized_probs),
        "best_price": round(best_price, 4),
        "raw_bookmaker_data": raw_bookmaker_data,
    }


def get_bookmaker_prob(
    player_name: str,
    sport_key: str | None = None,
    bookmakers: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    Fetch the consensus bookmaker-implied win probability for *player_name*.

    Parameters
    ----------
    player_name : str
        Name of the player to look up (partial, case-insensitive).
    sport_key : str | None
        Specific Odds API sport key (e.g. "tennis_atp_madrid_open").
        If None, all keys in _TENNIS_SPORT_KEYS are scanned in order.
    bookmakers : list[str] | None
        Restrict to specific bookmaker keys (e.g. ["draftkings", "fanduel"]).
        When set, the request uses &bookmakers= instead of &regions=eu, and
        the response is filtered to only those books. Returns None if none
        of the requested bookmakers appear in the event.

    Returns
    -------
    dict | None
        Result dict on success, None if no matching event is found.
    """
    api_key = _get_api_key()

    keys_to_try = [sport_key] if sport_key else _TENNIS_SPORT_KEYS

    last_headers: dict[str, str] = {}

    try:
        for key in keys_to_try:
            events, last_headers = _fetch_odds_for_sport(key, api_key, bookmakers)
            if not events:
                continue

            for event in events:
                if not _player_in_event(event, player_name):
                    continue

                # Filter to requested bookmakers when specified
                if bookmakers:
                    bk_lower = {b.lower() for b in bookmakers}
                    filtered = [
                        bk for bk in event.get("bookmakers", [])
                        if bk.get("key", "").lower() in bk_lower
                    ]
                    if not filtered:
                        logger.warning(
                            "None of the requested bookmakers %s found for %s in %s.",
                            bookmakers, player_name, key,
                        )
                        return None
                    event = {**event, "bookmakers": filtered}

                result = _compute_consensus(event, player_name)
                if result is None:
                    logger.warning(
                        "Found event for %s in %s but no usable bookmaker data.",
                        player_name,
                        key,
                    )
                    continue

                logger.info(
                    "Fetched odds for %s from %s (%d bookmakers, prob=%.4f)",
                    player_name,
                    key,
                    result["num_bookmakers"],
                    result["bookmaker_implied_prob"],
                )

                return {
                    "player_name": player_name,
                    "opponent_name": result["opponent_name"],
                    "bookmaker_implied_prob": result["bookmaker_implied_prob"],
                    "num_bookmakers": result["num_bookmakers"],
                    "best_price": result["best_price"],
                    "sport_key": key,
                    "event_commence_time": event.get("commence_time"),
                    "raw_bookmaker_data": result["raw_bookmaker_data"],
                    "api_credits_used": last_headers.get("x-requests-last"),
                    "api_credits_remaining": last_headers.get("x-requests-remaining"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

        logger.warning(
            "No active event found for player '%s' in any scanned sport key.", player_name
        )
        return None

    except ValueError:
        raise
    except Exception as exc:
        logger.error("Unexpected error fetching odds for '%s': %s", player_name, exc)
        return None


# ---------------------------------------------------------------------------
# Tournament ID → Odds API sport key mapping (TennisAPI1 uniqueTournament IDs)
# ---------------------------------------------------------------------------

TOURNAMENT_MAP: dict[int, str] = {
    2374: "tennis_atp_madrid_open",
    2370: "tennis_atp_italian_open",
    2371: "tennis_atp_french_open",
    2367: "tennis_atp_wimbledon",
    2369: "tennis_atp_us_open",
    2368: "tennis_atp_aus_open_singles",
    2375: "tennis_atp_canadian_open",
    2372: "tennis_atp_cincinnati_open",
    2376: "tennis_atp_shanghai_masters",
    2377: "tennis_atp_paris_masters",
    2373: "tennis_atp_indian_wells",
    2366: "tennis_atp_miami_open",
    2607: "tennis_wta_madrid_open",
    6078: "tennis_wta_italian_open",
    6079: "tennis_wta_french_open",
    6080: "tennis_wta_wimbledon",
    6081: "tennis_wta_us_open",
    6082: "tennis_wta_aus_open_singles",
}


def get_match_odds(
    tennisapi_match: dict,
    bookmakers: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    Alignment bridge between TennisAPI1 match dict and The Odds API.

    tennisapi_match must contain:
      - homeTeam.name  (string)
      - awayTeam.name  (string)
      - tournament.uniqueTournament.id  (int, may be nested or absent)
    """
    # 1. Extract player names — warn and bail if either is missing
    try:
        home_name: str = tennisapi_match["homeTeam"]["name"]
    except (KeyError, TypeError):
        home_name = None  # type: ignore[assignment]
    try:
        away_name: str = tennisapi_match["awayTeam"]["name"]
    except (KeyError, TypeError):
        away_name = None  # type: ignore[assignment]

    if not home_name or not away_name:
        logger.warning(
            "get_match_odds: missing homeTeam or awayTeam name in match dict."
        )
        return None

    # 2. Extract uniqueTournament ID (gracefully handle any nesting issues)
    try:
        tournament_id: int | None = tennisapi_match["tournament"]["uniqueTournament"]["id"]
    except (KeyError, TypeError):
        tournament_id = None

    # 3. Map tournament ID → sport key; fall back to full scan when unknown
    if tournament_id is not None and tournament_id in TOURNAMENT_MAP:
        sport_key: str | None = TOURNAMENT_MAP[tournament_id]
        matched_via_tournament_id = True
    else:
        sport_key = None  # triggers scan fallback in get_bookmaker_prob
        matched_via_tournament_id = False

    # 4 & 5. Fetch consensus probability and attach the tournament-match flag
    result = get_bookmaker_prob(home_name, sport_key, bookmakers)
    if result is None:
        return None

    result["matched_via_tournament_id"] = matched_via_tournament_id
    return result
