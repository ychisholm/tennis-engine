#!/usr/bin/env python3
"""
backfill_today.py — Backfill completed ATP/WTA singles matches into
live_raw.tennisapi_points via interactive prompts.

Usage (from project root):
    .venv/bin/python scripts/backtesting/backfill_today.py

The game_num / set_num derivation uses a fixed algorithm:
  - game_num : increments when the first regular-game score is 15-0 or 0-15;
               tiebreaks are always labeled game 13
  - set_num  : trusts the API's outer set-level grouping

Timestamps are synthetic: 45 s per point starting from the match startTimestamp.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

# ── Paths & config ─────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

_BASE_URL  = "https://tennisapi1.p.rapidapi.com"
_API_KEY   = os.environ["RAPIDAPI_KEY"]
_HEADERS   = {
    "x-rapidapi-host": "tennisapi1.p.rapidapi.com",
    "x-rapidapi-key":  _API_KEY,
}

_MAX_RETRIES = 3
_RETRY_DELAY  = 1.0

# ── Tennis scoring constants ────────────────────────────────────────────────────
_STD_SCORES = {"0", "15", "30", "40", "A", "AD"}


# ── DB connection ──────────────────────────────────────────────────────────────

def _get_conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(url)


def _ensure_tables(conn) -> None:
    """Create live_raw schema and tennisapi_points table if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS live_raw")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_raw.tennisapi_points (
                ts               TIMESTAMPTZ,
                match_id         INTEGER,
                player_a         VARCHAR,
                player_b         VARCHAR,
                point_num        INTEGER,
                set_num          INTEGER,
                game_num         INTEGER,
                home_point       VARCHAR,
                away_point       VARCHAR,
                server           VARCHAR,
                point_winner     VARCHAR,
                is_ace           BOOLEAN,
                is_double_fault  BOOLEAN,
                ingestion_source VARCHAR,
                tournament_name  VARCHAR,
                category         VARCHAR
            )
        """)
    conn.commit()


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _get(path: str) -> dict | list:
    url = f"{_BASE_URL}{path}"
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            last_exc = ValueError(f"HTTP {resp.status_code} from {path}: {resp.text[:300]}")
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY)
    raise last_exc


# ── Fixed game / set derivation ────────────────────────────────────────────────

def _is_tb(hp: str, ap: str) -> bool:
    return hp not in _STD_SCORES or ap not in _STD_SCORES


def derive_points(raw_response: dict) -> list[dict]:
    """Flatten the nested pointByPoint payload and derive correct set_num /
    game_num from the raw data.

    set_num strategy
    ----------------
    Trust the API's top-level nested structure (pointByPoint[i].set).

    game_num strategy  (same fixed algorithm as tennis_feed.translate_to_engine_format)
    ------------------
    Within each set, local_game_num starts at 1 and increments whenever the
    current point's score is "15–0" or "0–15".  Tiebreak games are always
    labelled game 13.
    """
    sets_sorted = sorted(
        raw_response.get("pointByPoint", []),
        key=lambda s: s.get("set", 0),
    )

    result: list[dict] = []

    for set_data in sets_sorted:
        set_number   = set_data.get("set", 1)
        local_game   = 1
        first_of_set = True

        games_sorted = sorted(
            set_data.get("games", []),
            key=lambda g: g.get("game", 0),
        )

        for game_data in games_sorted:
            score  = game_data.get("score", {})
            server = "home" if score.get("serving", 1) == 1 else "away"

            for point in game_data.get("points", []):
                hp = str(point.get("homePoint", "0"))
                ap = str(point.get("awayPoint", "0"))
                is_tb_now = _is_tb(hp, ap)

                if is_tb_now:
                    game_out = 13
                else:
                    if not first_of_set:
                        if (hp == "15" and ap == "0") or (hp == "0" and ap == "15"):
                            local_game += 1
                    first_of_set = False
                    game_out = local_game

                home_type = point.get("homePointType")
                away_type = point.get("awayPointType")
                winner = "home" if home_type == 1 else ("away" if away_type == 1 else "home")
                desc = point.get("pointDescription", 0)

                result.append({
                    "set_num":         set_number,
                    "game_num":        game_out,
                    "home_point":      hp,
                    "away_point":      ap,
                    "server":          server,
                    "point_winner":    winner,
                    "is_ace":          desc == 1,
                    "is_double_fault": desc == 2,
                })

    return result


# ── DB helpers ─────────────────────────────────────────────────────────────────

_INSERT_RAW_POINT = """
INSERT INTO live_raw.tennisapi_points (
    ts, match_id, player_a, player_b,
    point_num, set_num, game_num,
    home_point, away_point, server, point_winner, is_ace, is_double_fault,
    ingestion_source
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def get_existing_match_ids(conn) -> dict[int, int]:
    """Return a mapping of match_id → point count in live_raw.tennisapi_points."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT match_id, COUNT(*) FROM live_raw.tennisapi_points GROUP BY match_id"
        )
        rows = cur.fetchall()
    return {r[0]: r[1] for r in rows}


def write_points(
    conn,
    match_id: int,
    player_a: str,
    player_b: str,
    points: list[dict],
    start_ts: int,
) -> None:
    """Write point rows with synthetic timestamps spaced 45 s apart from start_ts."""
    base_dt = datetime.fromtimestamp(start_ts, timezone.utc)
    with conn.cursor() as cur:
        for i, pt in enumerate(points):
            ts = base_dt + timedelta(seconds=i * 45)
            cur.execute(_INSERT_RAW_POINT, [
                ts, match_id, player_a, player_b,
                i, pt["set_num"], pt["game_num"],
                pt["home_point"], pt["away_point"],
                pt["server"], pt["point_winner"],
                pt["is_ace"], pt["is_double_fault"],
                "backfill",
            ])
    conn.commit()


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _sep(char: str = "─", width: int = 70) -> str:
    return char * width


def _games_per_set(points: list[dict]) -> dict[int, int]:
    by_set: dict[int, set[int]] = {}
    for pt in points:
        s = pt["set_num"]
        g = pt["game_num"]
        by_set.setdefault(s, set()).add(g)
    return {s: len(gs) for s, gs in sorted(by_set.items())}


# ── Step A helpers ─────────────────────────────────────────────────────────────

def _prompt_tour() -> str:
    while True:
        val = input("Tour (ATP/WTA): ").strip().upper()
        if val in ("ATP", "WTA"):
            return val
        print("  Please enter ATP or WTA.")


def _prompt_date() -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    raw = input("Date (YYYY-MM-DD, or press Enter for today): ").strip()
    if not raw:
        return today
    try:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    except ValueError:
        print(f"  Invalid format — using today ({today}).")
        return today


def _fetch_events(day: int, month: int, year: int) -> list[dict]:
    try:
        data = _get(f"/api/tennis/events/{day}/{month}/{year}")
        if isinstance(data, dict):
            return data.get("events", [])
    except Exception as exc:
        print(f"  Warning: could not fetch events — {exc}")
    return []


def _build_tournament_list(events: list[dict], tour: str) -> list[tuple[int, str]]:
    """Return deduplicated [(uid, name)] for the tour, excluding doubles."""
    seen: dict[int, str] = {}
    for ev in events:
        tournament = ev.get("tournament", {})
        cat_name = str(tournament.get("category", {}).get("name", "")).upper()
        if tour not in cat_name:
            continue
        ut = tournament.get("uniqueTournament", {})
        ut_name = ut.get("name", "")
        ut_id = ut.get("id")
        if not ut_name or ut_id is None:
            continue
        if "double" in ut_name.lower():
            continue
        seen.setdefault(ut_id, ut_name)
    return sorted(seen.items(), key=lambda t: t[1])


def _prompt_tournament(entries: list[tuple[int, str]]) -> tuple[str, int]:
    """Display a numbered list and return (name, uid) for the user's choice."""
    print("\n  Available tournaments:")
    for i, (uid, name) in enumerate(entries, 1):
        print(f"    {i}.  {name}")
    while True:
        raw = input("Select tournament number: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(entries):
                uid, name = entries[idx]
                return name, uid
        print(f"  Please enter a number between 1 and {len(entries)}.")


def _filter_finished_singles(
    events: list[dict],
    tour: str,
    tournament_uid: int,
    date_min: int,
    date_max: int,
) -> list[dict]:
    """Extract finished singles match dicts from an event list."""
    matches: list[dict] = []
    for ev in events:
        if ev.get("status", {}).get("type", "") != "finished":
            continue

        start_ts = ev.get("startTimestamp", 0)
        if not (date_min <= start_ts < date_max):
            continue

        tournament = ev.get("tournament", {})
        if tournament.get("uniqueTournament", {}).get("id") != tournament_uid:
            continue

        if "double" in str(tournament.get("name", "")).lower():
            continue

        home = (
            (ev.get("homeTeam") or {}).get("name")
            or ev.get("homeName")
            or (ev.get("home") or {}).get("name", "Unknown")
        )
        away = (
            (ev.get("awayTeam") or {}).get("name")
            or ev.get("awayName")
            or (ev.get("away") or {}).get("name", "Unknown")
        )
        hs  = ev.get("homeScore", {}).get("current", "?")
        as_ = ev.get("awayScore", {}).get("current", "?")

        matches.append({
            "id":          ev.get("id"),
            "start_ts":    start_ts,
            "home_player": home,
            "away_player": away,
            "tournament":  tournament.get("name", "Unknown"),
            "category":    tournament.get("category", {}).get("name", "Unknown"),
            "score":       f"{hs}–{as_}",
        })
    return matches


# ── Step A: Discover completed ATP/WTA singles matches ─────────────────────────

def fetch_todays_matches() -> list[dict]:
    """Interactive: prompt for tour/date/tournament, return finished singles."""
    print(_sep("═"))
    print("  STEP A — Discover completed ATP/WTA singles matches")
    print(_sep("═"))
    print()

    tour = _prompt_tour()
    date_str = _prompt_date()

    yf, mf, df = (int(x) for x in date_str.split("-"))
    print(f"\n  Fetching events for {date_str}...")
    events = _fetch_events(df, mf, yf)

    entries = _build_tournament_list(events, tour)
    if not entries:
        print(f"  No {tour} tournaments found for {date_str}.")
        return []

    name, uid = _prompt_tournament(entries)

    date_min = int(datetime(yf, mf, df, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    date_max = int((datetime(yf, mf, df, tzinfo=timezone.utc) + timedelta(days=1)).timestamp())

    matches = _filter_finished_singles(events, tour, uid, date_min, date_max)

    print(f"\n  Tournament : {name}  (id={uid})")
    print(f"  Date       : {date_str}  |  Tour: {tour}")
    print(f"  Finished singles matches: {len(matches)}\n")
    if matches:
        print(f"  {'ID':<12} {'Home':<25} {'Away':<25} {'Score':<8} Tournament")
        print(f"  {'─'*10} {'─'*23} {'─'*23} {'─'*6} {'─'*25}")
        for m in matches:
            print(
                f"  {m['id']:<12} {m['home_player']:<25} {m['away_player']:<25}"
                f" {m['score']:<8} {m['tournament']}"
            )
    print()
    return matches


def fetch_matches(
    tour: str,
    date_str: str,
    tournament_uid: int,
    tournament_name: str,
) -> list[dict]:
    """Non-interactive: fetch finished singles for a given tournament/date."""
    yf, mf, df = (int(x) for x in date_str.split("-"))
    events = _fetch_events(df, mf, yf)
    date_min = int(datetime(yf, mf, df, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    date_max = int((datetime(yf, mf, df, tzinfo=timezone.utc) + timedelta(days=1)).timestamp())
    return _filter_finished_singles(events, tour, tournament_uid, date_min, date_max)


# ── Step B: Annotate matches with DB state ─────────────────────────────────────

def filter_new_matches(matches: list[dict], conn) -> list[dict]:
    """Annotate each match with its current DB point count (_db_count).

    If the DB already has a partial backfill for a match (db_count > 0 but
    less than the API will return), the stale rows are deleted here so
    backfill_matches can write a clean set.  We can't know the API count
    until we fetch, so deletion for incomplete matches is deferred to
    backfill_matches — this step just flags them.
    """
    print(_sep("═"))
    print("  STEP B — Checking which matches are already in the database")
    print(_sep("═"))

    existing = get_existing_match_ids(conn)
    print(f"\n  Existing match IDs in DB: {sorted(existing) or '(none)'}\n")

    for m in matches:
        m["_db_count"] = existing.get(m["id"], 0)

    in_db = [m for m in matches if m["_db_count"] > 0]
    new   = [m for m in matches if m["_db_count"] == 0]

    if in_db:
        print("  IN DB (will check for completeness):")
        for m in in_db:
            print(
                f"    ~  {m['id']}  {m['home_player']} vs {m['away_player']}"
                f"  ({m['_db_count']} pts in DB)"
            )
    if new:
        print("  TO BACKFILL:")
        for m in new:
            print(f"    →  {m['id']}  {m['home_player']} vs {m['away_player']}")
    print()
    return matches  # all matches; backfill_matches decides skip vs re-backfill


# ── Step C: Backfill each match ────────────────────────────────────────────────

def backfill_matches(matches: list[dict], conn) -> list[dict]:
    """Fetch point-by-point data and write to DB.

    For each match:
    - If not in DB → write it.
    - If DB count < API count → delete all rows for the match_id across
      live_raw.tennisapi_points, live_raw.oddsapi_polls, and
      live_processed.dashboard_log, then re-backfill.
    - If DB count >= API count → already complete, skip.
    """
    print(_sep("═"))
    print("  STEP C — Backfilling point-by-point data")
    print(_sep("═"))

    summaries: list[dict] = []

    for m in matches:
        mid      = m["id"]
        home     = m["home_player"]
        away     = m["away_player"]
        start_ts = m.get("start_ts", 0)
        db_count = m.get("_db_count", 0)
        print(f"\n  [{mid}] {home} vs {away}")

        try:
            raw    = _get(f"/api/tennis/event/{mid}/point-by-point")
            points = derive_points(raw)
        except Exception as exc:
            print(f"    ✗  API error: {exc}")
            summaries.append({"id": mid, "home": home, "away": away,
                               "points": 0, "gps": {}, "error": str(exc)})
            continue

        if not points:
            print("    ⚠  No point data returned — skipping.")
            summaries.append({"id": mid, "home": home, "away": away,
                               "points": 0, "gps": {}, "error": "empty"})
            continue

        api_count = len(points)

        if db_count > 0:
            if api_count <= db_count:
                print(
                    f"    ✓  Already complete "
                    f"({db_count} pts in DB, {api_count} from API) — skipping."
                )
                summaries.append({
                    "id":      mid,
                    "home":    home,
                    "away":    away,
                    "points":  db_count,
                    "gps":     {},
                    "error":   None,
                    "skipped": True,
                })
                continue

            # DB count < API count → incomplete, delete and re-backfill.
            print(
                f"    ⚠  Incomplete in DB ({db_count} pts) vs "
                f"API ({api_count} pts) — re-backfilling."
            )
            _delete_match_rows(conn, mid)

        write_points(conn, mid, home, away, points, start_ts)

        gps = _games_per_set(points)
        gps_str = "  ".join(f"S{s}: {g}g" for s, g in gps.items())
        print(f"    ✓  {api_count} points written  |  {gps_str}")

        summaries.append({
            "id":     mid,
            "home":   home,
            "away":   away,
            "points": api_count,
            "gps":    gps,
            "error":  None,
        })

        time.sleep(0.5)

    print()
    return summaries


def _delete_match_rows(conn, match_id: int) -> None:
    """Delete all rows for match_id from all three Medallion tables.

    Each table is deleted in its own try/except so a missing table
    (e.g. on a fresh DB) doesn't abort the whole operation.
    """
    tables = (
        "live_raw.tennisapi_points",
        "live_raw.oddsapi_polls",
        "live_processed.dashboard_log",
    )
    for tbl in tables:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl} WHERE match_id = %s", [match_id])
            conn.commit()
        except psycopg2.Error:
            conn.rollback()


# ── Programmatic entry point ───────────────────────────────────────────────────

def run_backfill(
    tour: str,
    date_str: str,
    tournament_uid: int,
    tournament_name: str,
    conn,
) -> list[dict]:
    """Import-and-call entry point for use by the full-backfill orchestrator."""
    matches = fetch_matches(tour, date_str, tournament_uid, tournament_name)
    if not matches:
        print(f"  No finished singles matches for {tournament_name} on {date_str}.")
        return []

    existing = get_existing_match_ids(conn)
    for m in matches:
        m["_db_count"] = existing.get(m["id"], 0)

    return backfill_matches(matches, conn)


# ── Step D: Validation summary ─────────────────────────────────────────────────

def print_summary(summaries: list[dict]) -> None:
    print(_sep("═"))
    print("  STEP D — Final validation summary")
    print(_sep("═"))

    written    = [s for s in summaries if s["error"] is None and not s.get("skipped")]
    skipped    = [s for s in summaries if s.get("skipped")]
    errors     = [s for s in summaries if s["error"] is not None]
    total_pts  = sum(s["points"] for s in written)
    suspicious = []

    print(f"\n  Matches written     : {len(written)}")
    print(f"  Skipped (complete)  : {len(skipped)}")
    print(f"  Errors              : {len(errors)}")
    print(f"  Total points written: {total_pts}")

    print(f"\n  {'ID':<12} {'Players':<40} {'Points':<8} Games/Set")
    print(f"  {'─'*10} {'─'*38} {'─'*6} {'─'*25}")
    for s in summaries:
        players = f"{s['home']} vs {s['away']}"[:38]
        if s.get("skipped"):
            print(f"  {s['id']:<12} {players:<40} {'SKIP':<8} (already complete)")
            continue
        if s["error"]:
            print(f"  {s['id']:<12} {players:<40} {'ERR':<8} {s['error']}")
            continue
        gps_str = "  ".join(f"S{sv}: {g}" for sv, g in s["gps"].items())
        flag = ""
        for sv, g in s["gps"].items():
            if g < 6 or g > 13:
                flag = "  ⚠ SUSPICIOUS"
                suspicious.append((s["id"], sv, g))
        print(f"  {s['id']:<12} {players:<40} {s['points']:<8} {gps_str}{flag}")

    if suspicious:
        print(f"\n  ⚠  SUSPICIOUS GAME COUNTS (< 6 or > 13 games in a set):")
        for mid, sv, g in suspicious:
            print(f"     Match {mid}, Set {sv}: {g} games")
    else:
        print("\n  ✓  All derived game counts look normal (6–13 per set).")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    conn = _get_conn()
    _ensure_tables(conn)

    try:
        matches = fetch_todays_matches()
        if not matches:
            print("  No finished singles matches found for the selected tournament/date.")
            return

        annotated = filter_new_matches(matches, conn)
        summaries = backfill_matches(annotated, conn)
        print_summary(summaries)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
