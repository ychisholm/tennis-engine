"""
Markov probability engine for tennis win prediction (Component 2A).

Computes exact win probabilities at every level of tennis scoring:
point → game → tiebreak → set → match.
"""

from functools import lru_cache


# ---------------------------------------------------------------------------
# Game win probability
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def game_win_prob(p: float, points_server: int = 0, points_receiver: int = 0) -> float:
    """
    Return the probability that the server wins the current game.

    Args:
        p: Probability the server wins any given point.
        points_server: Server's current points (0-3 = 0/15/30/40).
        points_receiver: Receiver's current points (0-3 = 0/15/30/40).

    Returns:
        Float in [0, 1] — probability server wins the game.

    Notes:
        At deuce (3-3) the closed-form solution is p² / (p² + (1-p)²).
        All other states are solved by backward induction.
    """
    q = 1.0 - p

    # Server won
    if points_server >= 4 and points_server - points_receiver >= 2:
        return 1.0
    # Receiver won
    if points_receiver >= 4 and points_receiver - points_server >= 2:
        return 0.0
    # Deuce (both >= 3)
    if points_server >= 3 and points_receiver >= 3:
        return (p * p) / (p * p + q * q)

    return (
        p * game_win_prob(p, points_server + 1, points_receiver)
        + q * game_win_prob(p, points_server, points_receiver + 1)
    )


# ---------------------------------------------------------------------------
# Tiebreak win probability
# ---------------------------------------------------------------------------

