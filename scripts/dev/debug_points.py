"""
Point-by-point data debugger for tennis-engine DuckDB.
Connects to the local DuckDB, inspects all tables, finds the most recent
match with point data, prints every point with quality checks, and falls back
to TennisAPI1 if no live data exists.

Run from project root:
    .venv/bin/python scripts/debug_points.py
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

# ── project root on sys.path ──────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import duckdb
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "processed" / "tennis.duckdb"

DIVIDER  = "─" * 80
SECTION  = "═" * 80
WARN     = "⚠️  WARNING"

# Valid tennis point-score progressions (non-tiebreak)
VALID_SCORES = {"0", "15", "30", "40", "A", "AD", "50"}   # 50 = game-winner sentinel in some APIs

SCORE_ORDER = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4, "AD": 4}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print(f"\n{SECTION}")
    print(f"  {title}")
    print(SECTION)


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def warn(msg: str) -> None:
    print(f"\n  {WARN}: {msg}")


def fmt_val(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


# ──────────────────────────────────────────────────────────────────────────────
# 1. List all tables / schemas
# ──────────────────────────────────────────────────────────────────────────────

def list_tables(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    banner("STEP 1 — All tables / schemas in tennis.duckdb")
    rows = conn.execute("""
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        ORDER BY table_schema, table_name
    """).fetchall()

    print(f"\n  {'SCHEMA':<20} {'TABLE':<35} {'TYPE':<12}")
    print(f"  {'------':<20} {'-----':<35} {'----':<12}")

    live_candidates = []
    for schema, table, ttype in rows:
        marker = ""
        name_lower = table.lower()
        if any(kw in name_lower for kw in ("live", "match_log", "point", "raw", "ingest")):
            marker = "  ◀ live/point candidate"
            live_candidates.append({"schema": schema, "table": table})
        print(f"  {schema:<20} {table:<35} {ttype:<12}{marker}")

    if not live_candidates:
        print("\n  (no tables with 'live', 'match_log', 'point', 'raw', or 'ingest' in name found)")

    # Also check for live_match_log specifically
    try:
        n = conn.execute("SELECT COUNT(*) FROM live_match_log").fetchone()[0]
        print(f"\n  live_match_log (main schema): {n:,} rows")
        if {"schema": "main", "table": "live_match_log"} not in live_candidates:
            live_candidates.append({"schema": "main", "table": "live_match_log"})
    except Exception:
        pass

    return live_candidates


# ──────────────────────────────────────────────────────────────────────────────
# 2. Find the most recent match
# ──────────────────────────────────────────────────────────────────────────────

def find_recent_match(conn: duckdb.DuckDBPyConnection) -> dict | None:
    banner("STEP 2 — Most recent match with point-by-point data")

    # Confirm live_match_log exists and has rows
    try:
        total = conn.execute("SELECT COUNT(*) FROM live_match_log").fetchone()[0]
    except Exception as exc:
        print(f"\n  live_match_log not found or unreadable: {exc}")
        return None

    if total == 0:
        print("\n  live_match_log exists but has 0 rows.")
        return None

    print(f"\n  live_match_log has {total:,} total rows across all matches.\n")

    # Show schema
    cols = conn.execute("DESCRIBE live_match_log").fetchall()
    print(f"  {'COLUMN':<25} {'TYPE':<20}")
    print(f"  {'------':<25} {'----':<20}")
    for col_name, col_type, *_ in cols:
        print(f"  {col_name:<25} {col_type:<20}")

    # Most recent match
    row = conn.execute("""
        SELECT match_id,
               player_a,
               player_b,
               MIN(ts)   AS first_ts,
               MAX(ts)   AS last_ts,
               COUNT(*)  AS point_count
        FROM live_match_log
        GROUP BY match_id, player_a, player_b
        ORDER BY MAX(ts) DESC
        LIMIT 1
    """).fetchone()

    if not row:
        return None

    match_id, player_a, player_b, first_ts, last_ts, point_count = row

    print(f"""
  Match ID   : {match_id}
  Player A   : {player_a}
  Player B   : {player_b}
  First point: {first_ts}
  Last point : {last_ts}
  Points     : {point_count:,}
