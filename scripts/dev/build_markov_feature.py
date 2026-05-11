"""Build the Markov baseline feature: populate core.player_p0 and core.ml_game_level.markov_set_win_prob_A."""

import statistics
import sys
import time
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.markov_engine import clear_set_cache, set_win_prob
from src.p0_engine import compute_p0_table


DB_PATH = REPO_ROOT / "data" / "processed" / "tennis.duckdb"
REPORT_PATH = REPO_ROOT / "data" / "processed" / "markov_build_report.txt"


def main() -> None:
    t0 = time.time()
    con = duckdb.connect(str(DB_PATH))

    print(f"Connected to {DB_PATH}")
    print("Computing p0 for ml_game_level players...")
    p0_rows = compute_p0_table(con)

    p0_map = {r["player_name"]: r["p0"] for r in p0_rows}
    method_counts = {"direct": 0, "fuzzy": 0, "league_average": 0}
    for r in p0_rows:
        method_counts[r["match_method"]] += 1
    print(f"  total players: {len(p0_rows)}")
    print(f"  direct: {method_counts['direct']}, fuzzy: {method_counts['fuzzy']}, "
          f"league_average: {method_counts['league_average']}")

    league_avg_p0 = next(
        (r["p0"] for r in p0_rows if r["match_method"] == "league_average"),
        statistics.mean(r["p0"] for r in p0_rows),
    )

    print("Rebuilding core.player_p0...")
    con.execute("DROP TABLE IF EXISTS core.player_p0")
    con.execute(
        """
        CREATE TABLE core.player_p0 (
            player_name VARCHAR PRIMARY KEY,
            p0 DOUBLE NOT NULL,
            serve_points_played BIGINT,
            serve_points_won BIGINT,
            match_method VARCHAR
        )
        """
    )
    con.executemany(
        """
        INSERT INTO core.player_p0
            (player_name, p0, serve_points_played, serve_points_won, match_method)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                r["player_name"],
                r["p0"],
                r["serve_points_played"],
                r["serve_points_won"],
                r["match_method"],
            )
            for r in p0_rows
        ],
    )
    print(f"  inserted {len(p0_rows)} rows")

    print("Adding markov_set_win_prob_A column...")
    existing_cols = {
        c[0] for c in con.execute("DESCRIBE core.ml_game_level").fetchall()
    }
    if "markov_set_win_prob_A" in existing_cols:
        con.execute("ALTER TABLE core.ml_game_level DROP COLUMN markov_set_win_prob_A")
    con.execute("ALTER TABLE core.ml_game_level ADD COLUMN markov_set_win_prob_A DOUBLE")

    print("Loading rows for computation...")
    rows = con.execute(
        """
        SELECT
            match_id_int,
            set_number,
            game_number_in_set,
            player_A,
            player_B,
            games_A,
            games_B,
            server_was_A
        FROM core.ml_game_level
        ORDER BY match_id_int, set_number, game_number_in_set
        """
    ).fetchall()
    print(f"  {len(rows)} rows to process")

    print("Computing markov_set_win_prob_A per row...")
    results: list[tuple[int, int, int, float]] = []
    matches_processed = 0
    current_match_id = None
    p0_a = p0_b = league_avg_p0
    for (match_id, set_number, game_in_set,
         player_a, player_b, games_a, games_b, server_was_a) in rows:
        if match_id != current_match_id:
            current_match_id = match_id
            clear_set_cache()
            p0_a = p0_map.get(player_a, league_avg_p0)
            p0_b = p0_map.get(player_b, league_avg_p0)
            matches_processed += 1
            if matches_processed % 500 == 0:
                print(f"  {matches_processed} matches processed...")
        next_server_is_a = not bool(server_was_a)
        prob = set_win_prob(p0_a, p0_b, int(games_a), int(games_b), next_server_is_a)
        results.append((match_id, set_number, game_in_set, prob))
    print(f"  {matches_processed} matches processed, {len(results)} rows computed")

    print("Bulk-updating ml_game_level...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _markov_results (
            match_id_int BIGINT,
            set_number INTEGER,
            game_number_in_set INTEGER,
            prob DOUBLE
        )
        """
    )
    con.executemany("INSERT INTO _markov_results VALUES (?, ?, ?, ?)", results)

    con.execute(
        """
        UPDATE core.ml_game_level AS ml
        SET markov_set_win_prob_A = r.prob
        FROM _markov_results AS r
        WHERE ml.match_id_int = r.match_id_int
          AND ml.set_number = r.set_number
          AND ml.game_number_in_set = r.game_number_in_set
        """
    )
    null_count = con.execute(
        "SELECT COUNT(*) FROM core.ml_game_level WHERE markov_set_win_prob_A IS NULL"
    ).fetchone()[0]
    print(f"  rows still NULL after update: {null_count}")

    print("Computing report distributions...")
    p0_vals = [r["p0"] for r in p0_rows]
    p0_min = min(p0_vals)
    p0_max = max(p0_vals)
    p0_mean = statistics.mean(p0_vals)
    p0_median = statistics.median(p0_vals)
    p0_std = statistics.pstdev(p0_vals)

    markov_stats = con.execute(
        """
        SELECT
            MIN(markov_set_win_prob_A),
            MAX(markov_set_win_prob_A),
            AVG(markov_set_win_prob_A),
            MEDIAN(markov_set_win_prob_A),
            STDDEV_POP(markov_set_win_prob_A)
        FROM core.ml_game_level
        """
    ).fetchone()
    m_min, m_max, m_mean, m_median, m_std = markov_stats

    runtime = time.time() - t0

    report_lines = [
        "Markov build report",
        "===================",
        f"Total ml_game_level players: {len(p0_rows)}",
        f"  direct: {method_counts['direct']}",
        f"  fuzzy: {method_counts['fuzzy']}",
        f"  league_average: {method_counts['league_average']}",
        "",
        f"League-average p0: {round(league_avg_p0, 4)}",
        "",
        "p0 distribution (across ml_game_level players):",
        f"  min:    {p0_min:.6f}",
        f"  max:    {p0_max:.6f}",
        f"  mean:   {p0_mean:.6f}",
        f"  median: {p0_median:.6f}",
        f"  std:    {p0_std:.6f}",
        "",
        "markov_set_win_prob_A distribution:",
        f"  min:    {m_min:.6f}",
        f"  max:    {m_max:.6f}",
        f"  mean:   {m_mean:.6f}",
        f"  median: {m_median:.6f}",
        f"  std:    {m_std:.6f}",
        "",
        f"Total runtime: {runtime:.2f} s",
    ]
    REPORT_PATH.write_text("\n".join(report_lines) + "\n")
    print(f"Report written to {REPORT_PATH}")
    print(f"Runtime: {runtime:.2f} s")
    con.close()


if __name__ == "__main__":
    main()
