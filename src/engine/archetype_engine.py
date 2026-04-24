#!/usr/bin/env python3
"""
Component 3A — Archetype Engine

Computes a four-dimensional ArchetypeVector for any player from career match
statistics stored in a DuckDB database (Jeff Sackmann ATP/WTA data).

Dimensions
----------
  SD  Serve Dominance     (computed from real serve stats)
  BA  Baseline Aggression (proxy from return / break stats)
  PE  Physical Endurance  (proxy from 3-set outcomes + pressure stats)
  TV  Tactical Variety    (proxy from cross-surface consistency)

Usage
-----
    from src.archetype_engine import ArchetypeEngine

    engine = ArchetypeEngine()
    av = engine.compute_career_archetype("Roger Federer", surface="all")
    print(engine.describe_archetype(av))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import math

import duckdb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scale(value: float, lo: float, hi: float) -> float:
    """Linear interpolation: lo→0, hi→100, clipped to [0, 100]."""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def _safe_div(num: float, den: float) -> Optional[float]:
    """Return num/den, or None if den is zero."""
    return num / den if den != 0 else None


# ---------------------------------------------------------------------------
# ArchetypeVector dataclass
# ---------------------------------------------------------------------------

@dataclass
class ArchetypeVector:
    SD: float               # Serve Dominance, 0-100
    BA: float               # Baseline Aggression, 0-100
    PE: float               # Physical Endurance, 0-100
    TV: float               # Tactical Variety, 0-100
    data_quality: dict = field(default_factory=dict)
    player_name: str = ""
    surface: str = "all"    # "Hard", "Clay", "Grass", or "all"
    matches_used: int = 0


# ---------------------------------------------------------------------------
# ArchetypeEngine
# ---------------------------------------------------------------------------

class ArchetypeEngine:
    """Computes and caches player archetype vectors from DuckDB match data."""

    def __init__(self, db_path: str = "data/processed/tennis.duckdb"):
        self._con = duckdb.connect(db_path, read_only=True)
        self._cache: dict[tuple[str, str], ArchetypeVector] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_career_archetype(
        self,
        player_name: str,
        surface: str = "all",
    ) -> ArchetypeVector:
        """
        Compute an ArchetypeVector for *player_name* over their career.

        Parameters
        ----------
        player_name : str
            Exact name as stored in the database.
        surface : str
            One of "Hard", "Clay", "Grass", or "all".

        Returns
        -------
        ArchetypeVector

        Raises
        ------
        ValueError
            If the player is not found in either atp_matches or wta_matches.
        """
        cache_key = (player_name, surface)
        if cache_key in self._cache:
            return self._cache[cache_key]

        rows, actual_surface, surface_fallback = self._fetch_rows(
            player_name, surface
        )

        if not rows:
            raise ValueError(
                f"Player '{player_name}' not found in the database. "
                "Check spelling or try a different surface filter."
            )

        stats = self._aggregate_stats(rows, player_name)
        av = self._build_vector(
            player_name=player_name,
            surface=actual_surface,
            matches_used=len(rows),
            stats=stats,
            surface_fallback=surface_fallback,
        )
        self._cache[cache_key] = av
        return av

    def update_archetype(
        self,
        career: ArchetypeVector,
        observed: ArchetypeVector,
        beta: float = 0.15,
    ) -> ArchetypeVector:
        """
        Blend a career vector with an in-match observed vector.

            updated = (1 - beta) * career + beta * observed

        Parameters
        ----------
        career   : baseline career ArchetypeVector
        observed : observed in-match ArchetypeVector
        beta     : adaptation weight (default 0.15)

        Returns a new ArchetypeVector (inputs are not mutated).
        """
        alpha = 1.0 - beta
        return ArchetypeVector(
            SD=alpha * career.SD + beta * observed.SD,
            BA=alpha * career.BA + beta * observed.BA,
            PE=alpha * career.PE + beta * observed.PE,
            TV=alpha * career.TV + beta * observed.TV,
            data_quality={
                "SD": "adapted",
                "BA": "adapted",
                "PE": "adapted",
                "TV": "adapted",
            },
            player_name=career.player_name,
            surface=career.surface,
            matches_used=career.matches_used,
        )

    def get_signal_weights(
        self,
        archetype: ArchetypeVector,
        signal: str,
    ) -> dict:
        """
        Return archetype-adjusted, normalised sub-signal weights for *signal*.

        Parameters
        ----------
        signal : one of "NMI", "SMS", "RMS", "PMS", "GPS"

        Returns
        -------
        dict mapping sub-signal label → float weight (sum == 1.0)
        """
        valid = {"NMI", "SMS", "RMS", "PMS", "GPS"}
        if signal not in valid:
            raise ValueError(
                f"signal must be one of {valid}, got '{signal}'"
            )

        if signal == "NMI":
            # No archetype adjustment — internal formula handles weighting
            return {"NMI_1": 0.50, "NMI_2": 0.50}

        if signal == "GPS":
            # Applied uniformly — no archetype adjustment
            return {
                "GPS_1": 0.25,
                "GPS_2": 0.25,
                "GPS_3": 0.25,
                "GPS_4": 0.25,
            }

        if signal == "SMS":
            weights = {
                "SMS_1": 0.35,
                "SMS_2": 0.30,
                "SMS_3": 0.20,
                "SMS_4": 0.15,
            }
            if archetype.SD > 70:
                weights["SMS_1"] *= 1.3
                weights["SMS_2"] *= 1.2
            elif archetype.SD < 40:
                weights["SMS_1"] *= 0.8
                weights["SMS_2"] *= 0.9

        elif signal == "RMS":
            weights = {
                "RMS_1": 0.35,
                "RMS_2": 0.25,
                "RMS_3": 0.40,
            }
            if archetype.BA > 70:
                weights["RMS_2"] *= 1.2
            if archetype.SD > 70:
                weights["RMS_3"] *= 0.85

        elif signal == "PMS":
            weights = {
                "PMS_1": 0.25,
                "PMS_2": 0.35,
                "PMS_3": 0.30,
                "PMS_4": 0.10,
            }
            # Apply the dominant archetype adjustment (SD vs PE), not both
            if archetype.SD > 70 or archetype.PE > 70:
                if archetype.SD >= archetype.PE:
                    # Big Server profile
                    weights["PMS_1"] *= 1.60
                    weights["PMS_4"] *= 2.50
                else:
                    # Counter-Puncher profile
                    weights["PMS_3"] *= 1.67
                    weights["PMS_1"] *= 0.40

        # Normalise so weights sum to 1.0
        total = sum(weights.values())
        return {k: v / total for k, v in weights.items()}

    def describe_archetype(self, archetype: ArchetypeVector) -> str:
        """
        Return a plain-English description of the player's style.

        Classification
        --------------
        SD > 75                  → Big Server
        PE > 80 and BA < 60      → Counter-Puncher
        BA > 75                  → Aggressive Baseliner
        else                     → All-Court
        """
        sd, ba, pe, tv = archetype.SD, archetype.BA, archetype.PE, archetype.TV

        if sd > 75:
            style = "Big Server"
        elif pe > 80 and ba < 60:
            style = "Counter-Puncher"
        elif ba > 75:
            style = "Aggressive Baseliner"
        else:
            style = "All-Court"

        proxy_dims = [
            dim
            for dim, quality in archetype.data_quality.items()
            if "proxy" in quality.lower()
        ]

        proxy_note = ""
        if proxy_dims:
            proxy_note = (
                f" Note: {', '.join(proxy_dims)} "
                "dimensions are proxies and will improve with charting data."
            )

        surface_note = (
            f" on {archetype.surface} surfaces"
            if archetype.surface != "all"
            else " across all surfaces"
        )

        return (
            f"{archetype.player_name} is a {style} player"
            f"{surface_note} "
            f"(SD: {sd:.1f}, BA: {ba:.1f}, PE: {pe:.1f}, TV: {tv:.1f}), "
            f"based on {archetype.matches_used} career matches.{proxy_note}"
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_rows(
        self,
        player_name: str,
        surface: str,
    ) -> tuple[list[tuple], str, bool]:
        """
        Fetch all match rows for *player_name* from both ATP and WTA tables.

        Returns
        -------
        (rows, actual_surface, surface_fallback)
        """
        surface_filter = ""
        if surface != "all":
            surface_filter = "AND LOWER(surface) = LOWER(?)"

        query = f"""
            SELECT
                -- winner perspective
                w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
                w_SvGms, w_bpSaved, w_bpFaced,
                l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
                l_SvGms, l_bpSaved, l_bpFaced,
                surface, score,
                'winner' AS player_role
            FROM {{table}}
            WHERE winner_name = ?
              AND w_svpt IS NOT NULL
              {surface_filter}

            UNION ALL

            SELECT
                l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
                l_SvGms, l_bpSaved, l_bpFaced,
                w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
                w_SvGms, w_bpSaved, w_bpFaced,
                surface, score,
                'loser' AS player_role
            FROM {{table}}
            WHERE loser_name = ?
              AND l_svpt IS NOT NULL
              {surface_filter}
        """

        params_no_surface = [player_name, player_name]
        params_with_surface = [player_name, surface, player_name, surface]
        params = params_with_surface if surface != "all" else params_no_surface

        rows = []
        for table in ("core.atp_matches", "core.wta_matches"):
            q = query.format(table=table)
            rows.extend(self._con.execute(q, params).fetchall())

        surface_fallback = False
        actual_surface = surface

        # Require minimum 20 matches; fall back to all surfaces if needed
        if len(rows) < 20 and surface != "all":
            fallback_params = [player_name, player_name]
            rows = []
            for table in ("core.atp_matches", "core.wta_matches"):
                q_fallback = f"""
                    SELECT
                        w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
                        w_SvGms, w_bpSaved, w_bpFaced,
                        l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
                        l_SvGms, l_bpSaved, l_bpFaced,
                        surface, score,
                        'winner' AS player_role
                    FROM {table}
                    WHERE winner_name = ?
                      AND w_svpt IS NOT NULL

                    UNION ALL

                    SELECT
                        l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
                        l_SvGms, l_bpSaved, l_bpFaced,
                        w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
                        w_SvGms, w_bpSaved, w_bpFaced,
                        surface, score,
                        'loser' AS player_role
                    FROM {table}
                    WHERE loser_name = ?
                      AND l_svpt IS NOT NULL
                """
                rows.extend(
                    self._con.execute(q_fallback, fallback_params).fetchall()
                )
            surface_fallback = True
            actual_surface = "all"

        return rows, actual_surface, surface_fallback

    def _aggregate_stats(
        self,
        rows: list[tuple],
        player_name: str,
    ) -> dict:
        """
        Aggregate per-match stats into career averages.

        Column order (from SQL):
          0  p_ace     1  p_df      2  p_svpt    3  p_1stIn
          4  p_1stWon  5  p_2ndWon  6  p_SvGms   7  p_bpSaved
          8  p_bpFaced 9  o_ace    10  o_df     11  o_svpt
         12  o_1stIn  13  o_1stWon 14  o_2ndWon 15  o_SvGms
         16  o_bpSaved 17 o_bpFaced 18 surface   19 score
         20 player_role
        """
        # Accumulators: list of per-match values
        acc: dict[str, list[float]] = {
            "ace_rate": [],
            "df_rate": [],
            "first_serve_pct": [],
            "first_serve_win_pct": [],
            "second_serve_win_pct": [],
            "bp_save_rate": [],
            "hold_rate": [],
            "opp_ace_rate": [],
            "opp_first_serve_win_pct": [],
            "opp_bp_faced_rate": [],
            "break_rate": [],
            "return_pts_won_pct": [],
        }

        # Surface win-rate tracking
        surface_wins: dict[str, list[int]] = {
            "Hard": [], "Clay": [], "Grass": []
        }

        # 3-set match tracking
        three_set_played = 0
        three_set_won = 0

        for row in rows:
            (
                p_ace, p_df, p_svpt, p_1stIn, p_1stWon, p_2ndWon,
                p_SvGms, p_bpSaved, p_bpFaced,
                o_ace, o_df, o_svpt, o_1stIn, o_1stWon, o_2ndWon,
                o_SvGms, o_bpSaved, o_bpFaced,
                surface_val, score, player_role,
            ) = row

            won = player_role == "winner"

            # Surface win tracking
            s = str(surface_val).strip() if surface_val else ""
            for surf in ("Hard", "Clay", "Grass"):
                if s.lower() == surf.lower():
                    surface_wins[surf].append(1 if won else 0)

            # 3-set match detection (best-of-3 with score like "6-4 4-6 6-3")
            if score:
                sets_played = len(str(score).strip().split())
                if sets_played == 3:
                    three_set_played += 1
                    if won:
                        three_set_won += 1

            # Player serve stats
            p_svpt_f = float(p_svpt) if p_svpt is not None else 0.0
            if p_svpt_f > 0:
                acc["ace_rate"].append(float(p_ace or 0) / p_svpt_f)
                acc["df_rate"].append(float(p_df or 0) / p_svpt_f)
                acc["first_serve_pct"].append(float(p_1stIn or 0) / p_svpt_f)

                p_1stIn_f = float(p_1stIn or 0)
                if p_1stIn_f > 0:
                    acc["first_serve_win_pct"].append(
                        float(p_1stWon or 0) / p_1stIn_f
                    )
                    second_den = p_svpt_f - p_1stIn_f
                    if second_den > 0:
                        acc["second_serve_win_pct"].append(
                            float(p_2ndWon or 0) / second_den
                        )

                p_bpFaced_f = float(p_bpFaced or 0)
                if p_bpFaced_f > 0:
                    acc["bp_save_rate"].append(
                        float(p_bpSaved or 0) / p_bpFaced_f
                    )

                p_SvGms_f = float(p_SvGms or 0)
                if p_SvGms_f > 0:
                    hold = (
                        p_SvGms_f - (p_bpFaced_f - float(p_bpSaved or 0))
                    ) / p_SvGms_f
                    acc["hold_rate"].append(max(0.0, min(1.0, hold)))

            # Opponent serve stats (player's return context)
            o_svpt_f = float(o_svpt) if o_svpt is not None else 0.0
            if o_svpt_f > 0:
                acc["opp_ace_rate"].append(float(o_ace or 0) / o_svpt_f)
                return_pts = (
                    1.0
                    - (float(o_1stWon or 0) + float(o_2ndWon or 0)) / o_svpt_f
                )
                acc["return_pts_won_pct"].append(return_pts)

                o_1stIn_f = float(o_1stIn or 0)
                if o_1stIn_f > 0:
                    acc["opp_first_serve_win_pct"].append(
                        float(o_1stWon or 0) / o_1stIn_f
                    )

                o_SvGms_f = float(o_SvGms or 0)
                if o_SvGms_f > 0:
                    acc["opp_bp_faced_rate"].append(
                        float(o_bpFaced or 0) / o_SvGms_f
                    )

                o_bpFaced_f = float(o_bpFaced or 0)
                if o_bpFaced_f > 0:
                    acc["break_rate"].append(
                        (o_bpFaced_f - float(o_bpSaved or 0)) / o_bpFaced_f
                    )

        # Average each stat
        avgs: dict[str, float] = {}
        for key, values in acc.items():
            avgs[key] = sum(values) / len(values) if values else 0.0

        # Surface win rates (for TV)
        surface_win_rates = []
        for surf_vals in surface_wins.values():
            if surf_vals:
                surface_win_rates.append(sum(surf_vals) / len(surf_vals))

        if len(surface_win_rates) >= 2:
            mean_wr = sum(surface_win_rates) / len(surface_win_rates)
            variance = sum((x - mean_wr) ** 2 for x in surface_win_rates) / len(
                surface_win_rates
            )
            avgs["surface_win_rate_std"] = math.sqrt(variance)
        else:
            avgs["surface_win_rate_std"] = 0.0

        # 3-set win rate
        if three_set_played > 0:
            avgs["three_set_win_rate"] = three_set_won / three_set_played
        else:
            avgs["three_set_win_rate"] = 0.5  # neutral default

        return avgs

    def _build_vector(
        self,
        player_name: str,
        surface: str,
        matches_used: int,
        stats: dict,
        surface_fallback: bool,
    ) -> ArchetypeVector:
        """Apply the dimension formulae to aggregated stats."""

        # ---- SD — Serve Dominance (COMPUTED) ----------------------------
        ace_sc = _scale(stats.get("ace_rate", 0.0), 0.00, 0.12)
        fsw_sc = _scale(stats.get("first_serve_win_pct", 0.0), 0.55, 0.85)
        ssw_sc = _scale(stats.get("second_serve_win_pct", 0.0), 0.40, 0.65)
        bps_sc = _scale(stats.get("bp_save_rate", 0.0), 0.50, 0.85)
        df_sc = _scale(stats.get("df_rate", 0.0), 0.00, 0.08)

        sd_raw = (
            0.30 * ace_sc
            + 0.25 * fsw_sc
            + 0.25 * ssw_sc
            + 0.10 * bps_sc
            - 0.10 * df_sc
        )
        SD = max(0.0, min(100.0, sd_raw))

        # ---- BA — Baseline Aggression (PROXY) ---------------------------
        ret_sc = _scale(stats.get("return_pts_won_pct", 0.0), 0.25, 0.50)
        brk_sc = _scale(stats.get("break_rate", 0.0), 0.10, 0.45)

        ba_raw = 0.45 * ret_sc + 0.40 * brk_sc + 0.15 * (100.0 - SD)
        BA = max(0.0, min(100.0, ba_raw))

        # ---- PE — Physical Endurance (PROXY) ----------------------------
        tsr_sc = _scale(stats.get("three_set_win_rate", 0.5), 0.40, 0.75)

        pe_raw = (
            0.50 * tsr_sc
            + 0.30 * bps_sc
            + 0.20 * ret_sc
        )
        PE = max(0.0, min(100.0, pe_raw))

        # ---- TV — Tactical Variety (PROXY) ------------------------------
        suf_std = stats.get("surface_win_rate_std", 0.0)
        suf_sc = _scale(suf_std, 0.00, 0.25)  # higher std → higher scaled
        hold_sc = _scale(stats.get("hold_rate", 0.0), 0.60, 0.95)

        # Low surface std → high TV, so invert
        tv_raw = (
            0.60 * (100.0 - suf_sc)
            + 0.25 * hold_sc
            + 0.15 * bps_sc
        )
        TV = max(0.0, min(100.0, tv_raw))

        # ---- Data quality annotations -----------------------------------
        dq: dict[str, str] = {
            "SD": "computed",
            "BA": "proxy — charting data will improve this",
            "PE": "proxy — rally length data will improve this",
            "TV": "proxy — shot distribution data will improve this",
        }
        if surface_fallback:
            for k in dq:
                dq[k] += " (surface fallback: insufficient data, using all surfaces)"

        return ArchetypeVector(
            SD=SD,
            BA=BA,
            PE=PE,
            TV=TV,
            data_quality=dq,
            player_name=player_name,
            surface=surface,
            matches_used=matches_used,
        )