""")

    return {
        "match_id": match_id,
        "player_a": player_a,
        "player_b": player_b,
        "point_count": point_count,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 3. Print every point
# ──────────────────────────────────────────────────────────────────────────────

def print_all_points(conn: duckdb.DuckDBPyConnection, match: dict) -> list[dict]:
    banner(f"STEP 3 — All {match['point_count']} points for match {match['match_id']}")

    rows = conn.execute("""
        SELECT *
        FROM live_match_log
        WHERE match_id = ?
        ORDER BY set_num, game_num, point_num, ts
    """, [match["match_id"]]).fetchall()

    col_names = [d[0] for d in conn.description]
    points = [dict(zip(col_names, r)) for r in rows]

    # Determine "extra" columns beyond the core set
    core_cols = {
        "ts", "match_id", "player_a", "player_b",
        "set_num", "game_num", "point_num",
        "home_point", "away_point", "server", "point_winner",
        "is_ace", "is_double_fault",
        "model_prob_a", "bookmaker_prob_a", "edge",
    }
    extra_cols = [c for c in col_names if c not in core_cols]

    current_set  = None
    current_game = None

    for i, pt in enumerate(points):
        s = pt.get("set_num")
        g = pt.get("game_num")

        if s != current_set:
            current_set  = s
            current_game = None
            print(f"\n  ┌── SET {s} {'─'*60}")

        if g != current_game:
            current_game = g
            print(f"  │  ── Game {g} ──")

        hp = fmt_val(pt.get("home_point"))
        ap = fmt_val(pt.get("away_point"))
        srv_tag  = f"{pt.get('server','?'):<5}"
        win_tag  = f"{pt.get('point_winner','?'):<5}"
        ace_tag  = "ACE " if pt.get("is_ace")          else "    "
        df_tag   = "DF  " if pt.get("is_double_fault") else "    "
        prob_tag = f"P(A)={fmt_val(pt.get('model_prob_a')):<8}"
        bk_tag   = f"Bk={fmt_val(pt.get('bookmaker_prob_a')):<8}"
        edge_tag = f"Edge={fmt_val(pt.get('edge')):<9}"

        line = (
            f"  │    Pt {pt.get('point_num','?'):>3}  "
            f"S{s}G{g}  "
            f"Score [{hp:>3} – {ap:<3}]  "
            f"Srv:{srv_tag}  Won:{win_tag}  "
            f"{ace_tag}{df_tag}"
            f"{prob_tag} {bk_tag} {edge_tag}"
        )
        print(line)

        # Extra columns (model signals, score ints, etc.)
        extras = []
        for ec in extra_cols:
            v = pt.get(ec)
            if v is not None:
                extras.append(f"{ec}={fmt_val(v)}")
        if extras:
            print(f"  │         " + "  ".join(extras))

    return points


# ──────────────────────────────────────────────────────────────────────────────
# 4. Data quality checks
# ──────────────────────────────────────────────────────────────────────────────

def quality_checks(points: list[dict], match: dict) -> None:
    banner("STEP 4 — Data quality checks")

    issues = 0

    # ── 4a. Null / missing score fields ──────────────────────────────────────
    section("4a. Null / missing score fields")
    null_score = []
    for pt in points:
        missing = [f for f in ("home_point", "away_point", "server", "point_winner")
                   if pt.get(f) is None]
        if missing:
            null_score.append((pt.get("point_num"), pt.get("set_num"), pt.get("game_num"), missing))

    if null_score:
        for pnum, s, g, fields in null_score:
            warn(f"Pt {pnum} (S{s}G{g}): NULL in {fields}")
            issues += 1
    else:
        print("\n  ✓  No null score fields.")

    # ── 4b. Duplicate point records ──────────────────────────────────────────
    section("4b. Duplicate point records")
    seen: dict[tuple, int] = {}
    for pt in points:
        key = (pt.get("set_num"), pt.get("game_num"), pt.get("point_num"))
        if key in seen:
            warn(f"Duplicate key (S{key[0]}G{key[1]}Pt{key[2]}) "
                 f"— appears at list positions {seen[key]} and {points.index(pt)}")
            issues += 1
        else:
            seen[key] = points.index(pt)

    if not any(
        (pt.get("set_num"), pt.get("game_num"), pt.get("point_num"))
        in [k for k, v in seen.items() if list(seen.values()).count(v) > 1]
        for pt in points
    ):
        print("\n  ✓  No duplicate (set, game, point_num) combos.")

    # ── 4c. Out-of-sequence point_num ────────────────────────────────────────
    section("4c. Out-of-sequence point numbers")
    prev_pnum = None
    prev_key  = None
    seq_issues = 0
    for pt in points:
        pnum = pt.get("point_num")
        key  = (pt.get("set_num"), pt.get("game_num"))
        if prev_key == key and prev_pnum is not None and pnum is not None:
            if pnum <= prev_pnum:
                warn(f"S{key[0]}G{key[1]}: point_num jumped from {prev_pnum} → {pnum} (not strictly increasing)")
                issues += 1
                seq_issues += 1
        prev_pnum = pnum
        prev_key  = key

    if seq_issues == 0:
        print("\n  ✓  Point numbers are strictly increasing within each game.")

    # ── 4d. Score progression (non-tiebreak games) ───────────────────────────
    section("4d. Score progression (non-tiebreak games only)")

    # Group by (set, game)
    from collections import defaultdict
    by_game: dict[tuple, list[dict]] = defaultdict(list)
    for pt in points:
        by_game[(pt.get("set_num"), pt.get("game_num"))].append(pt)

    prog_issues = 0
    for (s, g), game_pts in sorted(by_game.items()):
        # Detect tiebreak: any score that's a raw integer > 4 or not in VALID_SCORES
        is_tiebreak = any(
            str(pt.get("home_point", "")).strip() not in VALID_SCORES or
            str(pt.get("away_point", "")).strip() not in VALID_SCORES
            for pt in game_pts
        )
        if is_tiebreak:
            print(f"\n  S{s}G{g}: appears to be a tiebreak — score progression check skipped.")
            continue

        # Check server's score never goes backwards (simple sanity check)
        prev_h = prev_a = None
        for pt in game_pts:
            hp = str(pt.get("home_point", "")).strip()
            ap = str(pt.get("away_point", "")).strip()
            pnum = pt.get("point_num")

            # 0 appearing after a non-zero score is suspicious unless it's a new game
            if prev_h is not None:
                if hp == "0" and prev_h not in ("0", None):
                    # Only flag if prev wasn't "40" or "A" (game just ended)
                    if prev_h not in ("40", "A", "AD"):
                        warn(f"S{s}G{g} Pt{pnum}: home score reset to 0 from {prev_h} "
                             f"(unexpected mid-game reset)")
                        issues += 1
                        prog_issues += 1
                if ap == "0" and prev_a not in ("0", None):
                    if prev_a not in ("40", "A", "AD"):
                        warn(f"S{s}G{g} Pt{pnum}: away score reset to 0 from {prev_a} "
                             f"(unexpected mid-game reset)")
                        issues += 1
                        prog_issues += 1

            prev_h = hp
            prev_a = ap

    if prog_issues == 0:
        print("\n  ✓  No invalid score progressions detected.")

    # ── 4e. Probability sanity ────────────────────────────────────────────────
    section("4e. Probability value sanity")
    prob_issues = 0
    for pt in points:
        mp = pt.get("model_prob_a")
        bp = pt.get("bookmaker_prob_a")
        for label, val in [("model_prob_a", mp), ("bookmaker_prob_a", bp)]:
            if val is not None and not (0.0 <= val <= 1.0):
                warn(f"Pt {pt.get('point_num')} (S{pt.get('set_num')}G{pt.get('game_num')}): "
                     f"{label}={val:.4f} outside [0,1]")
                issues += 1
                prob_issues += 1

    if prob_issues == 0:
        print("\n  ✓  All probability values in [0, 1].")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Quality check summary")
    if issues == 0:
        print(f"\n  ✅  No data quality issues found in {len(points)} points.")
    else:
        print(f"\n  ❌  {issues} total issue(s) flagged across {len(points)} points.")


# ──────────────────────────────────────────────────────────────────────────────
# 5. Fallback: call TennisAPI1 directly
# ──────────────────────────────────────────────────────────────────────────────

def fallback_api() -> None:
    banner("STEP 5 — Fallback: calling TennisAPI1 directly")
    print("""
  NOTE: live_match_log has no data (or doesn't exist).
        The live ingestion pipeline may not have been run yet.
        Attempting to call TennisAPI1 /events/live now...
""")

    api_key = os.environ.get("RAPIDAPI_KEY")
    if not api_key:
        print("  ERROR: RAPIDAPI_KEY not found in .env — cannot call API.")
        return

    import requests  # only import if needed

    headers = {
        "x-rapidapi-host": "tennisapi1.p.rapidapi.com",
        "x-rapidapi-key": api_key,
    }

    # 5a. List live matches
    url_live = "https://tennisapi1.p.rapidapi.com/api/tennis/events/live"
    print(f"  GET {url_live}")
    try:
        resp = requests.get(url_live, headers=headers, timeout=10)
        print(f"  HTTP {resp.status_code}")
        data = resp.json()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    events = data.get("events", data) if isinstance(data, dict) else data
    if not isinstance(events, list) or not events:
        print("\n  No live matches currently available.")
        print("\n  Raw response:")
        print(textwrap.indent(json.dumps(data, indent=2)[:3000], "    "))
        print("\n  NOTE: The live ingestion pipeline (src/live/match_runner.py) "
              "needs to be running during a live match to populate live_match_log.")
        return

    print(f"\n  {len(events)} live match(es) found:\n")
    for ev in events[:5]:
        home = (ev.get("homeTeam") or {}).get("name") or ev.get("homeName", "?")
        away = (ev.get("awayTeam") or {}).get("name") or ev.get("awayName", "?")
        print(f"    ID {ev.get('id')} — {home} vs {away}")

    # 5b. Fetch point-by-point for first match
    first_id = events[0].get("id")
    url_pbp  = f"https://tennisapi1.p.rapidapi.com/api/tennis/event/{first_id}/point-by-point"
    print(f"\n  GET {url_pbp}")
    try:
        resp2 = requests.get(url_pbp, headers=headers, timeout=10)
        print(f"  HTTP {resp2.status_code}")
        pbp = resp2.json()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return

    print("\n  Raw pointByPoint JSON (first 4000 chars):")
    print(textwrap.indent(json.dumps(pbp, indent=2)[:4000], "    "))
    print("\n  NOTE: The live ingestion pipeline (src/live/match_runner.py) "
          "has NOT been built yet, or has not been started. "
          "Run it during a live match to populate live_match_log.")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    banner(f"TENNIS POINT-BY-POINT DEBUGGER")
    print(f"  DB: {DB_PATH}")
    print(f"  DB exists: {DB_PATH.exists()}")

    if not DB_PATH.exists():
        print("\n  ERROR: DuckDB file not found. Run seed_demo.py first.")
        fallback_api()
        return

    conn = duckdb.connect(str(DB_PATH), read_only=True)

    try:
        live_candidates = list_tables(conn)
        match = find_recent_match(conn)

        if match is None:
            conn.close()
            fallback_api()
            return

        points = print_all_points(conn, match)
        quality_checks(points, match)

    finally:
        conn.close()

    print(f"\n{SECTION}")
    print("  Done.")
    print(SECTION)


if __name__ == "__main__":
    main()
