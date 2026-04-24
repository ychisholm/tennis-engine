#!/usr/bin/env python3
"""
Component 6A — Backtester

Replays historical matches point-by-point through the LiveMatch orchestrator,
logs model predictions at every point, and writes results to CSV for
downstream Brier score and log-loss analysis (6B).

Data source
-----------
  atp_points / wta_points  — one row per point (tennis_MatchChartingProject)
  atp_matches / wta_matches — match metadata and career serve stats

Output schema (one CSV row per point)
--------------------------------------
  match_id, point_index, set_no, game_no,
  P_match_A, actual_winner,
  D_A, D_B, delta, p_hat_A, p_hat_B,
  serving_player, sets_A, sets_B, games_A, games_B

Usage
-----
    from src.backtester import Backtester
    bt = Backtester(db_path="data/processed/tennis.duckdb")
    out = bt.run(tour="atp", max_matches=50)
    print(out)
"""

from __future__ import annotations

import csv
import datetime
import logging
import os
import warnings

import duckdb

from src.engine.live_match import LiveMatch

logger = logging.getLogger(__name__)

# CSV columns in output order
_CSV_COLUMNS = [
    "match_id", "point_index", "set_no", "game_no",
    "P_match_A", "actual_winner",
    "D_A", "D_B", "delta", "p_hat_A", "p_hat_B",
    "serving_player", "sets_A", "sets_B", "games_A", "games_B",
]

# Neutral archetype — used when no charting-level profile is available
_NEUTRAL_ARCH = {"sd": 50, "ba": 50, "pe": 50, "tv": 50}
_DEFAULT_P0 = 0.65