def _tiebreak_server(point_number: int) -> bool:
    """Return True if the original server serves point `point_number` (0-indexed).

    Tiebreak serve order:
      - Point 0: server (1 point)
      - Points 1-2: receiver (2 points)
      - Points 3-4: server (2 points)
      - Points 5-6: receiver (2 points)  … and so on
    So for n >= 1, the 2-point block index = (n-1) // 2.
    Block 0 → receiver, block 1 → server, block 2 → receiver …
    Server serves when block index is ODD.
    """
    if point_number == 0:
        return True
    return ((point_number - 1) // 2) % 2 == 1


@lru_cache(maxsize=None)
def tiebreak_win_prob(
    p: float,
    q: float,
    points_server: int = 0,
    points_receiver: int = 0,
) -> float:
    """
    Return the probability that the server wins the tiebreak.

    Args:
        p: P(server wins a point on their own serve).
        q: P(server wins a point on opponent's serve).
        points_server: Server's current tiebreak points.
        points_receiver: Receiver's current tiebreak points.

    Returns:
        Float in [0, 1].

    Notes:
        Server serves the first point; thereafter serve alternates every 2 points.
        At any n-n state with n >= 6 (tiebreak deuce) the closed form
        W = p*q / (1 - p - q + 2*p*q) is used, derived by solving the two
        alternating-serve deuce equations simultaneously.
    """
    # Server wins tiebreak
    if points_server >= 7 and points_server - points_receiver >= 2:
        return 1.0
    # Receiver wins tiebreak
    if points_receiver >= 7 and points_receiver - points_server >= 2:
        return 0.0

    # Tiebreak deuce (n-n, n >= 6): closed-form avoids infinite recursion.
    # Derivation: from any tied state the 2-point mini-game equations give
    #   W = p*q / (1 - (p + q - 2*p*q))  regardless of whose serve starts.
    if points_server >= 6 and points_server == points_receiver:
        denominator = 1.0 - p - q + 2.0 * p * q
        return max(0.0, min(1.0, (p * q) / denominator))

    point_index = points_server + points_receiver
    server_serves = _tiebreak_server(point_index)
    p_point = p if server_serves else q

    return (
        p_point * tiebreak_win_prob(p, q, points_server + 1, points_receiver)
        + (1.0 - p_point) * tiebreak_win_prob(p, q, points_server, points_receiver + 1)
    )


# ---------------------------------------------------------------------------
# Lookup tables (pre-computed at module load)
# ---------------------------------------------------------------------------

def _build_tables():
    import math
    ps = [round(0.35 + i * 0.01, 2) for i in range(51)]  # 0.35 … 0.85

    game_table = {p: game_win_prob(p) for p in ps}

    tiebreak_table = {}
    for p in ps:
        for q in ps:
            tiebreak_table[(p, q)] = tiebreak_win_prob(p, q)

    return game_table, tiebreak_table


GAME_TABLE, TIEBREAK_TABLE = _build_tables()


# ---------------------------------------------------------------------------
# Set win probability
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def set_win_prob(
    p: float,
    q: float,
    games_server: int = 0,
    games_receiver: int = 0,
) -> float:
    """
    Return the probability that the server wins the current set.

    Args:
        p: P(server wins a game on their own serve) — i.e. game_win_prob(p_hat_server).
        q: P(server wins a game on opponent's serve) — i.e. 1 - game_win_prob(p_hat_receiver).
        games_server: Server's current games in this set.
        games_receiver: Receiver's current games in this set.

    Returns:
        Float in [0, 1].

    Notes:
        Tiebreak is played at 6-6. Server in the set is the server throughout
        their own service games; q captures the cross-serve probability.
    """
    # Set won by server
    if games_server >= 6 and games_server - games_receiver >= 2:
        return 1.0
    if games_server == 7 and games_receiver <= 5:
        return 1.0
    # Set won by receiver
    if games_receiver >= 6 and games_receiver - games_server >= 2:
        return 0.0
    if games_receiver == 7 and games_server <= 5:
        return 0.0

    game_index = games_server + games_receiver  # 0-indexed game number

    # At 6-6 → tiebreak
    if games_server == 6 and games_receiver == 6:
        tb = tiebreak_win_prob(p, q)
        return tb

    # Whose serve is it? Server of the set serves games 0, 2, 4, … (even-indexed).
    server_serves_this_game = (game_index % 2 == 0)

    if server_serves_this_game:
        p_game = p  # P(set-server wins this service game)
    else:
        p_game = q  # P(set-server wins opponent's service game)

    return (
        p_game * set_win_prob(p, q, games_server + 1, games_receiver)
        + (1.0 - p_game) * set_win_prob(p, q, games_server, games_receiver + 1)
    )


# ---------------------------------------------------------------------------
# Match win probability
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def match_win_prob(
    p_hat_A: float,
    p_hat_B: float,
    sets_A: int = 0,
    sets_B: int = 0,
    games_A: int = 0,
    games_B: int = 0,
    points_A: int = 0,
    points_B: int = 0,
    serving: str = "A",
    best_of: int = 3,
) -> float:
    """
    Return P(Player A wins match) given the full current match state.

    Args:
        p_hat_A: P(A wins a point on their own serve).
        p_hat_B: P(B wins a point on their own serve).
        sets_A: Sets won by A so far.
        sets_B: Sets won by B so far.
        games_A: Games won by A in the current set.
        games_B: Games won by B in the current set.
        points_A: Points won by A in the current game (0-3).
        points_B: Points won by B in the current game (0-3).
        serving: 'A' if A is currently serving, 'B' otherwise.
        best_of: 3 or 5.

    Returns:
        Float in [0, 1] clipped to that range.
    """
    sets_to_win = (best_of + 1) // 2

    # Match already decided
    if sets_A >= sets_to_win:
        return 1.0
    if sets_B >= sets_to_win:
        return 0.0

    # ---- Probability A wins the current game ----
    if serving == "A":
        p_game_A = game_win_prob(p_hat_A, points_A, points_B)
    else:
        # B is serving; prob A wins = 1 - prob B wins their service game
        p_game_A = 1.0 - game_win_prob(p_hat_B, points_B, points_A)

    # ---- Prob A wins the current set (from game level) ----
    # p = P(A wins a game on A's serve), q = P(A wins a game on B's serve)
    p_set_level = game_win_prob(p_hat_A)
    q_set_level = 1.0 - game_win_prob(p_hat_B)

    # Determine which player is the "server" of the current set.
    # We track this via the current game index.
    game_index = games_A + games_B
    # A serves on even game indices if A served first in the set; detect from `serving`.
    # A serves the current game iff serving == 'A'.
    a_serves_current_game = (serving == "A")
    # If A serves the current game and game_index is even → A served first in set (index 0).
    set_server_is_A = (game_index % 2 == 0) == a_serves_current_game

    if set_server_is_A:
        p_set = p_set_level
        q_set = q_set_level
        gs_A, gs_B = games_A, games_B
    else:
        # B is the set server; reframe: p=B wins own serve, q=B wins A's serve
        p_set = game_win_prob(p_hat_B)
        q_set = 1.0 - game_win_prob(p_hat_A)
        gs_A, gs_B = games_B, games_A  # from B's (set-server) perspective

    p_set_A_wins = set_win_prob(p_set, q_set, gs_A, gs_B)
    if not set_server_is_A:
        p_set_A_wins = 1.0 - p_set_A_wins

    # ---- Forward-simulate remaining sets ----
    # After the current set resolves, serve alternates by set.
    # Remaining sets: use set_win_prob from 0-0 for each set.
    p_set_A_on_A_serve = set_win_prob(p_set_level, q_set_level)
    p_set_A_on_B_serve = 1.0 - set_win_prob(
        game_win_prob(p_hat_B), 1.0 - game_win_prob(p_hat_A)
    )

    @lru_cache(maxsize=None)
    def future_sets(sa, sb, a_serves_next):
        if sa >= sets_to_win:
            return 1.0
        if sb >= sets_to_win:
            return 0.0
        pw = p_set_A_on_A_serve if a_serves_next else p_set_A_on_B_serve
        return pw * future_sets(sa + 1, sb, not a_serves_next) + (1 - pw) * future_sets(
            sa, sb + 1, not a_serves_next
        )

    # Determine who serves next set
    a_serves_next_set = not a_serves_current_game  # serve flips each set

    result = (
        p_set_A_wins * future_sets(sets_A + 1, sets_B, a_serves_next_set)
        + (1.0 - p_set_A_wins) * future_sets(sets_A, sets_B + 1, not a_serves_next_set)
    )

    return max(0.0, min(1.0, result))


# ---------------------------------------------------------------------------
# Live probability interface
# ---------------------------------------------------------------------------

def compute_live_probabilities(
    p_hat_A: float,
    p_hat_B: float,
    match_state: dict,
) -> dict:
    """
    Compute live win probabilities for Player A given current match state.

    Args:
        p_hat_A: P(A wins a point on their own serve).
        p_hat_B: P(B wins a point on their own serve).
        match_state: Dict with keys:
            - sets_A (int)
            - sets_B (int)
            - games_A (int)
            - games_B (int)
            - points_A (int, 0-3)
            - points_B (int, 0-3)
            - serving_player ('A' or 'B')
            - best_of (3 or 5)

    Returns:
        Dict with keys:
            - P_game_A (float): P(A wins current game)
            - P_set_A (float): P(A wins current set)
            - P_match_A (float): P(A wins match)
    """
    sets_A = match_state["sets_A"]
    sets_B = match_state["sets_B"]
    games_A = match_state["games_A"]
    games_B = match_state["games_B"]
    points_A = match_state["points_A"]
    points_B = match_state["points_B"]
    serving = match_state["serving_player"]
    best_of = match_state.get("best_of", 3)

    # P(A wins current game)
    if serving == "A":
        p_game_A = game_win_prob(p_hat_A, points_A, points_B)
    else:
        p_game_A = 1.0 - game_win_prob(p_hat_B, points_B, points_A)

    # P(A wins current set) — reuse set_win_prob logic
    p_set_level = game_win_prob(p_hat_A)
    q_set_level = 1.0 - game_win_prob(p_hat_B)

    game_index = games_A + games_B
    a_serves_current_game = (serving == "A")
    set_server_is_A = (game_index % 2 == 0) == a_serves_current_game

    if set_server_is_A:
        p_set = p_set_level
        q_set = q_set_level
        gs_server, gs_receiver = games_A, games_B
        p_set_A = set_win_prob(p_set, q_set, gs_server, gs_receiver)
    else:
        p_set = game_win_prob(p_hat_B)
        q_set = 1.0 - game_win_prob(p_hat_A)
        gs_server, gs_receiver = games_B, games_A
        p_set_A = 1.0 - set_win_prob(p_set, q_set, gs_server, gs_receiver)

    p_match_A = match_win_prob(
        p_hat_A, p_hat_B,
        sets_A, sets_B,
        games_A, games_B,
        points_A, points_B,
        serving, best_of,
    )

    return {
        "P_game_A": max(0.0, min(1.0, p_game_A)),
        "P_set_A": max(0.0, min(1.0, p_set_A)),
        "P_match_A": max(0.0, min(1.0, p_match_A)),
    }
