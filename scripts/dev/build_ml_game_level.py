"""Build core.ml_game_level (game-level ML dataset) and core.match_id_map.

One row per completed game in matches 2015-2025. Adds the integer match_id
mapping plus descriptive within-game features (BP faced/saved, max deficit,
max-lead-surrendered, deuce/pressure counts, first-point winner, character).

Source: core.atp_points_enhanced (point-level)
Outputs:
  - core.match_id_map (DuckDB table)
  - core.ml_game_level (DuckDB table)
  - data/processed/ml_game_level.parquet
"""
from __future__ import annotations

import os
import re
import subprocess
import duckdb

DB_PATH = "data/processed/tennis.duckdb"
PARQUET_PATH = "data/processed/ml_game_level.parquet"
REPORT_PATH = "data/processed/game_level_build_report.txt"

MATCH_ID_INT_OFFSET = 1_000_000_000


def build_match_id_map(con: duckdb.DuckDBPyConnection) -> None:
    """Create core.match_id_map with deterministic integer IDs starting at 1B.

    Ordering by (match_date ASC, match_id_string ASC) ensures the same input
    table always produces the same match_id_int -> match_id_string mapping.
    """
    con.execute(f"""
        CREATE OR REPLACE TABLE core.match_id_map AS
        WITH per_match AS (
            SELECT
                match_id                        AS match_id_string,
                ANY_VALUE(match_date)           AS match_date,
                ANY_VALUE(player1_name)         AS player_1_name,
                ANY_VALUE(player2_name)         AS player_2_name
            FROM core.atp_points_enhanced
            WHERE match_id IS NOT NULL
            GROUP BY match_id
        )
        SELECT
            match_id_string,
            CAST({MATCH_ID_INT_OFFSET}
                 + (ROW_NUMBER() OVER (
                        ORDER BY match_date ASC NULLS LAST,
                                 match_id_string ASC
                    ) - 1) AS BIGINT) AS match_id_int,
            match_date,
            player_1_name,
            player_2_name
        FROM per_match
        ORDER BY match_id_int
    """)


