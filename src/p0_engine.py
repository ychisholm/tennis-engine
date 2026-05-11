"""Compute per-player serve point-win probability (p0) for ml_game_level players."""

from rapidfuzz import fuzz, process


def compute_p0_table(con) -> list[dict]:
    rows = con.execute(
        """
        SELECT winner_name AS name, w_svpt, w_1stWon, w_2ndWon
        FROM core.atp_matches
        WHERE w_svpt IS NOT NULL AND w_1stWon IS NOT NULL AND w_2ndWon IS NOT NULL
        UNION ALL
        SELECT loser_name AS name, l_svpt, l_1stWon, l_2ndWon
        FROM core.atp_matches
        WHERE l_svpt IS NOT NULL AND l_1stWon IS NOT NULL AND l_2ndWon IS NOT NULL
        """
    ).fetchall()

    agg: dict[str, list[int]] = {}
    total_won = 0
    total_played = 0
    for name, svpt, first_won, second_won in rows:
        won = int(first_won) + int(second_won)
        played = int(svpt)
        if name not in agg:
            agg[name] = [0, 0]
        agg[name][0] += won
        agg[name][1] += played
        total_won += won
        total_played += played

    league_avg_p0 = total_won / total_played if total_played > 0 else 0.65

    ml_names_rows = con.execute(
        """
        SELECT DISTINCT name FROM (
            SELECT player_A AS name FROM core.ml_game_level
            UNION
            SELECT player_B AS name FROM core.ml_game_level
        )
        """
    ).fetchall()
    ml_names = [r[0] for r in ml_names_rows]

    atp_name_list = list(agg.keys())

    out: list[dict] = []
    for name in ml_names:
        if name in agg:
            sp_won, sp_played = agg[name]
            out.append({
                "player_name": name,
                "p0": sp_won / sp_played if sp_played > 0 else league_avg_p0,
                "serve_points_played": sp_played,
                "serve_points_won": sp_won,
                "match_method": "direct",
            })
            continue

        match = process.extractOne(
            name, atp_name_list, scorer=fuzz.ratio, score_cutoff=90
        )
        if match is not None:
            matched_name = match[0]
            sp_won, sp_played = agg[matched_name]
            out.append({
                "player_name": name,
                "p0": sp_won / sp_played if sp_played > 0 else league_avg_p0,
                "serve_points_played": sp_played,
                "serve_points_won": sp_won,
                "match_method": "fuzzy",
            })
            continue

        out.append({
            "player_name": name,
            "p0": league_avg_p0,
            "serve_points_played": None,
            "serve_points_won": None,
            "match_method": "league_average",
        })

    return out
