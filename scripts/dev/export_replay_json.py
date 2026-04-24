#!/usr/bin/env python3
"""
export_replay_json.py — Export main.live_match_log to debug_ui/public/replay_matches.json
in the shape expected by App.jsx's replay mode.

Usage (from project root):
    .venv/bin/python scripts/export_replay_json.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

_ROOT       = Path(__file__).resolve().parents[2]
_DB_PATH    = _ROOT / "data" / "processed" / "tennis.duckdb"
_OUT_PATH   = _ROOT / "debug_ui" / "public" / "replay_matches.json"

_SIDE = {"home": "A", "away": "B"}


def main() -> None:
    conn = duckdb.connect(str(_DB_PATH), read_only=True)
    try:
        rows = conn.execute("""
            SELECT
                match_id,
                player_a,
                player_b,
                ts,
                set_num,
                game_num,
                point_num,
                home_point,
                away_point,
                server,
                point_winner,
                is_ace,
                is_double_fault,
                model_prob_a,
                bookmaker_prob_a,
                edge
            FROM main.live_match_log
            ORDER BY match_id, point_num
        """).fetchall()
    finally:
        conn.close()

    # Group by match_id, preserving insertion order (rows already sorted).
    by_match: dict[int, list] = defaultdict(list)
    meta: dict[int, dict] = {}

    for row in rows:
        (match_id, player_a, player_b, ts,
         set_num, game_num, point_num,
         home_point, away_point, server, point_winner,
         is_ace, is_double_fault,
         model_prob_a, bookmaker_prob_a, edge) = row

        if match_id not in meta:
            year = ts.year if ts is not None else None
            meta[match_id] = {
                "matchId":    match_id,
                "playerA":    player_a,
                "playerB":    player_b,
                "tournament": None,
                "year":       year,
                "surface":    None,
                "finalScore": None,
                "p0_A":       None,
                "p0_B":       None,
            }

        by_match[match_id].append({
            "server":          _SIDE.get(server, server),
            "winner":          _SIDE.get(point_winner, point_winner),
            "rallyLength":     None,
            "serveSpeed":      None,
            "isFirstServe":    None,
            "homePoint":       home_point,
            "awayPoint":       away_point,
            "setNum":          set_num,
            "gameNum":         game_num,
            "pointNum":        point_num,
            "isAce":           bool(is_ace) if is_ace is not None else False,
            "isDoubleFault":   bool(is_double_fault) if is_double_fault is not None else False,
            "modelProbA":      model_prob_a,
            "bookmakerProbA":  bookmaker_prob_a,
            "edge":            edge,
        })

    output = []
    for match_id, points in by_match.items():
        entry = dict(meta[match_id])
        entry["points"] = points
        output.append(entry)

    _OUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    total_points = sum(len(m["points"]) for m in output)
    print(f"Exported {len(output)} match(es), {total_points} total points")
    print(f"→ {_OUT_PATH}")


if __name__ == "__main__":
    main()
