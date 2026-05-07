from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import requests
from dotenv import load_dotenv

if TYPE_CHECKING:
    from src.live.api_logger import ApiLogger

load_dotenv()

_log = logging.getLogger(__name__)

_BASE_URL = "https://tennisapi1.p.rapidapi.com"
_MAX_RETRIES = 3
_RETRY_DELAY = 1.0


class TennisFeed:
    def __init__(
        self,
        api_key: str | None = None,
        api_logger: "ApiLogger | None" = None,
    ) -> None:
        self._api_key = api_key or os.environ["RAPIDAPI_KEY"]
        self._headers = {
            "x-rapidapi-host": "tennisapi1.p.rapidapi.com",
            "x-rapidapi-key": self._api_key,
        }
        if api_logger is not None:
            self._api_logger = api_logger
        elif os.getenv("DATABASE_URL"):
            try:
                from src.live.api_logger import ApiLogger
                self._api_logger = ApiLogger()
            except Exception as exc:
                _log.warning("ApiLogger construction failed: %s", exc)
                self._api_logger = None
        else:
            self._api_logger = None

    def _log_attempt(
        self,
        endpoint: str,
        request_path: str,
        params: dict | None,
        match_id: str | int | None,
        http_status: int | None,
        latency_ms: int | None,
        raw_response: Any,
        error: str | None,
    ) -> None:
        if self._api_logger is None:
            return
        try:
            from src.live.api_logger import summarize_response
            summary = (
                summarize_response(endpoint, raw_response)
                if raw_response is not None
                else None
            )
            self._api_logger.log_call(
                endpoint=endpoint,
                request_path=request_path,
                request_params=params,
                match_id=match_id,
                http_status=http_status,
                latency_ms=latency_ms,
                response_summary=summary,
                raw_response=raw_response,
                error=error,
            )
        except Exception as exc:
            _log.warning("API audit logging failed: %s", exc)

    def _get(
        self,
        path: str,
        *,
        endpoint: str,
        params: dict | None = None,
        match_id: str | int | None = None,
    ) -> Any:
        url = f"{_BASE_URL}{path}"
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(_MAX_RETRIES):
            start = time.monotonic()
            http_status: int | None = None
            latency_ms: int | None = None
            raw_response: Any = None
            error: str | None = None
            try:
                resp = requests.get(url, headers=self._headers, timeout=10)
                latency_ms = int((time.monotonic() - start) * 1000)
                http_status = resp.status_code
                if resp.status_code == 200:
                    raw_response = resp.json()
                    self._log_attempt(
                        endpoint, path, params, match_id,
                        http_status, latency_ms, raw_response, None,
                    )
                    return raw_response
                error = f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                last_exc = ValueError(error)
            except requests.RequestException as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                error = str(exc) or exc.__class__.__name__
                last_exc = exc
            self._log_attempt(
                endpoint, path, params, match_id,
                http_status, latency_ms, raw_response, error,
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY)
        raise last_exc

    def get_live_matches(self) -> list[dict]:
        data = self._get(
            "/api/tennis/events/live",
            endpoint="live_matches",
            params={},
        )
        events = data.get("events", data) if isinstance(data, dict) else data
        if not isinstance(events, list):
            return []
        result = []
        for event in events:
            home = (
                (event.get("homeTeam") or {}).get("name")
                or event.get("homeName")
                or (event.get("home") or {}).get("name", "Unknown")
            )
            away = (
                (event.get("awayTeam") or {}).get("name")
                or event.get("awayName")
                or (event.get("away") or {}).get("name", "Unknown")
            )
            result.append({
                "id": event.get("id"),
                "home_player": home,
                "away_player": away,
                "tournament_id": (
                    (event.get("tournament") or {})
                    .get("uniqueTournament", {})
                    .get("id")
                ),
            })
        return result

    def get_live_matches_raw(self) -> list[dict]:
        """Returns full raw event dicts from /api/tennis/events/live."""
        data = self._get(
            "/api/tennis/events/live",
            endpoint="live_matches",
            params={},
        )
        events = data.get("events", data) if isinstance(data, dict) else data
        return events if isinstance(events, list) else []

    def get_point_by_point(self, match_id: int | str) -> dict:
        return self._get(
            f"/api/tennis/event/{match_id}/point-by-point",
            endpoint="point_by_point",
            params={"match_id": str(match_id)},
            match_id=str(match_id),
        )

    def get_match_detail(self, match_id: int | str) -> dict:
        return self._get(
            f"/api/tennis/event/{match_id}",
            endpoint="match_details",
            params={"match_id": str(match_id)},
            match_id=str(match_id),
        )

    def parse_match_detail(
        self,
        raw: dict,
        match_id: int,
        player_a: str,
        player_b: str,
        tournament_name: str,
        category: str,
    ) -> dict:
        event = raw.get("event", raw)
        home_score = event.get("homeScore", {})
        away_score = event.get("awayScore", {})
        status = event.get("status", {}).get("type", "unknown")
        return {
            "match_id": match_id,
            "player_a": player_a,
            "player_b": player_b,
            "status": status,
            "home_sets": home_score.get("current"),
            "away_sets": away_score.get("current"),
            "home_period1": home_score.get("period1"),
            "away_period1": away_score.get("period1"),
            "home_period2": home_score.get("period2"),
            "away_period2": away_score.get("period2"),
            "home_period3": home_score.get("period3"),
            "away_period3": away_score.get("period3"),
            "home_current_point": home_score.get("point"),
            "away_current_point": away_score.get("point"),
            "winner_code": event.get("winnerCode"),
            "tournament_name": tournament_name,
            "category": category,
        }

    def get_upcoming_matches(self, days_ahead: int = 1) -> list[dict]:
        """Return scheduled ATP/WTA singles matches for today and the next
        ``days_ahead`` days. Matches lacking ``startTimestamp`` are skipped."""
        today = datetime.now(timezone.utc).date()
        results: list[dict] = []
        for offset in range(days_ahead + 1):
            day = today + timedelta(days=offset)
            path = f"/api/tennis/events/{day.day}/{day.month}/{day.year}"
            try:
                data = self._get(
                    path,
                    endpoint="events_by_date",
                    params={
                        "date_day": day.day,
                        "date_month": day.month,
                        "date_year": day.year,
                    },
                )
            except Exception:
                continue
            events = data.get("events", data) if isinstance(data, dict) else data
            if not isinstance(events, list):
                continue
            for event in events:
                if not self._is_qualifying_scheduled(event):
                    continue
                start_ts = event.get("startTimestamp")
                if start_ts is None:
                    continue
                match_id = event.get("id")
                if match_id is None:
                    continue
                home = (event.get("homeTeam") or {}).get("name", "Unknown")
                away = (event.get("awayTeam") or {}).get("name", "Unknown")
                country_a = ((event.get("homeTeam") or {}).get("country") or {}).get("alpha2")
                country_b = ((event.get("awayTeam") or {}).get("country") or {}).get("alpha2")
                tournament = (event.get("tournament") or {}).get("name", "Unknown")
                category_slug = (
                    (event.get("tournament") or {})
                    .get("category", {})
                    .get("slug", "atp")
                )
                results.append({
                    "match_id": int(match_id),
                    "player_a": home,
                    "player_b": away,
                    "country_a": country_a,
                    "country_b": country_b,
                    "tournament": tournament,
                    "scheduled_start_unix": int(start_ts),
                    "tour": category_slug.upper(),
                })
        return results

    @staticmethod
    def _is_qualifying_scheduled(event: dict) -> bool:
        """Same ATP/WTA singles filter as MatchCollector._is_qualifying but
        without the live-status requirement."""
        try:
            category_slug = event["tournament"]["category"]["slug"]
        except (KeyError, TypeError):
            return False
        if category_slug not in ("atp", "wta"):
            return False

        try:
            ef_category = event["eventFilters"]["category"]
        except (KeyError, TypeError):
            ef_category = ""
        if "doubles" in str(ef_category).lower():
            return False

        return True

    # Tennis point scores used in standard (non-tiebreak) games.
    _STD_SCORES = {"0", "15", "30", "40", "A", "AD"}

    @staticmethod
    def _is_tiebreak_score(home_score: str, away_score: str) -> bool:
        """Return True when at least one score is a raw integer not in the
        standard tennis point-score vocabulary — indicates a tiebreak game."""
        return (
            home_score not in TennisFeed._STD_SCORES
            or away_score not in TennisFeed._STD_SCORES
        )

    def translate_to_engine_format(self, raw_response: dict) -> list[dict]:
        """Convert raw TennisAPI1 pointByPoint payload to a flat list of point
        dicts suitable for the engine and the match logger.

        game_num strategy (mirrors derive_points in backfill_today.py)
        ---------------------------------------------------------------
        Within each set, local_game_num starts at 1 and increments whenever the
        current point's score is "15–0" or "0–15" — unambiguously the first
        point of a new game.  (The first point of each set is exempt so we don't
        double-count game 1.)  Tiebreak games are always labelled game 13.

        This approach is more reliable than server-change detection because the
        API sometimes delivers mis-segmented games where consecutive games share
        the same ``game`` number or a server field is stale.
        """
        sets = raw_response.get("pointByPoint", [])
        sets_sorted = sorted(sets, key=lambda s: s.get("set", 0))

        points: list[dict] = []

        for set_data in sets_sorted:
            set_number     = set_data.get("set", 0)
            local_game_num = 1
            first_of_set   = True

            games = sorted(
                set_data.get("games", []), key=lambda g: g.get("game", 0)
            )

            for game_data in games:
                score  = game_data.get("score", {})
                server = "home" if score.get("serving", 1) == 1 else "away"
                game_out = local_game_num

                for point in game_data.get("points", []):
                    # Normalise "A" (alternate advantage notation) to "AD" so
                    # the pipeline always sees a single token for advantage.
                    home_score = str(point.get("homePoint", "0"))
                    away_score = str(point.get("awayPoint", "0"))
                    if home_score == "A":
                        home_score = "AD"
                    if away_score == "A":
                        away_score = "AD"

                    # ── Derive game number ───────────────────────────────────
                    if self._is_tiebreak_score(home_score, away_score):
                        # Tiebreak games are always labelled 13.
                        game_out = 13
                    else:
                        if not first_of_set:
                            # "15-0" or "0-15" is unambiguously the first point
                            # of a new game — increment before assigning.
                            if (home_score == "15" and away_score == "0") or (
                                home_score == "0" and away_score == "15"
                            ):
                                local_game_num += 1
                        first_of_set = False
                        game_out = local_game_num

                    # ── Determine point winner ───────────────────────────────
                    home_type = point.get("homePointType")
                    away_type = point.get("awayPointType")
                    if home_type == 1:
                        point_winner = "home"
                    elif away_type == 1:
                        point_winner = "away"
                    else:
                        # Neither side is marked as winner — point is incomplete
                        # or malformed (e.g. in-progress point). Skip it rather
                        # than defaulting to "home" which corrupts game scores.
                        continue

                    desc = point.get("pointDescription", 0)
                    points.append({
                        "server":           server,
                        "home_point_score": home_score,
                        "away_point_score": away_score,
                        "point_winner":     point_winner,
                        "is_ace":           desc == 1,
                        "is_double_fault":  desc == 2,
                        "game_number":      game_out,
                        "set_number":       set_number,
                    })

        return points
