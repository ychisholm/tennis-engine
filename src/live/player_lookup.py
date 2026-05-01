"""
player_lookup.py — PostgreSQL-backed player profile lookup for the live engine.

Computes per-player p0 (serve-point win rate by surface) and archetype
(SD / BA / PE / TV) from the core.atp_matches / core.wta_matches tables.
Results are cached in-process for the lifetime of the module so repeated
lookups for the same player within one session hit the DB only once.

Fallback hierarchy
------------------
1. Surface-specific p0 with ≥ 500 serve points (2015-onwards data).
2. Cross-surface average p0 with ≥ 500 serve points.
3. Tour-level constant: 0.63 (ATP) / 0.60 (WTA).
4. Neutral profile if the player name is not found in the DB at all:
       p0_hard=0.63, p0_clay=0.60, p0_grass=0.65
       archetype sd=60, ba=60, pe=60, tv=60

If DATABASE_URL is not set, or a database error occurs, the neutral profile
is returned and a warning is logged — the engine never crashes on lookup.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

import psycopg2

_log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MIN_DATE         = 20150101   # matches before this year are excluded
_MIN_SERVE_PTS    = 500        # minimum surface-specific serve points for a valid p0

_ATP_P0_DEFAULT   = 0.63
_WTA_P0_DEFAULT   = 0.60

# "Player not found at all" neutral profile
_NEUTRAL = {
    "p0_hard":   0.63,
    "p0_clay":   0.60,
    "p0_grass":  0.65,
    "archetype": {"sd": 60.0, "ba": 60.0, "pe": 60.0, "tv": 60.0},
}

# ── Module-level in-process cache ─────────────────────────────────────────────
_cache: dict[str, dict] = {}


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_player(name: str, tour: str = "atp") -> dict:
    """Return the player profile dict expected by LiveMatch.

    Parameters
    ----------
    name : str
        Player name exactly as returned by TennisAPI.
    tour : str
        ``"atp"`` or ``"wta"`` — determines which table is queried first.

    Returns
    -------
    dict
        Keys: ``name``, ``p0_hard``, ``p0_clay``, ``p0_grass``, ``archetype``.
        ``archetype`` is a dict with keys ``sd``, ``ba``, ``pe``, ``tv``
        (each 0–100 float).
    """
    cache_key = f"{name}|{tour.lower()}"
    if cache_key in _cache:
        return _cache[cache_key]

    url = os.environ.get("DATABASE_URL")
    if not url:
        _log.warning(
            "[player_lookup] DATABASE_URL not set — neutral defaults for '%s'", name
        )
        result = {**_NEUTRAL, "name": name}
        _cache[cache_key] = result
        return result

    try:
        conn = psycopg2.connect(url)
        try:
            result = _fetch_profile(conn, name, tour)
        finally:
            conn.close()
    except Exception as exc:
        _log.warning(
            "[player_lookup] DB error for '%s': %s — neutral defaults", name, exc
        )
        result = {**_NEUTRAL, "name": name}

    _cache[cache_key] = result
    return result


def clear_cache() -> None:
    """Flush the in-process player cache (useful between sessions / tests)."""
    _cache.clear()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_profile(conn, name: str, tour: str) -> dict:
    """Query PostgreSQL and build the full player profile dict."""
    tour = tour.lower()
    primary   = "core.atp_matches" if tour == "atp" else "core.wta_matches"
    secondary = "core.wta_matches" if tour == "atp" else "core.atp_matches"
    default_p0 = _ATP_P0_DEFAULT if tour == "atp" else _WTA_P0_DEFAULT

    for table in (primary, secondary):
        if not _player_exists(conn, name, table):
            continue

        p0_hard  = _p0_for_surface(conn, name, table, "Hard",  default_p0)
        p0_clay  = _p0_for_surface(conn, name, table, "Clay",  default_p0)
        p0_grass = _p0_for_surface(conn, name, table, "Grass", default_p0)
        archetype = _compute_archetype(conn, name, table)

        _log.debug(
            "[player_lookup] '%s' found in %s  p0=(%.3f/%.3f/%.3f)",
            name, table, p0_hard, p0_clay, p0_grass,
        )
        return {
            "name":      name,
            "p0_hard":   p0_hard,
            "p0_clay":   p0_clay,
            "p0_grass":  p0_grass,
            "archetype": archetype,
        }

    # Not found in either table
    _log.warning(
        "[player_lookup] '%s' not found in PostgreSQL — using neutral defaults", name
    )
    return {**_NEUTRAL, "name": name}


def _player_exists(conn, name: str, table: str) -> bool:
    """Return True if *name* appears as winner or loser in *table*."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {table}"
            f" WHERE winner_name = %s OR loser_name = %s LIMIT 1",
            [name, name],
        )
        return cur.fetchone() is not None


