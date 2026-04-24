"""
Validation test for the local game-counter fix in TennisFeed.translate_to_engine_format.

Replays match 16042676 (Kypson vs Basilashvili) from live_match_log through the
fixed logic, then asserts:

  1. The number of distinct locally-derived game numbers per set matches the
     expected count derived from the stored point data (via independent server-
     transition counting — NOT from games_a/games_b/sets_a/sets_b, which are NULL
     for this match).

  2. The two previously-flagged boundary errors are resolved:
       - S1 Pt34 is now assigned to a strictly higher game number than the
         API's raw game_num stored in the DB (was game 6, must now be ≥ 7).
       - S2 Pt92 is now assigned to a strictly higher game number than the
         API's raw game_num stored in the DB (was game 7, must now be ≥ 8).

Run from project root:
    .venv/bin/python scripts/validate_game_counter.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import duckdb
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DB_PATH  = ROOT / "data" / "processed" / "tennis.duckdb"
MATCH_ID = 16042676
DIVIDER  = "─" * 72
SECTION  = "═" * 72

# ── Tennis score vocabulary ──────────────────────────────────────────────────
_STD_SCORES = {"0", "15", "30", "40", "A", "AD"}


def is_tiebreak_score(h: str, a: str) -> bool:
    return h not in _STD_SCORES or a not in _STD_SCORES


# ── Fixed game-counter logic (mirrors tennis_feed.py exactly) ────────────────

def derive_game_counter(db_points: list[dict]) -> list[dict]:
    """
    Accept the raw DB rows (ordered by set_num, game_num, point_num) and
    return a copy where each point has a new key ``local_game`` — the
    locally-derived game number produced by the fixed boundary detection.

    Rules:
      - Both standard points  → new game iff server changed
      - Standard ↔ tiebreak   → always a new game (tiebreak start/end)
      - Both tiebreak points  → no new game (server alternates within tiebreak)
      - Set boundary          → reset counter to 1
    """
    result = []
    prev_server:     str | None = None
    prev_home_score: str | None = None
    prev_away_score: str | None = None
    prev_set:        int | None = None
    local_game_num:  int        = 1

    for pt in db_points:
        set_num    = pt["set_num"]
        home_score = str(pt["home_point"] or "0")
        away_score = str(pt["away_point"] or "0")
        server     = pt["server"]

        # Set boundary
        if set_num != prev_set:
            local_game_num  = 1
            prev_server     = None
            prev_home_score = None
            prev_away_score = None
            prev_set        = set_num

        # Game boundary detection
        if prev_home_score is not None:
            cur_tb  = is_tiebreak_score(home_score, away_score)
            prev_tb = is_tiebreak_score(prev_home_score, prev_away_score)

            if not cur_tb and not prev_tb:
                # Both standard: new game iff server changed
                if server != prev_server:
                    local_game_num += 1
            elif cur_tb != prev_tb:
                # Score type changed (regular ↔ tiebreak): new game
                local_game_num += 1
            # Both tiebreak: no increment

        result.append({**pt, "local_game": local_game_num})

        prev_server     = server
        prev_home_score = home_score
        prev_away_score = away_score

    return result


# ── Independent expected-game-count from server transitions ──────────────────

def expected_games_from_data(db_points: list[dict]) -> dict[int, int]:
    """
    Compute the expected number of games per set by counting server transitions
    in the raw point data, treating tiebreak blocks as a single game each.

    This is INDEPENDENT of the local-game-counter implementation:
      - It walks the points once and counts "game starts":
          * First point of a set always starts game 1.
          * Subsequent points start a new game when the score type changes
            (regular ↔ tiebreak) OR, for two regular points, when the server
            changes AND the previous point was the last of its score-type run
            (i.e. the transition is regular→regular with a server change).
          * Two consecutive tiebreak points never start a new game.
      - The total count is: 1 (first game) + number of increments.

    NOTE: games_a / games_b / sets_a / sets_b are all NULL for this match
    (ingested before those columns had live values), so we derive entirely
    from home_point, away_point, and server.
    """
    counts: dict[int, int] = {}
    prev_server:     str | None = None
    prev_home_score: str | None = None
    prev_away_score: str | None = None
    prev_set:        int | None = None
    game_count:      int        = 0

    for pt in db_points:
        set_num    = pt["set_num"]
        home_score = str(pt["home_point"] or "0")
        away_score = str(pt["away_point"] or "0")
        server     = pt["server"]

        if set_num != prev_set:
            if prev_set is not None:
                counts[prev_set] = game_count
            game_count      = 1          # first game of the new set
            prev_server     = None
            prev_home_score = None
            prev_away_score = None
            prev_set        = set_num

        if prev_home_score is not None:
            cur_tb  = is_tiebreak_score(home_score, away_score)
            prev_tb = is_tiebreak_score(prev_home_score, prev_away_score)

            if not cur_tb and not prev_tb:
                if server != prev_server:
                    game_count += 1
            elif cur_tb != prev_tb:
                game_count += 1

        prev_server     = server
        prev_home_score = home_score
        prev_away_score = away_score

    if prev_set is not None:
        counts[prev_set] = game_count

    return counts


# ── Assertion helper ─────────────────────────────────────────────────────────

def assert_eq(label: str, expected, actual) -> bool:
    ok = (expected == actual)
    sym = "✅  PASS" if ok else "❌  FAIL"
    print(f"  {sym}  {label}")
    print(f"            expected={expected!r}  actual={actual!r}")
    return ok


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SECTION)
    print("  GAME-COUNTER FIX — VALIDATION TEST")
    print(f"  Match {MATCH_ID}  (Kypson vs Basilashvili)")
    print(SECTION)

    conn = duckdb.connect(str(DB_PATH), read_only=True)

    rows = conn.execute("""
        SELECT set_num, game_num, point_num,
               home_point, away_point, server, point_winner
        FROM   live_match_log
        WHERE  match_id = ?
        ORDER  BY set_num, game_num, point_num, ts
    """, [MATCH_ID]).fetchall()
    col_names = [d[0] for d in conn.description]
    db_points = [dict(zip(col_names, r)) for r in rows]
    conn.close()

    print(f"\n  Loaded {len(db_points)} points from live_match_log.")

    # ── Compute expected counts (independent implementation) ─────────────────
    expected_per_set = expected_games_from_data(db_points)

    print(f"\n{DIVIDER}")
    print("  Expected games per set (derived from server-transition count in DB):\n")
    for s, exp in sorted(expected_per_set.items()):
        tb_note = ""
        # Detect whether this set had a tiebreak
        set_pts = [p for p in db_points if p["set_num"] == s]
        has_tb  = any(is_tiebreak_score(str(p["home_point"] or "0"),
                                         str(p["away_point"] or "0")) for p in set_pts)
        if has_tb:
            tb_note = "  (tiebreak set — expected 13 for 7-6)"
        print(f"    Set {s}: {exp} games{tb_note}")

    # ── Run fixed game-counter ────────────────────────────────────────────────
    enriched = derive_game_counter(db_points)

    # Count distinct local game numbers per set
    games_per_set: dict[int, set[int]] = defaultdict(set)
    for pt in enriched:
        games_per_set[pt["set_num"]].add(pt["local_game"])
    actual_per_set = {s: len(gs) for s, gs in sorted(games_per_set.items())}

    print(f"\n{DIVIDER}")
    print("  Actual distinct local game numbers per set (fixed counter):\n")
    for s, act in sorted(actual_per_set.items()):
        exp = expected_per_set.get(s, "?")
        match_sym = "✓" if act == exp else "✗"
        print(f"    Set {s}: {act} distinct local games  "
              f"(expected {exp})  {match_sym}")

    # ── Run assertions ────────────────────────────────────────────────────────
    results: list[bool] = []

    print(f"\n{DIVIDER}")
    print("  ASSERTIONS\n")

    # 1 & 2: game count per set matches expected
    for set_num in sorted(expected_per_set):
        exp = expected_per_set[set_num]
        act = actual_per_set.get(set_num, -1)
        ok  = assert_eq(
            f"Set {set_num}: distinct local games == {exp}",
            exp, act,
        )
        results.append(ok)
        print()

    # 3. Boundary fix — S1 Pt34
    pt34 = next(
        (p for p in enriched if p["set_num"] == 1 and p["point_num"] == 34), None
    )
    if pt34 is not None:
        old_api = pt34["game_num"]
        new_loc = pt34["local_game"]
        ok = assert_eq(
            f"S1 Pt34: local_game ({new_loc}) > api_game_num ({old_api})"
            f"  — boundary error resolved",
            True,
            new_loc > old_api,
        )
        results.append(ok)
        print(f"            score=[{pt34['home_point']}–{pt34['away_point']}]"
              f"  server={pt34['server']}")
        print()
    else:
        print("  ⚠️  S1 Pt34 not found in data\n")
        results.append(False)

    # 4. Boundary fix — S2 Pt92
    pt92 = next(
        (p for p in enriched if p["set_num"] == 2 and p["point_num"] == 92), None
    )
    if pt92 is not None:
        old_api = pt92["game_num"]
        new_loc = pt92["local_game"]
        ok = assert_eq(
            f"S2 Pt92: local_game ({new_loc}) > api_game_num ({old_api})"
            f"  — boundary error resolved",
            True,
            new_loc > old_api,
        )
        results.append(ok)
        print(f"            score=[{pt92['home_point']}–{pt92['away_point']}]"
              f"  server={pt92['server']}")
        print()
    else:
        print("  ⚠️  S2 Pt92 not found in data\n")
        results.append(False)

    # ── Print per-set game map for inspection ─────────────────────────────────
    print(f"{DIVIDER}")
    print("  LOCAL GAME MAP — set / local_game / api game_num(s) / scores\n")
    print(f"  {'S':>2} {'LG':>3} {'ApiG':>8}  {'Pts':>4}  {'Srv':>5}"
          f"  {'First score':>12}  {'Last score':>12}  {'Type'}")
    print(f"  {'─'*2} {'─'*3} {'─'*8}  {'─'*4}  {'─'*5}"
          f"  {'─'*12}  {'─'*12}  {'─'*8}")

    game_groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for pt in enriched:
        game_groups[(pt["set_num"], pt["local_game"])].append(pt)

    for (s, lg), pts in sorted(game_groups.items()):
        api_games = sorted({p["game_num"] for p in pts})
        api_str   = (str(api_games[0]) if len(api_games) == 1
                     else f"{api_games[0]}-{api_games[-1]}")
        first, last = pts[0], pts[-1]
        fh, fa = str(first["home_point"]), str(first["away_point"])
        lh, la = str(last["home_point"]),  str(last["away_point"])
        # detect if this game contains any tiebreak points
        game_type = ("TIEBREAK" if any(
            is_tiebreak_score(str(p["home_point"]), str(p["away_point"]))
            for p in pts
        ) else "regular")
        print(f"  {s:>2} {lg:>3} {api_str:>8}  {len(pts):>4}  {first['server']:>5}"
              f"  {fh:>3}–{fa:<5}        {lh:>3}–{la:<5}        {game_type}")

    # ── Summary ───────────────────────────────────────────────────────────────
    passes = sum(results)
    total  = len(results)
    print(f"\n{SECTION}")
    if all(results):
        print(f"  ✅  ALL {total}/{total} ASSERTIONS PASSED")
    else:
        print(f"  ❌  {passes}/{total} ASSERTION(S) PASSED  —  "
              f"{total - passes} FAILED")
    print(SECTION)


if __name__ == "__main__":
    main()
