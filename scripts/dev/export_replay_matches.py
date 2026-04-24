#!/usr/bin/env python3
"""
Export 3 randomly selected matches from DuckDB to JSON for the replay UI.

Usage (from project root, with venv active):
    python3 scripts/export_replay_matches.py
"""

import json
import os
import sys
import re
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("ERROR: duckdb not installed. Run: pip install duckdb")
    sys.exit(1)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "processed" / "tennis.duckdb"
OUTPUT_PATH = Path(__file__).resolve().parent.parent.parent / "debug_ui" / "public" / "replay_matches.json"


def parse_match_id(match_id: str):
    """
    Parse match_id like '20191124-M-Davis_Cup_Finals-F-Rafael_Nadal-Denis_Shapovalov'
    Returns dict with date, tournament, round, playerA, playerB, year.

    Format: YYYYMMDD-Gender-Tournament-Round-Player1-Player2
    Player names use underscores for spaces.
    Tournament names may also contain underscores.
    """
    parts = match_id.split("-")
    date_str = parts[0]  # YYYYMMDD
    year = int(date_str[:4])

    # The last two segments are player names (underscores -> spaces)
    # But we need to handle multi-part names joined by underscore within a segment.
    # The split is by '-', and player names are always the last 2 '-'-separated segments
    # that contain underscores (first_last format).

    # Strategy: work backwards. Last segment = player B, second-to-last = player A.
    # But some player names could be just one word (e.g., rare).
    # More robust: the standard format is DATE-M-TOURNEY-ROUND-PLAYER_A-PLAYER_B
    # where TOURNEY might have hyphens... actually no, looking at examples,
    # tournaments use underscores (Davis_Cup_Finals, Tour_Finals).
    # So splitting by '-' gives: [date, gender, tourney, round, playerA, playerB]

    player_b = parts[-1].replace("_", " ")
    player_a = parts[-2].replace("_", " ")
    round_code = parts[-3] if len(parts) > 4 else ""
    tournament = "-".join(parts[2:-3]).replace("_", " ") if len(parts) > 5 else parts[2].replace("_", " ") if len(parts) > 3 else ""

    return {
        "date": date_str,
        "year": year,
        "tournament": tournament,
        "round": round_code,
        "playerA": player_a,
        "playerB": player_b,
    }


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH), read_only=True)

    # ── Step 1: Find qualifying matches ──
    # Must have 80+ points, date between 2010-2025
    qualifying_query = """
        WITH match_stats AS (
            SELECT
                match_id,
                COUNT(*) AS pt_count,
                COUNT(rally_length) AS rally_count,
                COUNT(serve_number) AS serve_count
            FROM atp_points_enhanced
            GROUP BY match_id
            HAVING COUNT(*) >= 80
        )
        SELECT
            match_id, pt_count, rally_count, serve_count
        FROM match_stats
        WHERE CAST(LEFT(match_id, 8) AS INTEGER) >= 20100101
          AND CAST(LEFT(match_id, 8) AS INTEGER) <= 20251231
        ORDER BY rally_count DESC, pt_count DESC
    """
    qualifying = con.execute(qualifying_query).fetchall()
    total_qualifying = len(qualifying)
    print(f"Matches qualifying (80+ pts, 2010-2025): {total_qualifying}")

    if total_qualifying < 3:
        print("ERROR: Not enough qualifying matches.")
        sys.exit(1)

    # ── Step 2: Pick 3 randomly, preferring matches with good data coverage ──
    # Use SQL random selection for true randomness
    pick_query = """
        WITH match_stats AS (
            SELECT
                match_id,
                COUNT(*) AS pt_count,
                COUNT(rally_length) AS rally_count,
                COUNT(serve_number) AS serve_count
            FROM atp_points_enhanced
            GROUP BY match_id
            HAVING COUNT(*) >= 80
        )
        SELECT match_id, pt_count, rally_count, serve_count
        FROM match_stats
        WHERE CAST(LEFT(match_id, 8) AS INTEGER) >= 20100101
          AND CAST(LEFT(match_id, 8) AS INTEGER) <= 20251231
          AND rally_count >= pt_count * 0.5
        ORDER BY RANDOM()
        LIMIT 3
    """
    selected = con.execute(pick_query).fetchall()

    # Fallback if filter is too strict
    if len(selected) < 3:
        pick_query_fallback = """
            WITH match_stats AS (
                SELECT
                    match_id,
                    COUNT(*) AS pt_count,
                    COUNT(rally_length) AS rally_count,
                    COUNT(serve_number) AS serve_count
                FROM atp_points_enhanced
                GROUP BY match_id
                HAVING COUNT(*) >= 80
            )
            SELECT match_id, pt_count, rally_count, serve_count
            FROM match_stats
            WHERE CAST(LEFT(match_id, 8) AS INTEGER) >= 20100101
              AND CAST(LEFT(match_id, 8) AS INTEGER) <= 20251231
            ORDER BY RANDOM()
            LIMIT 3
        """
        selected = con.execute(pick_query_fallback).fetchall()

    print(f"\nSelected {len(selected)} matches:")

    # ── Step 3: Try to get surface from atp_matches ──
    # Build lookup keyed by (player names sorted, year) for fuzzy matching
    surface_cache = {}
    try:
        surfaces = con.execute("""
            SELECT winner_name, loser_name, tourney_date, surface, tourney_name, score
            FROM atp_matches
            WHERE tourney_date >= 20100101
        """).fetchall()
        for row in surfaces:
            winner, loser, tdate, surface, tname, score = row
            year = int(str(tdate)[:4])
            # Key by sorted player names + year for robust matching
            pair = tuple(sorted([winner, loser]))
            key_exact = (pair, year, (tname or "").lower())
            key_broad = (pair, year)
            entry = (surface or "Unknown", tname or "", score or "")
            surface_cache[key_exact] = entry
            # Broader key as fallback (just players + year)
            if key_broad not in surface_cache:
                surface_cache[key_broad] = entry
    except Exception as e:
        print(f"  Warning: could not load atp_matches for surface lookup: {e}")

    # ── Step 4: Extract points for each match ──
    matches_out = []
    total_rally_real = 0
    total_rally_default = 0

    for match_id, pt_count, rally_count, serve_count in selected:
        info = parse_match_id(match_id)
        print(f"  {info['playerA']} vs {info['playerB']} ({info['year']}) — {pt_count} points")

        # Try to find surface from atp_matches
        surface = "Unknown"
        final_score = ""
        tourney_display = info["tournament"]
        pair = tuple(sorted([info["playerA"], info["playerB"]]))
        year = info["year"]

        # Try exact match (players + year + tourney name)
        tourney_lower = info["tournament"].lower()
        exact_key = (pair, year, tourney_lower)
        broad_key = (pair, year)

        if exact_key in surface_cache:
            surface, tourney_display_alt, final_score = surface_cache[exact_key]
            if tourney_display_alt:
                tourney_display = tourney_display_alt
        elif broad_key in surface_cache:
            surface, tourney_display_alt, final_score = surface_cache[broad_key]
            if tourney_display_alt:
                tourney_display = tourney_display_alt

        # Fetch all points in order
        points_rows = con.execute("""
            SELECT Pt, Svr, PtWinner, rally_length, serve_number, Pts
            FROM atp_points_enhanced
            WHERE match_id = ?
            ORDER BY Pt ASC
        """, [match_id]).fetchall()

        points = []
        for i, (pt_num, svr, pt_winner, rally_len, serve_num, pts_score) in enumerate(points_rows):
            # Svr: 1 = first-named player (A), 2 = second-named (B)
            server = "A" if svr == 1 else "B"
            winner = "A" if pt_winner == 1 else "B"

            # Rally length: use actual if available, else default to 3
            if rally_len is not None:
                rl = int(rally_len)
                total_rally_real += 1
            else:
                rl = 3
                total_rally_default += 1

            # Serve number: 1 = first serve, 2 = second serve
            is_first = True
            if serve_num is not None:
                is_first = (serve_num == 1)

            # No serve speed in km/h available in this dataset
            serve_speed = None

            # Score before this point
            point_score = str(pts_score) if pts_score else ""

            points.append({
                "pointIndex": i,
                "server": server,
                "winner": winner,
                "rallyLength": rl,
                "serveSpeed": serve_speed,
                "isFirstServe": is_first,
                "pointScore": point_score,
            })

        matches_out.append({
            "matchId": match_id,
            "playerA": info["playerA"],
            "playerB": info["playerB"],
            "year": info["year"],
            "surface": surface,
            "tournament": tourney_display,
            "finalScore": final_score,
            "p0_A": 0.64,
            "p0_B": 0.64,
            "points": points,
        })

    con.close()

    # ── Step 5: Write output ──
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(matches_out, f, indent=2)

    total_points = total_rally_real + total_rally_default
    pct_real = (total_rally_real / total_points * 100) if total_points > 0 else 0
    pct_default = (total_rally_default / total_points * 100) if total_points > 0 else 0

    print(f"\n{'='*60}")
    print(f"Total qualifying matches: {total_qualifying}")
    print(f"Matches exported: {len(matches_out)}")
    print(f"Total points across all matches: {total_points}")
    print(f"  Rally length from data: {total_rally_real} ({pct_real:.1f}%)")
    print(f"  Rally length defaulted to 3: {total_rally_default} ({pct_default:.1f}%)")
    print(f"Output written to: {OUTPUT_PATH}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