# ── p0 computation ────────────────────────────────────────────────────────────

def _p0_for_surface(
    conn,
    name: str,
    table: str,
    surface: str,   # "Hard" | "Clay" | "Grass"
    default_p0: float,
) -> float:
    """
    Compute p0 on *surface* using the following fallback chain:
      1. Surface-specific aggregate with ≥ _MIN_SERVE_PTS
      2. All-surface aggregate with ≥ _MIN_SERVE_PTS
      3. *default_p0* (tour-level constant)
    """
    # 1. Surface-specific
    val, pts = _p0_query(conn, name, table, surface)
    if val is not None and pts >= _MIN_SERVE_PTS:
        return val

    # 2. Cross-surface fallback
    val_all, pts_all = _p0_query(conn, name, table, None)
    if val_all is not None and pts_all >= _MIN_SERVE_PTS:
        return val_all

    # 3. Tour-level constant
    return default_p0


def _p0_query(
    conn,
    name: str,
    table: str,
    surface: Optional[str],
) -> tuple[Optional[float], int]:
    """
    Run the serve-points-won aggregation.

    p0 = (SUM(1stWon) + SUM(2ndWon)) / SUM(svpt)

    Returns (p0, total_serve_points).  Returns (None, 0) on no data.
    """
    if surface is not None:
        surf_clause_w = "AND LOWER(surface) = LOWER(%s)"
        surf_clause_l = "AND LOWER(surface) = LOWER(%s)"
        params = [name, surface, name, surface]
    else:
        surf_clause_w = ""
        surf_clause_l = ""
        params = [name, name]

    sql = f"""
        SELECT SUM(pts_won), SUM(svpt)
        FROM (
            SELECT
                COALESCE(w_1stWon, 0) + COALESCE(w_2ndWon, 0) AS pts_won,
                COALESCE(w_svpt, 0)                            AS svpt
            FROM {table}
            WHERE winner_name = %s
              {surf_clause_w}
              AND tourney_date >= {_MIN_DATE}
              AND w_svpt IS NOT NULL
              AND w_svpt > 0

            UNION ALL

            SELECT
                COALESCE(l_1stWon, 0) + COALESCE(l_2ndWon, 0) AS pts_won,
                COALESCE(l_svpt, 0)                            AS svpt
            FROM {table}
            WHERE loser_name = %s
              {surf_clause_l}
              AND tourney_date >= {_MIN_DATE}
              AND l_svpt IS NOT NULL
              AND l_svpt > 0
        ) pts
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if row and row[0] is not None and row[1] and float(row[1]) > 0:
        return float(row[0]) / float(row[1]), int(row[1])
    return None, 0


# ── Archetype computation ─────────────────────────────────────────────────────

def _compute_archetype(conn, name: str, table: str) -> dict:
    """
    Derive {sd, ba, pe, tv} from career match stats in *table*.

    Uses the identical column selection, aggregation logic, and dimension
    formulae as src/engine/archetype_engine.py so both paths produce
    consistent outputs.  Falls back to neutral {60, 60, 60, 60} if fewer
    than 5 matches are available.
    """
    rows = _fetch_archetype_rows(conn, name, table)
    if len(rows) < 5:
        _log.debug(
            "[player_lookup] '%s' has only %d archetype rows in %s — neutral archetype",
            name, len(rows), table,
        )
        return {"sd": 60.0, "ba": 60.0, "pe": 60.0, "tv": 60.0}

    stats = _aggregate_stats(rows)
    return _build_archetype(stats)


def _fetch_archetype_rows(conn, name: str, table: str) -> list[tuple]:
    """
    Fetch the same columns as archetype_engine._fetch_rows, from 2015 onwards.

    Column order (matches _aggregate_stats expectations):
        0  p_ace     1  p_df      2  p_svpt    3  p_1stIn
        4  p_1stWon  5  p_2ndWon  6  p_SvGms   7  p_bpSaved
        8  p_bpFaced 9  o_ace    10  o_df     11  o_svpt
       12  o_1stIn  13  o_1stWon 14  o_2ndWon 15  o_SvGms
       16  o_bpSaved 17 o_bpFaced 18 surface   19 score
       20  player_role
    """
    sql = f"""
        SELECT
            w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
            w_SvGms, w_bpSaved, w_bpFaced,
            l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
            l_SvGms, l_bpSaved, l_bpFaced,
            surface, score,
            'winner' AS player_role
        FROM {table}
        WHERE winner_name = %s
          AND w_svpt IS NOT NULL
          AND tourney_date >= {_MIN_DATE}

        UNION ALL

        SELECT
            l_ace, l_df, l_svpt, l_1stIn, l_1stWon, l_2ndWon,
            l_SvGms, l_bpSaved, l_bpFaced,
            w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon,
            w_SvGms, w_bpSaved, w_bpFaced,
            surface, score,
            'loser' AS player_role
        FROM {table}
        WHERE loser_name = %s
          AND l_svpt IS NOT NULL
          AND tourney_date >= {_MIN_DATE}
    """
    with conn.cursor() as cur:
        cur.execute(sql, [name, name])
        return cur.fetchall()


# ── Aggregation + dimension formulae (mirrors archetype_engine.py) ────────────

def _scale(value: float, lo: float, hi: float) -> float:
    """Linear interpolation lo→0, hi→100, clipped to [0, 100]."""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def _aggregate_stats(rows: list[tuple]) -> dict:
    """
    Aggregate per-match row tuples into career-average stat dict.
    Mirrors ArchetypeEngine._aggregate_stats exactly.
    """
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

    surface_wins: dict[str, list[int]] = {"Hard": [], "Clay": [], "Grass": []}
    three_set_played = 0
    three_set_won    = 0

    for row in rows:
        (
            p_ace, p_df, p_svpt, p_1stIn, p_1stWon, p_2ndWon,
            p_SvGms, p_bpSaved, p_bpFaced,
            o_ace, o_df, o_svpt, o_1stIn, o_1stWon, o_2ndWon,
            o_SvGms, o_bpSaved, o_bpFaced,
            surface_val, score, player_role,
        ) = row

        won = player_role == "winner"

        s = str(surface_val).strip() if surface_val else ""
        for surf in ("Hard", "Clay", "Grass"):
            if s.lower() == surf.lower():
                surface_wins[surf].append(1 if won else 0)

        if score:
            sets_played = len(str(score).strip().split())
            if sets_played == 3:
                three_set_played += 1
                if won:
                    three_set_won += 1

        p_svpt_f = float(p_svpt) if p_svpt is not None else 0.0
        if p_svpt_f > 0:
            acc["ace_rate"].append(float(p_ace or 0) / p_svpt_f)
            acc["df_rate"].append(float(p_df or 0) / p_svpt_f)
            acc["first_serve_pct"].append(float(p_1stIn or 0) / p_svpt_f)

            p_1stIn_f = float(p_1stIn or 0)
            if p_1stIn_f > 0:
                acc["first_serve_win_pct"].append(float(p_1stWon or 0) / p_1stIn_f)
                second_den = p_svpt_f - p_1stIn_f
                if second_den > 0:
                    acc["second_serve_win_pct"].append(float(p_2ndWon or 0) / second_den)

            p_bpFaced_f = float(p_bpFaced or 0)
            if p_bpFaced_f > 0:
                acc["bp_save_rate"].append(float(p_bpSaved or 0) / p_bpFaced_f)

            p_SvGms_f = float(p_SvGms or 0)
            if p_SvGms_f > 0:
                hold = (p_SvGms_f - (p_bpFaced_f - float(p_bpSaved or 0))) / p_SvGms_f
                acc["hold_rate"].append(max(0.0, min(1.0, hold)))

        o_svpt_f = float(o_svpt) if o_svpt is not None else 0.0
        if o_svpt_f > 0:
            acc["opp_ace_rate"].append(float(o_ace or 0) / o_svpt_f)
            return_pts = 1.0 - (float(o_1stWon or 0) + float(o_2ndWon or 0)) / o_svpt_f
            acc["return_pts_won_pct"].append(return_pts)

            o_1stIn_f = float(o_1stIn or 0)
            if o_1stIn_f > 0:
                acc["opp_first_serve_win_pct"].append(float(o_1stWon or 0) / o_1stIn_f)

            o_SvGms_f = float(o_SvGms or 0)
            if o_SvGms_f > 0:
                acc["opp_bp_faced_rate"].append(float(o_bpFaced or 0) / o_SvGms_f)

            o_bpFaced_f = float(o_bpFaced or 0)
            if o_bpFaced_f > 0:
                acc["break_rate"].append(
                    (o_bpFaced_f - float(o_bpSaved or 0)) / o_bpFaced_f
                )

    avgs: dict[str, float] = {
        k: sum(v) / len(v) if v else 0.0
        for k, v in acc.items()
    }

    surface_win_rates = [
        sum(v) / len(v) for v in surface_wins.values() if v
    ]
    if len(surface_win_rates) >= 2:
        mean_wr  = sum(surface_win_rates) / len(surface_win_rates)
        variance = sum((x - mean_wr) ** 2 for x in surface_win_rates) / len(surface_win_rates)
        avgs["surface_win_rate_std"] = math.sqrt(variance)
    else:
        avgs["surface_win_rate_std"] = 0.0

    avgs["three_set_win_rate"] = (
        three_set_won / three_set_played if three_set_played > 0 else 0.5
    )

    return avgs


def _build_archetype(stats: dict) -> dict:
    """
    Apply the dimension formulae to aggregated stats and return a plain dict.
    Mirrors ArchetypeEngine._build_vector exactly (same thresholds / weights).
    """
    # ── SD — Serve Dominance ─────────────────────────────────────────────────
    ace_sc = _scale(stats.get("ace_rate", 0.0),             0.00, 0.12)
    fsw_sc = _scale(stats.get("first_serve_win_pct", 0.0),  0.55, 0.85)
    ssw_sc = _scale(stats.get("second_serve_win_pct", 0.0), 0.40, 0.65)
    bps_sc = _scale(stats.get("bp_save_rate", 0.0),         0.50, 0.85)
    df_sc  = _scale(stats.get("df_rate", 0.0),              0.00, 0.08)

    SD = max(0.0, min(100.0,
        0.30 * ace_sc + 0.25 * fsw_sc + 0.25 * ssw_sc + 0.10 * bps_sc - 0.10 * df_sc
    ))

    # ── BA — Baseline Aggression ─────────────────────────────────────────────
    ret_sc = _scale(stats.get("return_pts_won_pct", 0.0), 0.25, 0.50)
    brk_sc = _scale(stats.get("break_rate", 0.0),         0.10, 0.45)

    BA = max(0.0, min(100.0,
        0.45 * ret_sc + 0.40 * brk_sc + 0.15 * (100.0 - SD)
    ))

    # ── PE — Physical Endurance ──────────────────────────────────────────────
    tsr_sc = _scale(stats.get("three_set_win_rate", 0.5), 0.40, 0.75)

    PE = max(0.0, min(100.0,
        0.50 * tsr_sc + 0.30 * bps_sc + 0.20 * ret_sc
    ))

    # ── TV — Tactical Variety ────────────────────────────────────────────────
    suf_std = stats.get("surface_win_rate_std", 0.0)
    suf_sc  = _scale(suf_std, 0.00, 0.25)
    hold_sc = _scale(stats.get("hold_rate", 0.0), 0.60, 0.95)

    TV = max(0.0, min(100.0,
        0.60 * (100.0 - suf_sc) + 0.25 * hold_sc + 0.15 * bps_sc
    ))

    return {"sd": SD, "ba": BA, "pe": PE, "tv": TV}
