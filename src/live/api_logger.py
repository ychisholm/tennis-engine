"""
Audit logger for every TennisAPI1 HTTP call.

Writes one row to audit.api_call_log per call. For a configured subset of
endpoints, also archives the parsed JSON body in audit.api_response_archive
and back-references it via raw_response_id.

Failures inside log_call are caught, rolled back, and logged at WARNING. The
method returns None on failure so logging never crashes the caller. This
matches the silent-on-error contract used by PollLogger.log.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Optional

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json

_log = logging.getLogger(__name__)

# Endpoints whose parsed JSON body is archived to audit.api_response_archive.
# events_by_date is intentionally excluded: its responses are 2-5 MB each
# (hundreds of events with full nested team/tournament/score objects polled
# every 5 minutes by the dashboard), and they add no diagnostic value for the
# in-match gap-debugging use case the audit log was built for. Call metadata
# still flows to api_call_log for all four endpoints.
_ARCHIVE_ENDPOINTS = frozenset({"live_matches", "match_details"})

_KNOWN_ENDPOINTS = frozenset(
    {"live_matches", "events_by_date", "match_details", "point_by_point"}
)


class ApiLogger:
    def __init__(self, db_url: Optional[str] = None) -> None:
        load_dotenv()
        url = db_url or os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL environment variable is not set.")
        self._conn = psycopg2.connect(url)
        self._lock = threading.Lock()

    def log_call(
        self,
        endpoint: str,
        request_path: str,
        request_params: Optional[dict] = None,
        match_id: Optional[Any] = None,
        http_status: Optional[int] = None,
        latency_ms: Optional[int] = None,
        response_summary: Optional[dict] = None,
        raw_response: Any = None,
        error: Optional[str] = None,
        poll_cycle_id: Optional[uuid.UUID] = None,
    ) -> Optional[int]:
        match_id_str = str(match_id) if match_id is not None else None
        try:
            with self._lock:
                with self._conn.cursor() as cur:
                    raw_response_id: Optional[int] = None
                    if endpoint in _ARCHIVE_ENDPOINTS and raw_response is not None:
                        byte_size = len(
                            json.dumps(raw_response, default=str).encode("utf-8")
                        )
                        cur.execute(
                            """
                            INSERT INTO audit.api_response_archive
                                (endpoint, match_id, raw_json, byte_size)
                            VALUES (%s, %s, %s, %s)
                            RETURNING id
                            """,
                            (endpoint, match_id_str, Json(raw_response), byte_size),
                        )
                        raw_response_id = cur.fetchone()[0]

                    cur.execute(
                        """
                        INSERT INTO audit.api_call_log (
                            endpoint, request_path, request_params, match_id,
                            http_status, latency_ms, response_summary,
                            raw_response_id, error, poll_cycle_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            endpoint,
                            request_path,
                            Json(request_params) if request_params is not None else None,
                            match_id_str,
                            http_status,
                            latency_ms,
                            Json(response_summary) if response_summary is not None else None,
                            raw_response_id,
                            error,
                            str(poll_cycle_id) if poll_cycle_id is not None else None,
                        ),
                    )
                    call_id = cur.fetchone()[0]
                self._conn.commit()
                return int(call_id)
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            _log.warning("ApiLogger.log_call failed: %s", exc)
            return None

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "ApiLogger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Process-wide singleton accessor.
#
# Phase 3.1: every TennisFeed in the same process shares one ApiLogger and
# therefore one PostgreSQL connection. This prevents connection-pool
# exhaustion on managed databases with low max_connections caps.
# ---------------------------------------------------------------------------

_default_logger: "ApiLogger | None" = None
_default_logger_init_attempted: bool = False
_default_logger_lock = threading.Lock()


def get_default_logger() -> "ApiLogger | None":
    """Return the process-wide default ApiLogger, or None.

    Lazy-initialized on first call. Returns None permanently for the life
    of the process if DATABASE_URL is unset or if construction fails.
    Construction is attempted exactly once; subsequent calls return the
    memoized result. To re-attempt after a transient DB outage, restart
    the process.

    Thread-safe via double-checked locking.
    """
    global _default_logger, _default_logger_init_attempted

    if _default_logger_init_attempted:
        return _default_logger

    with _default_logger_lock:
        if _default_logger_init_attempted:
            return _default_logger

        if not os.getenv("DATABASE_URL"):
            _default_logger_init_attempted = True
            return None

        try:
            _default_logger = ApiLogger()
        except Exception as exc:
            _log.warning(
                "Default ApiLogger construction failed (audit logging disabled "
                "for this process until restart): %s",
                exc,
            )
        # Set the flag ONLY after construction has settled. Setting it before
        # would let the outside fast-path return _default_logger=None while a
        # slow ApiLogger() is still mid-construction in another thread.
        _default_logger_init_attempted = True

    return _default_logger


