#!/usr/bin/env python3
"""
backfill_tournament_odds.py — Backfill bookmaker odds for an entire
tournament day in one Odds API sweep.

A single historical snapshot covers every active match for a sport key,
so N snapshots backfill every match in the tournament instead of doing
N_matches × N_snapshots calls.

Usage (from project root):
    .venv/bin/python scripts/backtesting/backfill_tournament_odds.py
"""
from __future__ import annotations

import math
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import duckdb
import requests
from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from src.live.odds_fetcher import TOURNAMENT_MAP, _TENNIS_SPORT_KEYS, _compute_consensus

_DB_PATH = _ROOT / "data" / "processed" / "tennis.duckdb"

# ── TennisAPI config ──────────────────────────────────────────────────────────
_TENNIS_BASE    = "https://tennisapi1.p.rapidapi.com"
_RAPIDAPI_KEY   = os.environ.get("RAPIDAPI_KEY", "")
_TENNIS_HEADERS = {
    "x-rapidapi-host": "tennisapi1.p.rapidapi.com",
    "x-rapidapi-key":  _RAPIDAPI_KEY,
}
_MAX_RETRIES = 3
_RETRY_DELAY = 1.0

# ── Odds API config ───────────────────────────────────────────────────────────
_ODDS_BASE        = "https://api.the-odds-api.com/v4"
_ODDS_TIMEOUT     = 15
_STEP_MINUTES     = 5
_CREDITS_PER_CALL = 10
_REQUEST_SLEEP    = 2.0


# ── Formatting ────────────────────────────────────────────────────────────────

def _sep(char: str = "─", width: int = 70) -> str:
    return char * width


# ── TennisAPI HTTP helper ─────────────────────────────────────────────────────

def _get(path: str) -> dict | list:
    url = f"{_TENNIS_BASE}{path}"
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_TENNIS_HEADERS, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            last_exc = ValueError(
                f"HTTP {resp.status_code} from {path}: {resp.text[:300]}"
            )
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY)
    raise last_exc


# ── Prompt flow ───────────────────────────────────────────────────────────────

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
    seen: dict[int, str] = {}
    for ev in events:
        tournament = ev.get("tournament", {})
        cat_name = str(tournament.get("category", {}).get("name", "")).upper()
        if tour not in cat_name:
            continue
        ut = tournament.get("uniqueTournament", {})
        ut_name = ut.get("name", "")
        ut_id   = ut.get("id")
        if not ut_name or ut_id is None:
            continue
        if "double" in ut_name.lower():
            continue
        seen.setdefault(ut_id, ut_name)
    return sorted(seen.items(), key=lambda t: t[1])


def _prompt_tournament(entries: list[tuple[int, str]]) -> tuple[str, int]:
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


# ── Player name fuzzy matching ────────────────────────────────────────────────

def _normalise(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode()
    return " ".join(ascii_str.lower().split())


def _names_match(a: str, b: str) -> bool:
    na, nb = _normalise(a), _normalise(b)
    if na == nb or na in nb or nb in na:
        return True
    return bool(na.split()) and bool(nb.split()) and na.split()[-1] == nb.split()[-1]


def _find_db_match(
    odds_home: str,
    odds_away: str,
    db_matches: list[dict],
) -> dict | None:
    for m in db_matches:
        if (
            (_names_match(odds_home, m["home"]) and _names_match(odds_away, m["away"]))
            or (_names_match(odds_home, m["away"]) and _names_match(odds_away, m["home"]))
        ):
            return m
    return None


# ── Historical Odds API ───────────────────────────────────────────────────────

def _fetch_snapshot(
    sport_key: str,
    snapshot_ts: datetime,
    api_key: str,
) -> tuple[str | None, list[dict], dict[str, str]]:
    ts_str = snapshot_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"{_ODDS_BASE}/historical/sports/{sport_key}/odds"
        f"?apiKey={api_key}"
        f"&date={ts_str}"
        f"&regions=us&markets=h2h&oddsFormat=decimal"
    )
    try:
        resp = requests.get(url, timeout=_ODDS_TIMEOUT)
    except requests.RequestException as exc:
        print(f"    [request error] {exc}")
        return None, [], {}

    headers = dict(resp.headers)

    if resp.status_code == 429:
        print("    [429 rate-limited] sleeping 30 s …")
        time.sleep(30)
        return None, [], headers

    if resp.status_code != 200:
        print(f"    [HTTP {resp.status_code}] {resp.text[:200]}")
        return None, [], headers

    body        = resp.json()
    api_ts: str | None = body.get("timestamp")
    events: list[dict] = body.get("data", [])
    return api_ts, events, headers


