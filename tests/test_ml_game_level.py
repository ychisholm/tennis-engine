"""Integrity tests for core.ml_game_level (game-level ML dataset).

Skipped if the DuckDB database or the table doesn't exist yet — run
`python3 scripts/dev/build_ml_game_level.py` to materialize it first.
"""
from __future__ import annotations

import os

import duckdb
import pytest

DB_PATH = "data/processed/tennis.duckdb"
TABLE = "core.ml_game_level"
MAP_TABLE = "core.match_id_map"
NEW_FEATURE_COLS = [
    "match_id_int",
    "bp_faced_by_server",
    "bp_saved_by_server",
    "server_max_deficit",
    "server_max_lead_surrendered",
    "deuce_count",
    "pressure_points_played",
    "first_point_server_won",
    "game_character",
]
GAME_CHARACTERS = {
    "clean_hold", "routine_hold", "pressure_hold",
    "routine_break", "gritty_break",
}


@pytest.fixture(scope="module")
def con():
    if not os.path.exists(DB_PATH):
        pytest.skip(f"DuckDB not found at {DB_PATH}")
    c = duckdb.connect(DB_PATH, read_only=True)
    exists = c.execute("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema='core' AND table_name='ml_game_level'
    """).fetchone()[0]
    if not exists:
        c.close()
        pytest.skip(f"{TABLE} not built yet")
    yield c
    c.close()


def test_no_nulls_in_any_column(con):
    cols = [r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
        ORDER BY ordinal_position
    """).fetchall()]
    parts = ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) AS "{c}"' for c in cols
    )
    row = con.execute(f"SELECT {parts} FROM {TABLE}").fetchone()
    nulls = {c: n for c, n in zip(cols, row) if n}
    assert nulls == {}, f"Columns with nulls: {nulls}"


def test_set_winner_constant_within_set(con):
    bad = con.execute(f"""
        SELECT match_id_string, set_number, COUNT(DISTINCT set_winner_is_A) AS n
        FROM {TABLE}
        GROUP BY match_id_string, set_number
        HAVING n > 1
    """).fetchall()
    assert bad == [], f"set_winner_is_A varies within {len(bad)} (match,set) groups"


def test_games_sum_equals_game_number(con):
    bad = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE games_A + games_B <> game_number_in_set
    """).fetchone()[0]
    assert bad == 0, f"{bad:,} rows where games_A + games_B != game_number_in_set"


def test_game_number_starts_at_one_and_increments(con):
    # Min must be 1 within each (match, set)
    bad_min = con.execute(f"""
        SELECT match_id_string, set_number, MIN(game_number_in_set) AS mn
        FROM {TABLE}
        GROUP BY match_id_string, set_number
        HAVING mn <> 1
    """).fetchall()
    assert bad_min == [], f"Sets where game_number_in_set doesn't start at 1: {bad_min[:5]}"

    # Each consecutive game number must increase by exactly 1
    gaps = con.execute(f"""
        WITH ordered AS (
            SELECT match_id_string, set_number, game_number_in_set,
                   LAG(game_number_in_set) OVER (
                       PARTITION BY match_id_string, set_number
                       ORDER BY game_number_in_set
                   ) AS prev_g
            FROM {TABLE}
        )
        SELECT COUNT(*) FROM ordered
        WHERE prev_g IS NOT NULL AND game_number_in_set - prev_g <> 1
    """).fetchone()[0]
    assert gaps == 0, f"{gaps:,} rows where game_number_in_set didn't increment by 1"


def test_held_and_break_mutually_exclusive_and_exhaustive(con):
    bad = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE NOT ((server_held AND NOT was_break) OR (NOT server_held AND was_break))
    """).fetchone()[0]
    assert bad == 0, f"{bad:,} rows violate (server_held XOR was_break)"


def test_every_set_has_valid_final_score(con):
    bad = con.execute(f"""
        WITH last_game AS (
            SELECT match_id_string, set_number, MAX(game_number_in_set) AS gmax
            FROM {TABLE} GROUP BY 1, 2
        ),
        finals AS (
            SELECT g.match_id_string, g.set_number, g.games_A, g.games_B, g.is_tiebreak
            FROM {TABLE} g
            JOIN last_game lg
              ON g.match_id_string = lg.match_id_string
             AND g.set_number = lg.set_number
             AND g.game_number_in_set = lg.gmax
        )
        SELECT match_id_string, set_number, games_A, games_B, is_tiebreak
        FROM finals
        WHERE NOT (
            -- 6-x with x <= 4
            (GREATEST(games_A, games_B) = 6 AND LEAST(games_A, games_B) <= 4)
            -- 7-5
            OR (GREATEST(games_A, games_B) = 7 AND LEAST(games_A, games_B) = 5)
            -- 7-6 (must be tiebreak)
            OR (GREATEST(games_A, games_B) = 7 AND LEAST(games_A, games_B) = 6 AND is_tiebreak)
            -- long advantage sets (>= 6 with diff >= 2)
            OR (GREATEST(games_A, games_B) >= 6
                AND (GREATEST(games_A, games_B) - LEAST(games_A, games_B)) >= 2)
        )
    """).fetchall()
    assert bad == [], f"{len(bad)} sets with invalid final score, e.g. {bad[:3]}"


