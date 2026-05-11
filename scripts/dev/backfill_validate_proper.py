#!/usr/bin/env python3
"""
Proper validator for live.backfill_points.

Respects the actual structure of backfill data: the API systematically
omits the game-winning point — the last shown point of each game is
typically "one point before game end" (e.g. 40-15 with home about to win).
Per-point `point_winner` is correct for each shown point; the game winner
must be INFERRED from the last shown point's score.

Validation logic per match:
  1. Pull all rows from live.backfill_points ordered by point_num.
  2. Group by (set_num, game_num).
  3. Within each game, verify internal consistency: each point's score
     delta matches its point_winner, and progression is +1 point per step.
  4. Infer game winner from last shown point (40/AD side wins; tiebreak:
     higher integer side wins).
  5. Tally games per set; derive set winners and final score.
  6. Compare derived final score to recorded final score from
     live.match_polls. Report all anomalies.

Strictly read-only.

Usage:
    .venv/bin/python scripts/dev/backfill_validate_proper.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


_REGULAR_RANK = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4, "A": 4}


def _is_regular(score: str) -> bool:
    return score in _REGULAR_RANK


def _is_tiebreak_score(hp: str, ap: str) -> bool:
    """A pair where at least one side is an integer outside the regular vocab."""
    for v in (hp, ap):
        if v in _REGULAR_RANK:
            continue
        try:
            int(v)
            return True
        except (ValueError, TypeError):
            return False
    return False


def _format_recorded_final_score(poll_row: dict) -> str:
    parts: list[str] = []
    for n in (1, 2, 3, 4, 5):
        h = poll_row.get(f"home_period{n}")
        a = poll_row.get(f"away_period{n}")
        if h is None or a is None:
            continue
        parts.append(f"{h}-{a}")
    return " ".join(parts)


def _infer_game_winner(last_hp: str, last_ap: str) -> str | None:
    """
    From the last shown point's score, infer who won the (truncated)
    game-winning point. Returns 'home', 'away', or None if ambiguous.
    """
    if _is_tiebreak_score(last_hp, last_ap):
        try:
            h = int(last_hp)
            a = int(last_ap)
        except (ValueError, TypeError):
            return None
        if h > a:
            return "home"
        if a > h:
            return "away"
        return None

    if last_hp in ("AD", "A"):
        return "home"
    if last_ap in ("AD", "A"):
        return "away"
    if last_hp == "40" and last_ap != "40":
        return "home"
    if last_ap == "40" and last_hp != "40":
        return "away"
    return None  # 40-40, 30-30, or anything else mid-game


def _validate_point_progression(points: list[dict]) -> list[str]:
    """
    Walk a single game's points and flag illegal score deltas. Returns a
    list of human-readable anomaly descriptions.
    """
    anomalies: list[str] = []
    prev_hp = "0"
    prev_ap = "0"
    is_tb = any(_is_tiebreak_score(p["home_point"], p["away_point"]) for p in points)

    for p in points:
        hp = p["home_point"]
        ap = p["away_point"]
        winner = p["point_winner"]

        if is_tb:
            try:
                ph = int(prev_hp) if prev_hp not in ("AD", "A") else None
                pa = int(prev_ap) if prev_ap not in ("AD", "A") else None
                ch = int(hp)
                ca = int(ap)
            except (ValueError, TypeError):
                anomalies.append(
                    f"pt#{p['point_num']}: non-integer tiebreak score "
                    f"{prev_hp}-{prev_ap} → {hp}-{ap}"
                )
                prev_hp, prev_ap = hp, ap
                continue
            if ph is None or pa is None:
                prev_hp, prev_ap = hp, ap
                continue
            dh = ch - ph
            da = ca - pa
            total_delta = dh + da
            if total_delta != 1 or dh < 0 or da < 0:
                anomalies.append(
                    f"pt#{p['point_num']}: tiebreak score did not advance by 1 "
                    f"({prev_hp}-{prev_ap} → {hp}-{ap})"
                )
            elif dh == 1 and winner != "home":
                anomalies.append(
                    f"pt#{p['point_num']}: home advanced ({prev_hp}-{prev_ap} → "
                    f"{hp}-{ap}) but point_winner={winner}"
                )
            elif da == 1 and winner != "away":
                anomalies.append(
                    f"pt#{p['point_num']}: away advanced ({prev_hp}-{prev_ap} → "
                    f"{hp}-{ap}) but point_winner={winner}"
                )
        else:
            # Regular-game progression check. Just verify point_winner matches
            # which side's score advanced (or who pushed opponent's AD back to 40).
            ph_rank = _REGULAR_RANK.get(prev_hp, -1)
            pa_rank = _REGULAR_RANK.get(prev_ap, -1)
            ch_rank = _REGULAR_RANK.get(hp, -1)
            ca_rank = _REGULAR_RANK.get(ap, -1)
            if ph_rank < 0 or pa_rank < 0 or ch_rank < 0 or ca_rank < 0:
                anomalies.append(
                    f"pt#{p['point_num']}: unrecognized regular-game score "
                    f"{prev_hp}-{prev_ap} → {hp}-{ap}"
                )
            else:
                home_advanced = ch_rank > ph_rank or ca_rank < pa_rank
                away_advanced = ca_rank > pa_rank or ch_rank < ph_rank
                if winner == "home" and not home_advanced:
                    anomalies.append(
                        f"pt#{p['point_num']}: point_winner=home but score "
                        f"{prev_hp}-{prev_ap} → {hp}-{ap} doesn't reflect that"
                    )
                if winner == "away" and not away_advanced:
                    anomalies.append(
                        f"pt#{p['point_num']}: point_winner=away but score "
                        f"{prev_hp}-{prev_ap} → {hp}-{ap} doesn't reflect that"
                    )
        prev_hp, prev_ap = hp, ap

    return anomalies


def _validate_match(points: list[dict], recorded: str) -> dict:
    """
    Returns a dict with:
      derived_final_score, final_score_match, total_points,
      games_per_set: list of (home_games, away_games),
      ambiguous_games: int (games where winner couldn't be inferred),
      progression_anomalies: list[str],
      set_breakdown: list of dicts per set.
    """
    by_game: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for p in points:
        by_game[(p["set_num"], p["game_num"])].append(p)

    # Order games deterministically: by appearance order in points (the script
    # writes them sequentially), preserving set then game ordering.
    sorted_keys = sorted(by_game.keys())

    games_per_set: dict[int, list[str | None]] = defaultdict(list)
    progression_anomalies: list[str] = []
    set_breakdown: list[dict] = []
    ambiguous_games = 0

    for set_num, game_num in sorted_keys:
        game_points = by_game[(set_num, game_num)]
        progression_anomalies.extend(
            f"set {set_num} game {game_num}: {a}"
            for a in _validate_point_progression(game_points)
        )
        last = game_points[-1]
        gw = _infer_game_winner(last["home_point"], last["away_point"])
        if gw is None:
            ambiguous_games += 1
        games_per_set[set_num].append(gw)

    derived_parts: list[str] = []
    for s_num in sorted(games_per_set.keys()):
        wins = games_per_set[s_num]
        h = sum(1 for w in wins if w == "home")
        a = sum(1 for w in wins if w == "away")
        amb = sum(1 for w in wins if w is None)
        derived_parts.append(f"{h}-{a}")
        set_breakdown.append({
            "set_num": s_num,
            "home_games": h,
            "away_games": a,
            "ambiguous_games": amb,
            "total_games": len(wins),
        })

    derived = " ".join(derived_parts)
    return {
        "derived_final_score": derived,
        "final_score_match": derived == recorded,
        "total_points": len(points),
        "ambiguous_games": ambiguous_games,
        "progression_anomalies": progression_anomalies,
        "set_breakdown": set_breakdown,
    }


def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set.", file=sys.stderr)
        return 1

    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT match_id
                FROM live.backfill_points
                WHERE ingestion_source = 'backfill_validate_today'
                ORDER BY match_id
            """)
            match_ids = [r["match_id"] for r in cur.fetchall()]

        print(f"Found {len(match_ids)} matches in live.backfill_points (today's experiment).")
        print("=" * 92)

        match_counter: Counter = Counter()
        rows_summary: list[dict] = []
        total_anomalies = 0

        for mid in match_ids:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT point_num, set_num, game_num,
                           home_point, away_point, point_winner
                    FROM live.backfill_points
                    WHERE match_id = %s
                      AND ingestion_source = 'backfill_validate_today'
                    ORDER BY point_num
                    """,
                    (mid,),
                )
                points = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    """
                    SELECT home_period1, away_period1,
                           home_period2, away_period2,
                           home_period3, away_period3
                    FROM live.match_polls
                    WHERE match_id = %s AND status = 'finished'
                    ORDER BY polled_at DESC LIMIT 1
                    """,
                    (mid,),
                )
                poll = cur.fetchone()
            recorded = _format_recorded_final_score(dict(poll)) if poll else ""

            result = _validate_match(points, recorded)
            total_anomalies += len(result["progression_anomalies"])
            match_counter["match" if result["final_score_match"] else "mismatch"] += 1

            rows_summary.append({
                "match_id": mid,
                "points": result["total_points"],
                "derived": result["derived_final_score"],
                "recorded": recorded,
                "match": result["final_score_match"],
                "ambiguous_games": result["ambiguous_games"],
                "anomaly_count": len(result["progression_anomalies"]),
                "set_breakdown": result["set_breakdown"],
                "progression_anomalies": result["progression_anomalies"],
            })

        header = (
            f"{'match_id':>10}  {'pts':>4}  "
            f"{'derived':<22} {'recorded':<22} {'=':>2}  "
            f"{'amb':>4} {'anom':>5}"
        )
        print(header)
        print("-" * len(header))
        for r in rows_summary:
            print(
                f"{r['match_id']:>10}  {r['points']:>4}  "
                f"{r['derived']:<22} {r['recorded']:<22} "
                f"{('T' if r['match'] else 'F'):>2}  "
                f"{r['ambiguous_games']:>4} {r['anomaly_count']:>5}"
            )

        # Detailed dump of any mismatches
        mismatches = [r for r in rows_summary if not r["match"]]
        if mismatches:
            print()
            print("Mismatches — per-set breakdown:")
            for r in mismatches:
                print(f"  match {r['match_id']}:  derived='{r['derived']}'  "
                      f"recorded='{r['recorded']}'")
                for sb in r["set_breakdown"]:
                    print(
                        f"    set {sb['set_num']}: "
                        f"home={sb['home_games']} away={sb['away_games']} "
                        f"ambiguous={sb['ambiguous_games']} "
                        f"total_games={sb['total_games']}"
                    )

        if total_anomalies > 0:
            print()
            print(f"Progression anomalies found: {total_anomalies}")
            print("(first 20 across all matches)")
            count = 0
            for r in rows_summary:
                for a in r["progression_anomalies"]:
                    if count >= 20:
                        break
                    print(f"  match {r['match_id']}: {a}")
                    count += 1
                if count >= 20:
                    break

        print()
        print("=" * 92)
        print("Roll-up")
        print("-------")
        print(f"  total matches             : {len(match_ids)}")
        print(f"  final score reconciled    : {match_counter['match']}/{len(match_ids)}")
        print(f"  final score mismatched    : {match_counter['mismatch']}/{len(match_ids)}")
        print(f"  total progression anomalies: {total_anomalies}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