class Backtester:
    """
    Backtest the LiveMatch engine against historical charting data.

    Parameters
    ----------
    db_path : str
        Path to the DuckDB database that contains atp_points / wta_points
        and atp_matches / wta_matches tables.
    output_dir : str
        Directory where CSV prediction files will be written.
        Created automatically if it does not exist.
    """

    def __init__(
        self,
        db_path: str = "data/processed/tennis.duckdb",
        output_dir: str = "data/backtesting",
    ) -> None:
        self._con = duckdb.connect(db_path, read_only=True)
        os.makedirs(output_dir, exist_ok=True)
        self._output_dir = output_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        match_ids: list[str] | None = None,
        tour: str = "atp",
        max_matches: int | None = None,
        p0_lookup_fn=None,
        progress_callback=None,
        lambda_decay: float = 4.0,
        k: float = 0.08,
        sigma: float = 25.0,
    ) -> str:
        """
        Replay matches and write predictions to CSV.

        Parameters
        ----------
        match_ids : list or None
            Explicit list of match IDs to replay.  If None, all available
            match IDs from the points table are used.
        tour : str
            "atp" or "wta".
        max_matches : int or None
            Cap the number of matches processed.
        p0_lookup_fn : callable or None
            Signature: fn(player_name: str, surface: str) -> float.
            If None, career serve-win % is computed from the matches table;
            falls back to 0.65 for players with insufficient data.
        progress_callback : callable or None
            Called as fn(completed, total) after each finished match.
        lambda_decay : float
            Recency half-life passed to TemporalEngine.  Default 4.0.
        k : float
            Max p-hat adjustment passed to PhatAdjuster.  Default 0.08.
        sigma : float
            Sigmoid sensitivity passed to PhatAdjuster.  Default 25.0.

        Returns
        -------
        str — absolute path to the written CSV file.
        """
        points_table = f"{tour}_points"

        if match_ids is None:
            rows = self._con.execute(
                f"SELECT DISTINCT match_id FROM {points_table} ORDER BY match_id"
            ).fetchall()
            match_ids = [r[0] for r in rows]

        if max_matches is not None:
            match_ids = match_ids[:max_matches]

        total = len(match_ids)
        attempted = 0
        completed = 0
        skipped = 0
        all_rows: list[dict] = []

        for match_id in match_ids:
            attempted += 1
            try:
                result_rows = self._replay_match(
                    match_id, tour, p0_lookup_fn,
                    lambda_decay=lambda_decay, k=k, sigma=sigma,
                )
                if result_rows is None:
                    skipped += 1
                else:
                    all_rows.extend(result_rows)
                    completed += 1
            except Exception as exc:
                logger.warning("Skipping match %s due to error: %s", match_id, exc)
                skipped += 1

            if progress_callback is not None:
                progress_callback(attempted, total)

        logger.info(
            "Backtest complete — attempted: %d, completed: %d, skipped: %d",
            attempted, completed, skipped,
        )

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(self._output_dir, f"predictions_{tour}_{ts}.csv")
        self._write_csv(all_rows, out_path)
        return out_path

    # ------------------------------------------------------------------
    # Parameter-tuning API (6B)
    # ------------------------------------------------------------------

    def preload_match_data(
        self,
        match_ids: list[str],
        tour: str = "atp",
    ) -> dict:
        """
        Pre-fetch all data needed to replay the given matches using
        bulk queries to minimise round-trips to DuckDB.

        Step 1: fetch ALL points for all match_ids in one query.
        Step 2: compute ALL players' p0 values in two queries.
        Step 3: look up surfaces per match via a batched query.

        Returns
        -------
        dict mapping match_id -> {points, actual_winner, player_a, player_b,
                                   surface, p0_A, p0_B}
        Matches with fewer than 10 points are silently excluded.
        """
        if not match_ids:
            return {}

        points_table = f"{tour}_points"
        matches_table = f"{tour}_matches"

        # ---- Step 1: batch-fetch all points --------------------------------
        placeholders = ",".join("?" * len(match_ids))
        sql_pts = f"""
            SELECT
                match_id,
                Pt        AS pt,
                Set1      AS set1,
                Set2      AS set2,
                Gm1       AS gm1,
                Gm2       AS gm2,
                Pts       AS pts,
                "Gm#"     AS gm_no,
                Svr       AS svr,
                "1st"     AS first_serve,
                "2nd"     AS second_serve,
                PtWinner  AS pt_winner
            FROM {points_table}
            WHERE match_id IN ({placeholders})
            ORDER BY match_id, Pt
        """
        raw_pts = self._con.execute(sql_pts, match_ids).fetchall()
        pt_keys = ["pt", "set1", "set2", "gm1", "gm2", "pts", "gm_no",
                   "svr", "first_serve", "second_serve", "pt_winner"]

        # Group points by match_id
        points_by_match: dict[str, list[dict]] = {}
        for row in raw_pts:
            mid = row[0]
            points_by_match.setdefault(mid, []).append(dict(zip(pt_keys, row[1:])))

        # ---- Step 2: batch-compute p0 for all players ----------------------
        # One UNION ALL query returns (player_name, surface, total_won, total_svpt)
        sql_p0 = f"""
            SELECT player_name, surface,
                   SUM(won)  AS total_won,
                   SUM(svpt) AS total_svpt
            FROM (
                SELECT winner_name AS player_name,
                       LOWER(surface) AS surface,
                       CAST(w_1stWon AS BIGINT) + CAST(w_2ndWon AS BIGINT) AS won,
                       CAST(w_svpt   AS BIGINT) AS svpt
                FROM {matches_table}
                WHERE w_svpt IS NOT NULL
                UNION ALL
                SELECT loser_name,
                       LOWER(surface),
                       CAST(l_1stWon AS BIGINT) + CAST(l_2ndWon AS BIGINT),
                       CAST(l_svpt   AS BIGINT)
                FROM {matches_table}
                WHERE l_svpt IS NOT NULL
            )
            GROUP BY player_name, surface
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p0_rows = self._con.execute(sql_p0).fetchall()

        p0_table: dict[tuple[str, str], float] = {}
        for player_name, surface, total_won, total_svpt in p0_rows:
            if total_svpt and total_svpt >= 100:
                p0 = max(0.35, min(0.85, total_won / total_svpt))
                p0_table[(player_name, surface)] = p0

        # ---- Step 3: assemble preloaded dict --------------------------------
        preloaded: dict = {}
        for match_id in match_ids:
            pts = points_by_match.get(match_id)
            if not pts or len(pts) < 10:
                continue

            try:
                last = pts[-1]
                actual_winner = 1 if last["set1"] >= last["set2"] else 0

                player_a, player_b = self._parse_players(match_id)
                surface = self._lookup_surface(match_id, player_a, player_b, tour)
                surf_lower = surface.lower()

                p0_A = p0_table.get((player_a, surf_lower), _DEFAULT_P0)
                p0_B = p0_table.get((player_b, surf_lower), _DEFAULT_P0)

                preloaded[match_id] = {
                    "points": pts,
                    "actual_winner": actual_winner,
                    "player_a": player_a,
                    "player_b": player_b,
                    "surface": surface,
                    "p0_A": p0_A,
                    "p0_B": p0_B,
                }
            except Exception as exc:
                logger.warning("Skipping %s during preload: %s", match_id, exc)

        return preloaded

    def score_matches(
        self,
        match_ids: list[str],
        tour: str = "atp",
        lambda_decay: float = 4.0,
        k: float = 0.08,
        sigma: float = 25.0,
        preloaded_data: dict | None = None,
    ) -> float:
        """
        Replay matches with the given parameters and return the Brier score.

        Does not write any CSV files — intended for use in the 6B grid search.

        If *preloaded_data* is supplied (from ``preload_match_data``), all DB
        queries are skipped, making repeated calls with different parameters
        very fast.

        Returns
        -------
        float — mean squared error of P_match_A predictions, or nan if no
        valid predictions could be computed.
        """
        data = preloaded_data if preloaded_data is not None else \
            self.preload_match_data(match_ids, tour)

        all_pairs: list[tuple[float, int]] = []

        for match_id in match_ids:
            if match_id not in data:
                continue
            try:
                pairs = self._replay_match_with_params(
                    data[match_id], lambda_decay, k, sigma
                )
                all_pairs.extend(pairs)
            except Exception as exc:
                logger.warning("Error scoring match %s: %s", match_id, exc)

        if not all_pairs:
            return float("nan")

        return sum((p - a) ** 2 for p, a in all_pairs) / len(all_pairs)

    def _replay_match_with_params(
        self,
        match_data: dict,
        lambda_decay: float,
        k: float,
        sigma: float,
    ) -> list[tuple[float, int]]:
        """
        Replay a preloaded match with specific parameters.

        Returns
        -------
        list of (P_match_A, actual_winner) pairs — one per point played.
        """
        player_a = match_data["player_a"]
        player_b = match_data["player_b"]
        surface = match_data["surface"]
        p0_A = match_data["p0_A"]
        p0_B = match_data["p0_B"]
        actual_winner = match_data["actual_winner"]
        points = match_data["points"]

        match = LiveMatch(
            player_a={"name": player_a,
                       "p0_hard": p0_A, "p0_clay": p0_A, "p0_grass": p0_A,
                       "archetype": _NEUTRAL_ARCH},
            player_b={"name": player_b,
                       "p0_hard": p0_B, "p0_clay": p0_B, "p0_grass": p0_B,
                       "archetype": _NEUTRAL_ARCH},
            surface=surface.lower(),
            best_of=3,
            lambda_decay=lambda_decay,
            k=k,
            sigma=sigma,
        )

        pairs: list[tuple[float, int]] = []
        for pt in points:
            serving = "A" if pt["svr"] == 1 else "B"
            winner = "A" if pt["pt_winner"] == 1 else "B"
            s1 = pt["first_serve"] or ""
            s2 = pt["second_serve"] or ""
            rally_length = max(1, (len(s1) + len(s2)) // 2)
            is_first = not bool(s2)

            result = match.process_point({
                "winner": winner,
                "serving": serving,
                "rally_length": rally_length,
                "is_first_serve": is_first,
            })
            pairs.append((result["probabilities"]["P_match_A"], actual_winner))

            if result["match_over"]:
                break

        return pairs

    # ------------------------------------------------------------------
    # Match replay
    # ------------------------------------------------------------------

    def _replay_match(
        self,
        match_id: str,
        tour: str,
        p0_lookup_fn,
        lambda_decay: float = 4.0,
        k: float = 0.08,
        sigma: float = 25.0,
    ) -> list[dict] | None:
        """
        Replay one match.  Returns a list of row dicts, or None if skipped.
        """
        points = self._fetch_points(match_id, tour)

        if len(points) < 10:
            logger.warning(
                "Skipping %s: only %d points (minimum 10 required)",
                match_id, len(points),
            )
            return None

        # Determine actual match winner from the final point's cumulative sets
        last = points[-1]
        final_set1 = last["set1"]
        final_set2 = last["set2"]
        actual_winner = 1 if final_set1 >= final_set2 else 0

        # Player names and surface
        player_a, player_b = self._parse_players(match_id)
        surface = self._lookup_surface(match_id, player_a, player_b, tour)

        # Serve-win probabilities
        if p0_lookup_fn is not None:
            p0_A = float(p0_lookup_fn(player_a, surface))
            p0_B = float(p0_lookup_fn(player_b, surface))
        else:
            p0_A = self._career_p0(player_a, surface, tour)
            p0_B = self._career_p0(player_b, surface, tour)

        # Instantiate a fresh LiveMatch
        match = LiveMatch(
            player_a={"name": player_a,
                       "p0_hard": p0_A, "p0_clay": p0_A, "p0_grass": p0_A,
                       "archetype": _NEUTRAL_ARCH},
            player_b={"name": player_b,
                       "p0_hard": p0_B, "p0_clay": p0_B, "p0_grass": p0_B,
                       "archetype": _NEUTRAL_ARCH},
            surface=surface.lower(),
            best_of=3,
            lambda_decay=lambda_decay,
            k=k,
            sigma=sigma,
        )

        rows: list[dict] = []
        for idx, pt in enumerate(points):
            serving = "A" if pt["svr"] == 1 else "B"
            winner  = "A" if pt["pt_winner"] == 1 else "B"

            # Rally length: rough proxy from shot-string length
            s1 = pt["first_serve"] or ""
            s2 = pt["second_serve"] or ""
            rally_length = max(1, (len(s1) + len(s2)) // 2)

            is_first = not bool(s2)

            result = match.process_point({
                "winner":         winner,
                "serving":        serving,
                "rally_length":   rally_length,
                "is_first_serve": is_first,
            })

            ms  = result["match_state"]
            dom = result["dominance"]
            adj = result["adjusted_p"]
            prb = result["probabilities"]

            rows.append({
                "match_id":       match_id,
                "point_index":    idx,
                "set_no":         pt["set1"] + pt["set2"] + 1,
                "game_no":        pt["gm_no"],
                "P_match_A":      prb["P_match_A"],
                "actual_winner":  actual_winner,
                "D_A":            dom["D_A"],
                "D_B":            dom["D_B"],
                "delta":          dom["delta"],
                "p_hat_A":        adj["p_hat_A"],
                "p_hat_B":        adj["p_hat_B"],
                "serving_player": ms["serving_player"],
                "sets_A":         ms["sets_A"],
                "sets_B":         ms["sets_B"],
                "games_A":        ms["games_A"],
                "games_B":        ms["games_B"],
            })

            if result["match_over"]:
                break

        return rows

    # ------------------------------------------------------------------
    # Data access helpers
    # ------------------------------------------------------------------

    def _fetch_points(self, match_id: str, tour: str) -> list[dict]:
        """Fetch all points for a match in chronological order."""
        table = f"{tour}_points"
        sql = f"""
            SELECT
                Pt        AS pt,
                Set1      AS set1,
                Set2      AS set2,
                Gm1       AS gm1,
                Gm2       AS gm2,
                Pts       AS pts,
                "Gm#"     AS gm_no,
                Svr       AS svr,
                "1st"     AS first_serve,
                "2nd"     AS second_serve,
                PtWinner  AS pt_winner
            FROM {table}
            WHERE match_id = ?
            ORDER BY Pt
        """
        rows = self._con.execute(sql, [match_id]).fetchall()
        keys = ["pt", "set1", "set2", "gm1", "gm2", "pts", "gm_no",
                "svr", "first_serve", "second_serve", "pt_winner"]
        return [dict(zip(keys, r)) for r in rows]

    def _parse_players(self, match_id: str) -> tuple[str, str]:
        """
        Extract player names from a charting match_id.
        Format: YYYYMMDD-M/W-TourneyName-Round-PlayerA-PlayerB
        Player names use underscores in place of spaces.
        """
        parts = match_id.split("-")
        if len(parts) < 6:
            return "PlayerA", "PlayerB"
        player_b = parts[-1].replace("_", " ")
        player_a = parts[-2].replace("_", " ")
        return player_a, player_b

    def _lookup_surface(
        self, match_id: str, player_a: str, player_b: str, tour: str
    ) -> str:
        """
        Attempt to find surface from the matches table.  Falls back to "Hard".
        """
        table = f"{tour}_matches"
        date_prefix = match_id.split("-")[0]  # YYYYMMDD
        try:
            r = self._con.execute(
                f"""
                SELECT surface FROM {table}
                WHERE (winner_name = ? AND loser_name = ?)
                   OR (winner_name = ? AND loser_name = ?)
                ORDER BY ABS(CAST(tourney_date AS BIGINT) - CAST(? AS BIGINT))
                LIMIT 1
                """,
                [player_a, player_b, player_b, player_a, date_prefix],
            ).fetchone()
            if r and r[0]:
                return str(r[0])
        except Exception:
            pass
        return "Hard"

    def _career_p0(self, player_name: str, surface: str, tour: str) -> float:
        """
        Compute career serve-win % for *player_name* on *surface*.
        Returns _DEFAULT_P0 (0.65) if fewer than 100 serve points found.
        """
        table = f"{tour}_matches"
        # Suppress warnings from NULL handling
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                r_win = self._con.execute(
                    f"""
                    SELECT
                        SUM(CAST(w_1stWon AS BIGINT) + CAST(w_2ndWon AS BIGINT)),
                        SUM(CAST(w_svpt   AS BIGINT))
                    FROM {table}
                    WHERE winner_name = ? AND LOWER(surface) = LOWER(?)
                      AND w_svpt IS NOT NULL
                    """,
                    [player_name, surface],
                ).fetchone()

                r_los = self._con.execute(
                    f"""
                    SELECT
                        SUM(CAST(l_1stWon AS BIGINT) + CAST(l_2ndWon AS BIGINT)),
                        SUM(CAST(l_svpt   AS BIGINT))
                    FROM {table}
                    WHERE loser_name = ? AND LOWER(surface) = LOWER(?)
                      AND l_svpt IS NOT NULL
                    """,
                    [player_name, surface],
                ).fetchone()

                won = (r_win[0] or 0) + (r_los[0] or 0)
                total = (r_win[1] or 0) + (r_los[1] or 0)

                if total >= 100:
                    return max(0.35, min(0.85, won / total))
            except Exception:
                pass

        return _DEFAULT_P0

    # ------------------------------------------------------------------
    # CSV output
    # ------------------------------------------------------------------

    def _write_csv(self, rows: list[dict], path: str) -> None:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