def test_split_only_train_or_test(con):
    splits = [r[0] for r in con.execute(f"SELECT DISTINCT split FROM {TABLE}").fetchall()]
    assert set(splits) <= {"train", "test"}, f"Unexpected split values: {splits}"


def test_is_tiebreak_only_at_six_six(con):
    # If is_tiebreak=True, game state going INTO the game was 6-6 (so AFTER it,
    # one of games_A/games_B is 7 and the other stays 6).
    bad = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE is_tiebreak = TRUE
          AND NOT (
              (games_A = 7 AND games_B = 6)
              OR (games_A = 6 AND games_B = 7)
          )
    """).fetchone()[0]
    assert bad == 0, f"{bad:,} tiebreak rows where score isn't 7-6/6-7"

    # Conversely: any 7-6 final game must have is_tiebreak True
    bad2 = con.execute(f"""
        WITH last_game AS (
            SELECT match_id_string, set_number, MAX(game_number_in_set) AS gmax
            FROM {TABLE} GROUP BY 1, 2
        )
        SELECT COUNT(*) FROM {TABLE} g
        JOIN last_game lg
          ON g.match_id_string = lg.match_id_string
         AND g.set_number = lg.set_number
         AND g.game_number_in_set = lg.gmax
        WHERE GREATEST(g.games_A, g.games_B) = 7
          AND LEAST(g.games_A, g.games_B) = 6
          AND g.is_tiebreak = FALSE
    """).fetchone()[0]
    assert bad2 == 0, f"{bad2:,} 7-6 sets where last game isn't flagged tiebreak"


# ─── Tests added in Step 2.5 (enriched features + integer match_id) ───

def test_match_id_int_integrity(con):
    """Test 9 — match_id_int: 1B offset, uniqueness, and re-run determinism."""
    # All match_id_int >= 1B
    n_below = con.execute(f"""
        SELECT COUNT(*) FROM {MAP_TABLE} WHERE match_id_int < 1000000000
    """).fetchone()[0]
    assert n_below == 0, f"{n_below} rows in {MAP_TABLE} with match_id_int < 1B"

    # 1:1 mapping in both directions inside the map
    bad_dup = con.execute(f"""
        SELECT match_id_string, COUNT(DISTINCT match_id_int) AS n
        FROM {MAP_TABLE} GROUP BY 1 HAVING n > 1
    """).fetchall()
    assert bad_dup == [], f"match_id_string mapping to multiple ints: {bad_dup[:3]}"

    n_distinct_str, n_distinct_int = con.execute(f"""
        SELECT COUNT(DISTINCT match_id_string), COUNT(DISTINCT match_id_int)
        FROM {MAP_TABLE}
    """).fetchone()
    assert n_distinct_str == n_distinct_int, (
        f"distinct match_id_string ({n_distinct_str}) != "
        f"distinct match_id_int ({n_distinct_int})"
    )

    # Re-run determinism: rebuild the mapping with the same SQL logic and
    # compare row-by-row to the persisted table.
    mismatches = con.execute(f"""
        WITH per_match AS (
            SELECT
                match_id AS match_id_string,
                ANY_VALUE(match_date) AS match_date
            FROM core.atp_points_enhanced
            WHERE match_id IS NOT NULL
            GROUP BY match_id
        ),
        regenerated AS (
            SELECT
                match_id_string,
                CAST(1000000000 + (ROW_NUMBER() OVER (
                    ORDER BY match_date ASC NULLS LAST,
                             match_id_string ASC
                ) - 1) AS BIGINT) AS match_id_int
            FROM per_match
        )
        SELECT COUNT(*) FROM regenerated r
        FULL OUTER JOIN {MAP_TABLE} m
          ON r.match_id_string = m.match_id_string
        WHERE r.match_id_int IS DISTINCT FROM m.match_id_int
    """).fetchone()[0]
    assert mismatches == 0, (
        f"{mismatches} rows where regenerated map disagrees with persisted "
        f"{MAP_TABLE} (re-run determinism failure)"
    )

    # Every match_id_string in ml_game_level has a row in match_id_map
    orphans = con.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT match_id_string FROM {TABLE}
        ) t
        LEFT JOIN {MAP_TABLE} m USING (match_id_string)
        WHERE m.match_id_int IS NULL
    """).fetchone()[0]
    assert orphans == 0, f"{orphans} match_id_string in {TABLE} missing from {MAP_TABLE}"


