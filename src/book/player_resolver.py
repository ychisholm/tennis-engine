"""
Lazy resolver for book.players rows.

Ensures book.players carries a row for any player_id the verification /
promotion pipeline encounters. If a row already exists, returns it
without an API call. Otherwise fetches team metadata from TennisAPI1's
/api/tennis/team/{id} endpoint via TennisFeed.get_team_detail() and
inserts it. On API failure, falls back to (player_id, name_fallback)
with other columns NULL — unless name_fallback is None, in which case
the caller gets a ValueError.

Uses a SAVEPOINT around the INSERT so a single resolve_player call is
atomic with respect to the outer transaction. The caller controls
commit/rollback. INSERT uses ON CONFLICT DO NOTHING so concurrent
inserts (rare but possible — pipeline parallelism, retries) race
cleanly: whoever loses the race ends up re-selecting the winner's row.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timezone
from typing import Any, Optional

import psycopg2.extensions

_log = logging.getLogger(__name__)


_SELECT_PLAYER = """
SELECT player_id, name, dob, hand, country, created_at
FROM book.players
WHERE player_id = %s
"""

_INSERT_PLAYER = """
INSERT INTO book.players (player_id, name, dob, hand, country)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (player_id) DO NOTHING
"""

_PLAYER_COLUMNS = ("player_id", "name", "dob", "hand", "country", "created_at")


def _select_player(conn, player_id: int) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute(_SELECT_PLAYER, (player_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(_PLAYER_COLUMNS, row))


def _normalize_hand(plays: Optional[str]) -> Optional[str]:
    if not plays:
        return None
    s = str(plays).strip().lower()
    if s.startswith("right"):
        return "right"
    if s.startswith("left"):
        return "left"
    return s or None


def _parse_dob(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _extract_metadata(raw: dict) -> Optional[dict]:
    """Map a /api/tennis/team/{id} response onto book.players columns.
    Returns None if the response has no usable name."""
    if not isinstance(raw, dict):
        return None
    team = raw.get("team") if isinstance(raw.get("team"), dict) else raw
    name = team.get("name") or team.get("fullName")
    if not name:
        return None
    country_block = team.get("country") if isinstance(team.get("country"), dict) else {}
    country = country_block.get("alpha3") or country_block.get("alpha2")
    if country:
        country = str(country)[:3]
    info = team.get("playerTeamInfo") if isinstance(team.get("playerTeamInfo"), dict) else {}
    hand = _normalize_hand(info.get("plays"))
    dob = _parse_dob(info.get("birthDateTimestamp"))
    return {"name": str(name), "dob": dob, "hand": hand, "country": country}


def resolve_player(
    conn,
    player_id: int,
    *,
    name_fallback: Optional[str] = None,
    tennis_feed=None,
) -> dict:
    """
    Ensure book.players has a row for player_id and return it as a dict
    with keys (player_id, name, dob, hand, country, created_at).

    Lookup order:
      1. Existing row → returned as-is, no API call.
      2. Missing → tennis_feed.get_team_detail(player_id) is called.
         On success, metadata fields populate the new row.
         On failure or empty response, name_fallback is used (with other
         columns NULL).
      3. If name_fallback is None and the API also failed/empty:
         ValueError is raised.

    The INSERT runs inside a SAVEPOINT. ON CONFLICT (player_id) DO NOTHING
    means a concurrent insert races cleanly — the final SELECT returns
    whichever row landed first.

    Caller controls the outer transaction; this function does not commit.
    """
    existing = _select_player(conn, player_id)
    if existing is not None:
        return existing

    metadata: Optional[dict] = None
    if tennis_feed is None:
        from src.live.tennis_feed import TennisFeed  # local to avoid env reads at import time
        try:
            tennis_feed = TennisFeed()
        except Exception as exc:
            _log.warning(
                "TennisFeed unavailable for player_id=%s: %s", player_id, exc
            )
            tennis_feed = None

    if tennis_feed is not None:
        try:
            raw = tennis_feed.get_team_detail(player_id)
            metadata = _extract_metadata(raw)
        except Exception as exc:
            _log.warning(
                "get_team_detail failed for player_id=%s: %s", player_id, exc
            )

    if metadata is None:
        if name_fallback is None:
            raise ValueError(
                f"player_id={player_id} not in book.players, API fetch "
                f"failed or returned no usable metadata, and no name_fallback "
                f"was provided"
            )
        payload = {
            "name": name_fallback,
            "dob": None,
            "hand": None,
            "country": None,
        }
    else:
        payload = metadata

    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("SAVEPOINT sp_resolve_player")
        try:
            cur.execute(
                _INSERT_PLAYER,
                (
                    player_id,
                    payload["name"],
                    payload["dob"],
                    payload["hand"],
                    payload["country"],
                ),
            )
            cur.execute("RELEASE SAVEPOINT sp_resolve_player")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_resolve_player")
            raise

    row = _select_player(conn, player_id)
    if row is None:
        raise RuntimeError(
            f"player_id={player_id} insert succeeded but no row found on re-select"
        )
    return row


# ---------------------------------------------------------------------------
# Visual check — pulls today's matches' team IDs from the response archive
# and resolves the first few. Run with:
#     .venv/bin/python -m src.book.player_resolver
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import os
    from pathlib import Path

    import psycopg2
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(_ROOT / ".env")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    conn.autocommit = False
    try:
        # Pull 3 distinct (home_team_id, away_team_id) pairs from today's
        # match_details responses in audit.api_response_archive.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (match_id)
                       match_id,
                       (raw_json -> 'event' -> 'homeTeam' ->> 'id')::BIGINT AS home_id,
                       (raw_json -> 'event' -> 'awayTeam' ->> 'id')::BIGINT AS away_id,
                       raw_json -> 'event' -> 'homeTeam' ->> 'name' AS home_name,
                       raw_json -> 'event' -> 'awayTeam' ->> 'name' AS away_name
                FROM audit.api_response_archive
                WHERE endpoint = 'match_details'
                  AND timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                                   AT TIME ZONE 'UTC'
                  AND raw_json -> 'event' -> 'homeTeam' ->> 'id' IS NOT NULL
                ORDER BY match_id, timestamp DESC
                LIMIT 3
                """
            )
            pairs = cur.fetchall()

        if not pairs:
            print("No match_details responses with team IDs found today.")
        else:
            print(f"Resolving {2 * len(pairs)} players from {len(pairs)} matches:")
            for match_id, home_id, away_id, home_name, away_name in pairs:
                print(f"\n  match {match_id}: home={home_id} ({home_name})  "
                      f"away={away_id} ({away_name})")
                for pid, fb in ((home_id, home_name), (away_id, away_name)):
                    try:
                        row = resolve_player(conn, pid, name_fallback=fb)
                        print(f"    player_id={pid}: name={row['name']!r}  "
                              f"dob={row['dob']}  hand={row['hand']}  "
                              f"country={row['country']}")
                    except Exception as exc:
                        print(f"    player_id={pid}: FAILED — {exc}")
            conn.commit()
            print("\nCommitted.")
    finally:
        conn.close()