def _parse_api_ts(api_ts_str: str | None, fallback: datetime) -> datetime:
    if api_ts_str:
        try:
            return datetime.fromisoformat(
                api_ts_str.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            pass
    return fallback


# ── Snapshot generation ───────────────────────────────────────────────────────

def _generate_snapshots(start: datetime, end: datetime) -> list[datetime]:
    """Return UTC datetimes at _STEP_MINUTES intervals from start through end (inclusive)."""
    step  = timedelta(minutes=_STEP_MINUTES)
    snaps: list[datetime] = []
    t = start
    while t <= end:
        snaps.append(t)
        t += step
    return snaps


def _compute_window_from_db(
    match_ids: list[int],
    conn: duckdb.DuckDBPyConnection,
) -> tuple[datetime, datetime]:
    """Derive the odds-sweep window from actual point timestamps in the DB.

    Uses MIN(ts) of the earliest synthetic point and MAX(ts) + 10 min of the
    latest.  Points must already be written by backfill_today before calling.
    Returns tz-naive UTC datetimes (matching what DuckDB stores).
    """
    placeholders = ", ".join("?" * len(match_ids))
    bounds = conn.execute(
        f"SELECT MIN(ts), MAX(ts) FROM live_raw.tennisapi_points"
        f" WHERE match_id IN ({placeholders})",
        match_ids,
    ).fetchone()

    if not bounds or bounds[0] is None:
        raise RuntimeError(
            "No points found in DB for the given match_ids. "
            "Run point backfill first."
        )

    start_time = bounds[0]                          # tz-naive UTC
    end_time   = bounds[1] + timedelta(minutes=10)  # 10-min buffer after last point
    return start_time, end_time


# ── DB helpers ────────────────────────────────────────────────────────────────

def _clean(v: object) -> object:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS live_raw")
    conn.execute("CREATE SCHEMA IF NOT EXISTS live_processed")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_raw.oddsapi_polls (
            ts                    TIMESTAMP,
            match_id              INTEGER,
            player_a              VARCHAR,
            player_b              VARCHAR,
            bookmaker_prob_a      FLOAT,
            num_bookmakers        INTEGER,
            api_credits_remaining INTEGER
        )
    """)


_INSERT_POLL = """
INSERT INTO live_raw.oddsapi_polls (
    ts, match_id, player_a, player_b,
    bookmaker_prob_a, num_bookmakers, api_credits_remaining
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def _write_poll(
    conn: duckdb.DuckDBPyConnection,
    ts: datetime,
    match_id: int,
    player_a: str,
    player_b: str,
    bookmaker_prob_a: float,
    num_bookmakers: int,
    api_credits_remaining: int | None,
) -> None:
    conn.execute(_INSERT_POLL, [
        ts.replace(tzinfo=None),
        match_id,
        player_a,
        player_b,
        _clean(bookmaker_prob_a),
        num_bookmakers,
        api_credits_remaining,
    ])


# ── Core logic (shared by standalone and orchestrated paths) ──────────────────

def _get_target_matches(
    all_events: list[dict],
    tournament_uid: int,
    date_min: int,
    date_max: int,
) -> list[dict]:
    """Filter events to finished singles for the given tournament/date window."""
    target: list[dict] = []
    for ev in all_events:
        if ev.get("status", {}).get("type", "") != "finished":
            continue
        start_ts = ev.get("startTimestamp", 0)
        if not (date_min <= start_ts < date_max):
            continue
        if ev.get("tournament", {}).get("uniqueTournament", {}).get("id") != tournament_uid:
            continue
        if "double" in str(ev.get("tournament", {}).get("name", "")).lower():
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
        target.append({"home": home, "away": away, "start_ts": start_ts})
    return target


def _resolve_db_match_ids(
    target_matches: list[dict],
    conn: duckdb.DuckDBPyConnection,
) -> tuple[list[dict], list[dict]]:
    """Look up match_ids from tennisapi_points. Returns (db_matches, missing)."""
    for m in target_matches:
        row = conn.execute("""
            SELECT DISTINCT match_id FROM live_raw.tennisapi_points
            WHERE (player_a = ? AND player_b = ?)
               OR (player_a = ? AND player_b = ?)
            LIMIT 1
        """, [m["home"], m["away"], m["away"], m["home"]]).fetchone()
        m["match_id"] = row[0] if row else None

    db_matches = [m for m in target_matches if m["match_id"] is not None]
    missing    = [m for m in target_matches if m["match_id"] is None]
    return db_matches, missing


def _run_odds_sweep(
    db_matches: list[dict],
    sport_key: str,
    window_start: datetime,
    window_end: datetime,
    conn: duckdb.DuckDBPyConnection,
    odds_api_key: str,
) -> int:
    """Fetch snapshots and insert poll rows. Returns total rows inserted."""
    snapshots = _generate_snapshots(window_start, window_end)
    n = len(snapshots)
    total_inserted = 0
    total_empty    = 0

    for i, snap_ts in enumerate(snapshots, 1):
        label = snap_ts.strftime("%H:%M")
        api_ts_str, events_snap, headers = _fetch_snapshot(
            sport_key, snap_ts, odds_api_key
        )

        credits_remaining = headers.get("x-requests-remaining")
        cr_int = int(credits_remaining) if credits_remaining else None
        ts_to_store = _parse_api_ts(api_ts_str, snap_ts)

        snap_inserted = 0
        for event in events_snap:
            odds_home = event.get("home_team", "")
            odds_away = event.get("away_team", "")

            db_match = _find_db_match(odds_home, odds_away, db_matches)
            if db_match is None:
                continue

            consensus = _compute_consensus(event, db_match["home"])
            if consensus is None:
                continue

            _write_poll(
                conn,
                ts=ts_to_store,
                match_id=db_match["match_id"],
                player_a=db_match["home"],
                player_b=db_match["away"],
                bookmaker_prob_a=consensus["bookmaker_implied_prob"],
                num_bookmakers=consensus["num_bookmakers"],
                api_credits_remaining=cr_int,
            )
            snap_inserted += 1

        total_inserted += snap_inserted
        if snap_inserted == 0:
            total_empty += 1

        cr_str = str(cr_int) if cr_int is not None else "?"
        status = f"{snap_inserted} row(s)" if snap_inserted else "no matches in snapshot"
        print(f"  [{i:>3}/{n}] {label}  {status:<28}  credits_left={cr_str}")

        if i < n:
            time.sleep(_REQUEST_SLEEP)

    print()
    print(_sep("═"))
    print(
        f"  Done.  Rows inserted: {total_inserted}"
        f"  |  Empty snapshots: {total_empty}/{n}"
    )
    print(_sep("═"))
    return total_inserted


# ── Programmatic entry point ───────────────────────────────────────────────────

def run_odds_backfill(
    tour: str,
    date_str: str,
    tournament_uid: int,
    tournament_name: str,
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Import-and-call entry point for use by the full-backfill orchestrator.

    Raises RuntimeError if env vars are missing or tournament_uid is not in
    TOURNAMENT_MAP (use the standalone script for manual sport_key selection).
    """
    odds_api_key = os.environ.get("ODDS_API_KEY")
    if not odds_api_key:
        raise RuntimeError("ODDS_API_KEY is not set.")
    if not _RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is not set.")

    _ensure_tables(conn)

    yf, mf, df = (int(x) for x in date_str.split("-"))
    all_events = _fetch_events(df, mf, yf)

    date_min = int(datetime(yf, mf, df, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    date_max = int(
        (datetime(yf, mf, df, tzinfo=timezone.utc) + timedelta(days=1)).timestamp()
    )

    target_matches = _get_target_matches(all_events, tournament_uid, date_min, date_max)
    if not target_matches:
        print(f"  No finished singles matches for {tournament_name} on {date_str}.")
        return

    db_matches, missing = _resolve_db_match_ids(target_matches, conn)
    if missing:
        print(f"  WARNING — {len(missing)} match(es) not in DB (skipped):")
        for m in missing:
            print(f"    {m['home']} vs {m['away']}")
    if not db_matches:
        print("  No matches found in DB. Run point backfill first.")
        return

    sport_key = TOURNAMENT_MAP.get(tournament_uid)
    if not sport_key:
        raise RuntimeError(
            f"Tournament uid {tournament_uid} not in TOURNAMENT_MAP. "
            "Use the standalone script to select the sport_key manually."
        )

    # Derive window from actual point timestamps already in the DB so it
    # matches only the period when matches were in progress.
    match_id_list = [m["match_id"] for m in db_matches]
    window_start, window_end = _compute_window_from_db(match_id_list, conn)

    snapshots = _generate_snapshots(window_start, window_end)
    cost      = len(snapshots) * _CREDITS_PER_CALL
    print(
        f"  Window: {window_start.strftime('%Y-%m-%dT%H:%M')} → "
        f"{window_end.strftime('%Y-%m-%dT%H:%M')} UTC"
        f"  |  Snapshots: {len(snapshots)}  |  Est. cost: {cost:,} credits"
    )

    _run_odds_sweep(db_matches, sport_key, window_start, window_end, conn, odds_api_key)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    odds_api_key = os.environ.get("ODDS_API_KEY")
    if not odds_api_key:
        print("  ERROR: ODDS_API_KEY is not set. Add it to your .env file.")
        raise SystemExit(1)
    if not _RAPIDAPI_KEY:
        print("  ERROR: RAPIDAPI_KEY is not set. Add it to your .env file.")
        raise SystemExit(1)

    print(_sep("═"))
    print("  Tournament Odds Backfill")
    print(_sep("═"))
    print()

    # ── Step A: Tournament discovery ─────────────────────────────────────────
    tour     = _prompt_tour()
    date_str = _prompt_date()
    yf, mf, df = (int(x) for x in date_str.split("-"))

    print(f"\n  Fetching events for {date_str}...")
    all_events = _fetch_events(df, mf, yf)

    entries = _build_tournament_list(all_events, tour)
    if not entries:
        print(f"  No {tour} tournaments found for {date_str}.")
        return

    tournament_name, tournament_uid = _prompt_tournament(entries)

    # ── Step B: Filter to finished singles ───────────────────────────────────
    date_min = int(datetime(yf, mf, df, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    date_max = int(
        (datetime(yf, mf, df, tzinfo=timezone.utc) + timedelta(days=1)).timestamp()
    )

    target_matches = _get_target_matches(all_events, tournament_uid, date_min, date_max)
    if not target_matches:
        print(f"\n  No finished singles matches found for {tournament_name} on {date_str}.")
        return

    # ── Step C: Tournament summary ────────────────────────────────────────────
    print(_sep())
    print(f"  Tournament : {tournament_name}  (uid={tournament_uid})")
    print(f"  Date       : {date_str}  |  Tour: {tour}")
    print(f"  Matches    : {len(target_matches)}")
    for m in target_matches:
        print(f"    {m['home']} vs {m['away']}")

    # ── Step D: Resolve match_ids from DB ────────────────────────────────────
    conn = duckdb.connect(str(_DB_PATH))
    _ensure_tables(conn)

    try:
        db_matches, missing = _resolve_db_match_ids(target_matches, conn)

        if missing:
            print(f"\n  WARNING — {len(missing)} match(es) not in DB (skipped):")
            for m in missing:
                print(f"    {m['home']} vs {m['away']}")

        if not db_matches:
            print("\n  No matches found in DB. Run backfill_today.py first.")
            return

        print(f"\n  DB match_ids resolved: {len(db_matches)}")
        for m in db_matches:
            print(f"    [{m['match_id']}] {m['home']} vs {m['away']}")

        # Derive window from actual point timestamps so we only pull snapshots
        # during the period matches were in progress.
        match_id_list = [m["match_id"] for m in db_matches]
        window_start, window_end = _compute_window_from_db(match_id_list, conn)
        print(
            f"\n  Window     : {window_start.strftime('%Y-%m-%dT%H:%M')} → "
            f"{window_end.strftime('%Y-%m-%dT%H:%M')} UTC"
        )

        # ── Step E: Resolve sport_key ─────────────────────────────────────────
        sport_key = TOURNAMENT_MAP.get(tournament_uid)
        if sport_key:
            print(f"\n  Sport key  : {sport_key}  (from TOURNAMENT_MAP)")
        else:
            print(f"\n  Tournament uid {tournament_uid} not in TOURNAMENT_MAP.")
            print("  Available sport keys:")
            for i, k in enumerate(_TENNIS_SPORT_KEYS, 1):
                print(f"    {i:>2}.  {k}")
            while True:
                raw = input("Select number (or type the key directly): ").strip()
                if raw.isdigit():
                    idx = int(raw) - 1
                    if 0 <= idx < len(_TENNIS_SPORT_KEYS):
                        sport_key = _TENNIS_SPORT_KEYS[idx]
                        break
                elif raw in _TENNIS_SPORT_KEYS:
                    sport_key = raw
                    break
                print("  Invalid choice.")

        # ── Step F: Snapshots + credit gate ───────────────────────────────────
        snapshots = _generate_snapshots(window_start, window_end)
        n         = len(snapshots)
        cost      = n * _CREDITS_PER_CALL

        print(_sep())
        print(f"  Snapshots   : {n}  (every {_STEP_MINUTES} min)")
        print(f"  API cost    : {cost:,} credits")
        if snapshots:
            print(f"  First snap  : {snapshots[0].strftime('%Y-%m-%dT%H:%M')} UTC")
            print(f"  Last  snap  : {snapshots[-1].strftime('%Y-%m-%dT%H:%M')} UTC")

        confirm = input("\n  Proceed? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Aborted.")
            return

        # ── Step G: Fetch + store ─────────────────────────────────────────────
        print()
        _run_odds_sweep(db_matches, sport_key, window_start, window_end, conn, odds_api_key)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