def _reset_default_logger_for_testing() -> None:
    """Reset the singleton state. ONLY for use in tests.

    Closes any existing logger connection and clears the init-attempted
    flag so the next get_default_logger() call re-runs initialization.
    """
    global _default_logger, _default_logger_init_attempted
    with _default_logger_lock:
        if _default_logger is not None:
            try:
                _default_logger.close()
            except Exception:
                pass
        _default_logger = None
        _default_logger_init_attempted = False


# ---------------------------------------------------------------------------
# Response summarizers
#
# Pure functions. Must never raise — a malformed payload from the upstream API
# would otherwise break logging on the very call we're trying to record.
# ---------------------------------------------------------------------------


def _events_list(response: Any) -> list:
    """Normalize either {"events": [...]} or a raw list to a list."""
    if response is None:
        return []
    if isinstance(response, dict):
        events = response.get("events", [])
        return events if isinstance(events, list) else []
    if isinstance(response, list):
        return response
    return []


def _is_atp_wta_singles(event: dict) -> bool:
    """Mirrors scheduler._is_live_qualifying minus the status check."""
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


def _status_type(event: dict) -> Optional[str]:
    try:
        return event["status"]["type"]
    except (KeyError, TypeError):
        return None


def summarize_live_matches(response: Any) -> dict:
    events = _events_list(response)
    inprogress = 0
    qualifying_ids: list[int] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        status = _status_type(ev)
        if status == "inprogress":
            inprogress += 1
            if _is_atp_wta_singles(ev):
                mid = ev.get("id")
                if isinstance(mid, int):
                    qualifying_ids.append(mid)
    return {
        "total_events": len(events),
        "inprogress_count": inprogress,
        "qualifying_count": len(qualifying_ids),
        "qualifying_match_ids": qualifying_ids,
    }


def summarize_events_by_date(response: Any) -> dict:
    events = _events_list(response)
    status_breakdown: dict[str, int] = {}
    qualifying_ids: list[int] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        status = _status_type(ev) or "unknown"
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        if _is_atp_wta_singles(ev):
            mid = ev.get("id")
            if isinstance(mid, int):
                qualifying_ids.append(mid)
    return {
        "total_events": len(events),
        "status_breakdown": status_breakdown,
        "qualifying_count": len(qualifying_ids),
        "qualifying_match_ids": qualifying_ids,
    }


def summarize_match_details(response: Any) -> dict:
    if isinstance(response, dict):
        event = response.get("event", response)
        if not isinstance(event, dict):
            event = {}
    else:
        event = {}
    home = event.get("homeScore") if isinstance(event.get("homeScore"), dict) else {}
    away = event.get("awayScore") if isinstance(event.get("awayScore"), dict) else {}
    status = _status_type(event)
    return {
        "match_id": event.get("id"),
        "status": status,
        "winner_code": event.get("winnerCode"),
        "home_sets_won": home.get("current"),
        "away_sets_won": away.get("current"),
        "home_current_point": home.get("point"),
        "away_current_point": away.get("point"),
        "home_period1": home.get("period1"),
        "away_period1": away.get("period1"),
        "home_period2": home.get("period2"),
        "away_period2": away.get("period2"),
        "home_period3": home.get("period3"),
        "away_period3": away.get("period3"),
        "is_finished": status == "finished",
    }


def summarize_point_by_point(response: Any) -> dict:
    if isinstance(response, dict):
        sets = response.get("pointByPoint", [])
        if not isinstance(sets, list):
            sets = []
    else:
        sets = []
    total_points = 0
    latest_set = 0
    for s in sets:
        if not isinstance(s, dict):
            continue
        sn = s.get("set")
        if isinstance(sn, int) and sn > latest_set:
            latest_set = sn
        for game in s.get("games", []) or []:
            if not isinstance(game, dict):
                continue
            pts = game.get("points", []) or []
            if isinstance(pts, list):
                total_points += len(pts)
    return {
        "total_points": total_points,
        "set_count": len(sets),
        "latest_set": latest_set,
    }


_SUMMARIZERS = {
    "live_matches": summarize_live_matches,
    "events_by_date": summarize_events_by_date,
    "match_details": summarize_match_details,
    "point_by_point": summarize_point_by_point,
}


def summarize_response(endpoint: str, response: Any) -> Optional[dict]:
    if response is None:
        return None
    if endpoint not in _SUMMARIZERS:
        return {"error": "unknown_endpoint", "endpoint": endpoint}
    try:
        return _SUMMARIZERS[endpoint](response)
    except Exception as exc:
        return {"error": str(exc)}