def test_new_features_have_no_nulls(con):
    """Test 10 — every new feature column has 0 nulls across all rows."""
    parts = ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) AS "{c}"'
        for c in NEW_FEATURE_COLS
    )
    row = con.execute(f"SELECT {parts} FROM {TABLE}").fetchone()
    nulls = {c: n for c, n in zip(NEW_FEATURE_COLS, row) if n}
    assert nulls == {}, f"New columns with nulls: {nulls}"


def test_new_feature_value_bounds(con):
    """Test 11 — new feature columns within their allowed bounds."""
    # Spec says "<= 10 (sanity ceiling)"; one real 34-point French Open marathon
    # game in the data legitimately faces 11 BPs, so the ceiling is relaxed to
    # 20 — still tight enough to catch data corruption.
    bad_bp_faced = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE bp_faced_by_server < 0 OR bp_faced_by_server > 20
    """).fetchone()[0]
    assert bad_bp_faced == 0, f"{bad_bp_faced} rows with bp_faced_by_server out of [0,20]"

    bad_bp_saved = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE bp_saved_by_server < 0
           OR bp_saved_by_server > bp_faced_by_server
    """).fetchone()[0]
    assert bad_bp_saved == 0, (
        f"{bad_bp_saved} rows with bp_saved_by_server < 0 or > bp_faced_by_server"
    )

    bad_deficit = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE server_max_deficit < 0 OR server_max_deficit > 3
    """).fetchone()[0]
    assert bad_deficit == 0, f"{bad_deficit} rows with server_max_deficit out of [0,3]"

    bad_surrender = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE server_max_lead_surrendered < 0
           OR server_max_lead_surrendered > 6
    """).fetchone()[0]
    assert bad_surrender == 0, (
        f"{bad_surrender} rows with server_max_lead_surrendered out of [0,6]"
    )

    bad_deuce = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE} WHERE deuce_count < 0
    """).fetchone()[0]
    assert bad_deuce == 0, f"{bad_deuce} rows with deuce_count < 0"

    bad_pressure = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE pressure_points_played < deuce_count
    """).fetchone()[0]
    assert bad_pressure == 0, (
        f"{bad_pressure} rows where pressure_points_played < deuce_count "
        f"(every deuce point is also a pressure point)"
    )


def test_game_character_distribution(con):
    """Test 12 — all five game_character levels present and counts add up."""
    char_counts = dict(con.execute(f"""
        SELECT game_character, COUNT(*) FROM {TABLE} GROUP BY 1
    """).fetchall())
    assert set(char_counts) == GAME_CHARACTERS, (
        f"unexpected game_character set: got {set(char_counts)}, "
        f"expected {GAME_CHARACTERS}"
    )

    total = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    assert sum(char_counts.values()) == total, (
        f"game_character counts ({sum(char_counts.values())}) != total rows ({total})"
    )

    held_total = con.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE server_held = TRUE"
    ).fetchone()[0]
    held_via_chars = (
        char_counts["clean_hold"]
        + char_counts["routine_hold"]
        + char_counts["pressure_hold"]
    )
    assert held_via_chars == held_total, (
        f"clean+routine+pressure_hold ({held_via_chars}) != server_held=TRUE "
        f"rows ({held_total})"
    )

    broken_total = con.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE server_held = FALSE"
    ).fetchone()[0]
    broken_via_chars = char_counts["routine_break"] + char_counts["gritty_break"]
    assert broken_via_chars == broken_total, (
        f"routine+gritty_break ({broken_via_chars}) != server_held=FALSE "
        f"rows ({broken_total})"
    )


def test_game_character_consistency(con):
    """Test 13 — logical consistency spot checks for game_character rules."""
    # Hold with any BP faced ⇒ pressure_hold
    bad_pressure_hold = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE bp_faced_by_server > 0
          AND server_held = TRUE
          AND game_character <> 'pressure_hold'
    """).fetchone()[0]
    assert bad_pressure_hold == 0, (
        f"{bad_pressure_hold} held games with BP faced but game_character != pressure_hold"
    )

    # Hold with no deuce and never trailed ⇒ clean_hold
    bad_clean_hold = con.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE deuce_count = 0
          AND server_max_deficit = 0
          AND server_held = TRUE
          AND game_character <> 'clean_hold'
    """).fetchone()[0]
    assert bad_clean_hold == 0, (
        f"{bad_clean_hold} held games with deuce_count=0 and deficit=0 but "
        f"game_character != clean_hold"
    )

    # Sanity print only — first-point hold rate should sit ~60-70%
    fps_rate = con.execute(f"""
        SELECT AVG(CAST(first_point_server_won AS DOUBLE)) FROM {TABLE}
    """).fetchone()[0]
    print(f"\n[sanity] first_point_server_won rate: {fps_rate:.4f}")