def build_ml_game_level(con: duckdb.DuckDBPyConnection) -> None:
    # ── Step 1: filtered raw point rows with set/game numbering ──
    con.execute("""
        CREATE OR REPLACE TEMP TABLE points_filtered AS
        SELECT
            match_id,
            match_date,
            player1_name AS player_A,
            player2_name AS player_B,
            (Set1 + Set2 + 1) AS set_number,
            (Gm1 + Gm2 + 1)   AS game_number_in_set,
            Pt,
            Svr,
            PtWinner,
            score_before,
            server_won_point,
            is_break_point,
            is_tiebreak
        FROM core.atp_points_enhanced
        WHERE match_date BETWEEN DATE '2015-01-01' AND DATE '2025-12-31'
          AND player1_name IS NOT NULL
          AND player2_name IS NOT NULL
          AND Set1 IS NOT NULL AND Set2 IS NOT NULL
          AND Gm1  IS NOT NULL AND Gm2  IS NOT NULL
          AND Svr  IS NOT NULL AND PtWinner IS NOT NULL
    """)

    # ── Step 2: per-game core aggregates (existing logic) ──
    con.execute("""
        CREATE OR REPLACE TEMP TABLE games_raw AS
        SELECT
            match_id,
            ANY_VALUE(match_date)  AS match_date,
            ANY_VALUE(player_A)    AS player_A,
            ANY_VALUE(player_B)    AS player_B,
            set_number,
            game_number_in_set,
            COUNT(*)               AS points_in_game,
            ARG_MAX(PtWinner, Pt)  AS game_winner_int,
            ARG_MIN(Svr, Pt)       AS game_server_int,
            BOOL_OR(is_tiebreak)   AS is_tiebreak
        FROM points_filtered
        GROUP BY match_id, set_number, game_number_in_set
    """)

    # ── Step 3: per-game NEW within-game features ──
    # For non-tiebreak games we map the score string to numeric (0/15/30/40/AD
    # → 0/1/2/3/4); for tiebreaks the mapping returns NULL and the deficit /
    # surrender / deuce / pressure counters naturally evaluate to 0 (none of
    # the literal score strings can occur in a tiebreak).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE points_aug AS
        SELECT
            match_id, set_number, game_number_in_set, Pt,
            score_before, is_tiebreak, is_break_point, server_won_point,
            CASE WHEN is_tiebreak THEN NULL ELSE
                CASE split_part(score_before, '-', 1)
                    WHEN '0'  THEN 0
                    WHEN '15' THEN 1
                    WHEN '30' THEN 2
                    WHEN '40' THEN 3
                    WHEN 'AD' THEN 4
                END
            END AS server_num,
            CASE WHEN is_tiebreak THEN NULL ELSE
                CASE split_part(score_before, '-', 2)
                    WHEN '0'  THEN 0
                    WHEN '15' THEN 1
                    WHEN '30' THEN 2
                    WHEN '40' THEN 3
                    WHEN 'AD' THEN 4
                END
            END AS returner_num
        FROM points_filtered
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE game_features AS
        WITH agg AS (
            SELECT
                match_id, set_number, game_number_in_set,
                CAST(SUM(CASE WHEN score_before IN ('0-40','15-40','30-40','40-AD')
                              THEN 1 ELSE 0 END) AS INTEGER) AS bp_faced_by_server,
                CAST(SUM(CASE WHEN score_before IN ('0-40','15-40','30-40','40-AD')
                              AND server_won_point THEN 1 ELSE 0 END) AS INTEGER) AS bp_saved_by_server,
                CAST(SUM(CASE WHEN score_before = '40-40' THEN 1 ELSE 0 END) AS INTEGER) AS deuce_count,
                CAST(SUM(CASE WHEN score_before IN ('30-30','40-40','AD-40','40-AD')
                              THEN 1 ELSE 0 END) AS INTEGER) AS pressure_points_played,
                ARG_MIN(server_won_point, Pt) AS first_point_server_won,
                LIST(returner_num - server_num ORDER BY Pt)
                    FILTER (WHERE server_num IS NOT NULL)  AS deficits,
                LIST(server_num - returner_num ORDER BY Pt)
                    FILTER (WHERE server_num IS NOT NULL)  AS leads
            FROM points_aug
            GROUP BY match_id, set_number, game_number_in_set
        )
        SELECT
            match_id, set_number, game_number_in_set,
            bp_faced_by_server,
            bp_saved_by_server,
            deuce_count,
            pressure_points_played,
            first_point_server_won,
            CAST(COALESCE(GREATEST(0, list_max(deficits)), 0) AS INTEGER) AS server_max_deficit,
            CAST(
                CASE
                    WHEN leads IS NULL OR len(leads) = 0 THEN 0
                    WHEN list_max(leads) <= 0 THEN 0
                    WHEN list_position(leads, list_max(leads)) >= len(leads) THEN 0
                    ELSE GREATEST(
                        0,
                        list_max(leads) - COALESCE(
                            list_min(list_slice(
                                leads,
                                list_position(leads, list_max(leads)) + 1,
                                len(leads)
                            )),
                            list_max(leads)
                        )
                    )
                END
            AS INTEGER) AS server_max_lead_surrendered
        FROM agg
    """)

    # ── Step 4: per-game state (cumulative games / sets) ──
    con.execute("""
        CREATE OR REPLACE TEMP TABLE games_with_state AS
        SELECT
            match_id, match_date, player_A, player_B,
            set_number, game_number_in_set, points_in_game,
            game_winner_int, game_server_int, is_tiebreak,
            (game_winner_int = 1) AS game_winner_is_A,
            (game_server_int = 1) AS server_was_A,
            SUM(CASE WHEN game_winner_int = 1 THEN 1 ELSE 0 END)
                OVER (PARTITION BY match_id, set_number ORDER BY game_number_in_set
                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS games_A,
            SUM(CASE WHEN game_winner_int = 2 THEN 1 ELSE 0 END)
                OVER (PARTITION BY match_id, set_number ORDER BY game_number_in_set
                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS games_B
        FROM games_raw
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE set_summary AS
        WITH last_game AS (
            SELECT match_id, set_number, MAX(game_number_in_set) AS final_game_num
            FROM games_with_state GROUP BY match_id, set_number
        ),
        final_state AS (
            SELECT g.match_id, g.set_number,
                   g.games_A AS final_games_A,
                   g.games_B AS final_games_B,
                   g.is_tiebreak AS last_game_was_tb
            FROM games_with_state g
            JOIN last_game lg
              ON g.match_id = lg.match_id
             AND g.set_number = lg.set_number
             AND g.game_number_in_set = lg.final_game_num
        )
        SELECT
            match_id, set_number, final_games_A, final_games_B, last_game_was_tb,
            CASE
                WHEN GREATEST(final_games_A, final_games_B) = 7 AND LEAST(final_games_A, final_games_B) = 6
                    THEN last_game_was_tb
                WHEN GREATEST(final_games_A, final_games_B) = 7 AND LEAST(final_games_A, final_games_B) = 5
                    THEN TRUE
                WHEN GREATEST(final_games_A, final_games_B) = 6 AND LEAST(final_games_A, final_games_B) <= 4
                    THEN TRUE
                WHEN GREATEST(final_games_A, final_games_B) >= 6
                  AND (GREATEST(final_games_A, final_games_B) - LEAST(final_games_A, final_games_B)) >= 2
                    THEN TRUE
                ELSE FALSE
            END AS is_complete,
            CASE WHEN final_games_A > final_games_B THEN 1 ELSE 0 END AS set_winner_is_A
        FROM final_state
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE set_pre_state AS
        SELECT
            match_id, set_number,
            COALESCE(SUM(CASE WHEN set_winner_is_A = 1 AND is_complete THEN 1 ELSE 0 END)
                OVER (PARTITION BY match_id ORDER BY set_number
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS sets_won_A,
            COALESCE(SUM(CASE WHEN set_winner_is_A = 0 AND is_complete THEN 1 ELSE 0 END)
                OVER (PARTITION BY match_id ORDER BY set_number
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS sets_won_B,
            is_complete,
            set_winner_is_A
        FROM set_summary
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE surface_lookup AS
        WITH match_meta AS (
            SELECT DISTINCT match_id, match_date, player_A, player_B
            FROM games_with_state
        ),
        ranked AS (
            SELECT
                mm.match_id,
                LOWER(m.surface) AS surface,
                ROW_NUMBER() OVER (
                    PARTITION BY mm.match_id
                    ORDER BY ABS(DATE_DIFF('day', mm.match_date,
                        STRPTIME(CAST(m.tourney_date AS VARCHAR), '%Y%m%d'))) ASC
                ) AS rn
            FROM match_meta mm
            JOIN core.atp_matches m
              ON ((m.winner_name = mm.player_A AND m.loser_name = mm.player_B)
               OR (m.winner_name = mm.player_B AND m.loser_name = mm.player_A))
              AND ABS(DATE_DIFF('day', mm.match_date,
                    STRPTIME(CAST(m.tourney_date AS VARCHAR), '%Y%m%d'))) <= 14
        )
        SELECT match_id, surface FROM ranked WHERE rn = 1
    """)

    # ── Step 5: final assembly with new columns and game_character rules ──
    con.execute("""
        CREATE OR REPLACE TABLE core.ml_game_level AS
        SELECT
            CAST(mim.match_id_int AS BIGINT)      AS match_id_int,
            g.match_id                            AS match_id_string,
            g.match_date,
            g.player_A,
            g.player_B,
            COALESCE(
                CASE WHEN sl.surface IN ('hard','clay','grass','carpet') THEN sl.surface ELSE NULL END,
                'unknown'
            ) AS surface,
            CAST(g.set_number AS INTEGER)         AS set_number,
            CAST(g.game_number_in_set AS INTEGER) AS game_number_in_set,
            CAST(g.games_A AS INTEGER)            AS games_A,
            CAST(g.games_B AS INTEGER)            AS games_B,
            CAST(sp.sets_won_A AS INTEGER)        AS sets_won_A,
            CAST(sp.sets_won_B AS INTEGER)        AS sets_won_B,
            g.server_was_A,
            g.is_tiebreak,
            g.game_winner_is_A,
            CAST(g.points_in_game AS INTEGER)     AS points_in_game,
            (g.game_winner_is_A = g.server_was_A)  AS server_held,
            (g.game_winner_is_A <> g.server_was_A) AS was_break,
            gf.bp_faced_by_server,
            gf.bp_saved_by_server,
            gf.server_max_deficit,
            gf.server_max_lead_surrendered,
            gf.deuce_count,
            gf.pressure_points_played,
            gf.first_point_server_won,
            CASE
                WHEN  (g.game_winner_is_A =  g.server_was_A)
                  AND gf.server_max_deficit <= 1
                  AND gf.deuce_count = 0
                    THEN 'clean_hold'
                WHEN  (g.game_winner_is_A =  g.server_was_A)
                  AND gf.bp_faced_by_server = 0
                  AND (gf.deuce_count > 0 OR gf.server_max_deficit >= 2)
                    THEN 'routine_hold'
                WHEN  (g.game_winner_is_A =  g.server_was_A)
                  AND gf.bp_faced_by_server >= 1
                    THEN 'pressure_hold'
                WHEN  (g.game_winner_is_A <> g.server_was_A)
                  AND gf.bp_saved_by_server = 0
                    THEN 'routine_break'
                WHEN  (g.game_winner_is_A <> g.server_was_A)
                  AND gf.bp_saved_by_server >= 1
                    THEN 'gritty_break'
            END AS game_character,
            CAST(sp.set_winner_is_A AS INTEGER)   AS set_winner_is_A,
            CASE
                WHEN EXTRACT(YEAR FROM g.match_date) BETWEEN 2015 AND 2023 THEN 'train'
                WHEN EXTRACT(YEAR FROM g.match_date) BETWEEN 2024 AND 2025 THEN 'test'
            END AS split
        FROM games_with_state g
        JOIN set_pre_state sp
          ON sp.match_id = g.match_id AND sp.set_number = g.set_number
        JOIN game_features gf
          ON gf.match_id = g.match_id
         AND gf.set_number = g.set_number
         AND gf.game_number_in_set = g.game_number_in_set
        JOIN core.match_id_map mim
          ON mim.match_id_string = g.match_id
        LEFT JOIN surface_lookup sl
          ON sl.match_id = g.match_id
        WHERE sp.is_complete = TRUE
        ORDER BY mim.match_id_int, g.set_number, g.game_number_in_set
    """)

    con.execute(f"""
        COPY core.ml_game_level TO '{PARQUET_PATH}' (FORMAT 'parquet')
    """)


def write_report(con: duckdb.DuckDBPyConnection) -> None:
    lines: list[str] = []

    def log(s: str = "") -> None:
        print(s)
        lines.append(s)

    log("=" * 70)
    log("ML GAME-LEVEL DATASET BUILD REPORT")
    log("=" * 70)

    total = con.execute("SELECT COUNT(*) FROM core.ml_game_level").fetchone()[0]
    n_match = con.execute("SELECT COUNT(DISTINCT match_id_string) FROM core.ml_game_level").fetchone()[0]
    n_set = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT match_id_string, set_number FROM core.ml_game_level
        )
    """).fetchone()[0]
    log(f"\nTotal rows: {total:,}")
    log(f"Distinct matches: {n_match:,}")
    log(f"Distinct sets:    {n_set:,}")

    log("\nRows by split:")
    for s, c in con.execute(
        "SELECT split, COUNT(*) FROM core.ml_game_level GROUP BY 1 ORDER BY 1"
    ).fetchall():
        log(f"  {s:<8} {c:>10,}")

    log("\nset_winner_is_A distribution:")
    for v, c in con.execute(
        "SELECT set_winner_is_A, COUNT(*) FROM core.ml_game_level GROUP BY 1 ORDER BY 1"
    ).fetchall():
        log(f"  {v}: {c:,} ({c/total*100:.1f}%)")

    log("\nSurface distribution:")
    for s, c in con.execute(
        "SELECT surface, COUNT(*) FROM core.ml_game_level GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        log(f"  {s:<10} {c:>8,}")

    log("\nRows by set_number:")
    for s, c in con.execute(
        "SELECT set_number, COUNT(*) FROM core.ml_game_level GROUP BY 1 ORDER BY 1"
    ).fetchall():
        log(f"  set {s}: {c:,}")

    # ── New diagnostics ──
    map_rows, mn_int, mx_int = con.execute("""
        SELECT COUNT(*), MIN(match_id_int), MAX(match_id_int) FROM core.match_id_map
    """).fetchone()
    log("\nmatch_id_map:")
    log(f"  rows:               {map_rows:,}")
    log(f"  match_id_int range: [{mn_int:,}, {mx_int:,}]")
    log(f"  1B offset confirmed: {mn_int >= 1_000_000_000}")

    log("\ngame_character distribution:")
    for ch, c in con.execute("""
        SELECT game_character, COUNT(*) FROM core.ml_game_level
        GROUP BY 1 ORDER BY 2 DESC
    """).fetchall():
        log(f"  {ch:<14} {c:>8,}  ({c/total*100:5.1f}%)")

    log("\nMean of new count features:")
    means = con.execute("""
        SELECT
            AVG(bp_faced_by_server),
            AVG(bp_saved_by_server),
            AVG(deuce_count),
            AVG(pressure_points_played)
        FROM core.ml_game_level
    """).fetchone()
    for name, v in zip(
        ["bp_faced_by_server", "bp_saved_by_server",
         "deuce_count", "pressure_points_played"],
        means,
    ):
        log(f"  {name:<24} {v:.4f}")

    log("\nserver_max_deficit distribution:")
    for v, c in con.execute("""
        SELECT server_max_deficit, COUNT(*) FROM core.ml_game_level
        GROUP BY 1 ORDER BY 1
    """).fetchall():
        log(f"  deficit={v}: {c:>8,}  ({c/total*100:5.1f}%)")

    fps_rate = con.execute("""
        SELECT AVG(CAST(first_point_server_won AS DOUBLE)) FROM core.ml_game_level
    """).fetchone()[0]
    log(f"\nfirst_point_server_won rate: {fps_rate:.4f} ({fps_rate*100:.2f}%)")

    log("\nNull check:")
    cols = [r[0] for r in con.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='core' AND table_name='ml_game_level'
        ORDER BY ordinal_position
    """).fetchall()]
    null_select = ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) AS "{c}"' for c in cols
    )
    null_row = con.execute(f"SELECT {null_select} FROM core.ml_game_level").fetchone()
    any_nulls = False
    for col, n in zip(cols, null_row):
        if n:
            any_nulls = True
            log(f"  {col}: {n:,} NULLS  !!")
    if not any_nulls:
        log("  All columns clean — zero nulls.")

    log(f"\nParquet written to: {PARQUET_PATH}")
    log( "DuckDB tables:      core.match_id_map, core.ml_game_level")

    # ── Test suite results ──
    log("\n" + "=" * 70)
    log("TEST RESULTS (tests/test_ml_game_level.py)")
    log("=" * 70)
    proc = subprocess.run(
        ["python3", "-m", "pytest", "tests/test_ml_game_level.py", "-v", "--tb=line"],
        capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        m = re.match(r".*::(test_\w+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
        if m:
            log(f"  {m.group(1):<48} {m.group(2)}")
    summary = re.search(r"(\d+\s+passed|\d+\s+failed|\d+\s+error)", out)
    if summary:
        log(f"\n  pytest summary: {summary.group(0)}")
    log(f"  pytest exit code: {proc.returncode}")

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(lines))
    log(f"\nReport saved to:    {REPORT_PATH}")


def main() -> None:
    con = duckdb.connect(DB_PATH)
    build_match_id_map(con)
    build_ml_game_level(con)
    con.close()
    # Re-open read-only so an in-script pytest invocation can also open the DB.
    con = duckdb.connect(DB_PATH, read_only=True)
    write_report(con)
    con.close()


if __name__ == "__main__":
    main()
