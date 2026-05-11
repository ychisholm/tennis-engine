"""Markov baseline math: closed-form game / tiebreak / set probabilities."""

from functools import lru_cache

_P_MIN = 0.35
_P_MAX = 0.85


def game_win_prob(p: float) -> float:
    if p < _P_MIN:
        p = _P_MIN
    elif p > _P_MAX:
        p = _P_MAX
    q = 1.0 - p
    deuce_tail = (p * p) / (p * p + q * q)
    return (
        p ** 4
        + 4 * (p ** 4) * q
        + 10 * (p ** 4) * (q ** 2)
        + 20 * (p ** 3) * (q ** 3) * deuce_tail
    )


@lru_cache(maxsize=None)
def tiebreak_win_prob(p_a: float, p_b: float, first_server_is_a: bool) -> float:
    return _tb(p_a, p_b, first_server_is_a, 0, 0)


@lru_cache(maxsize=None)
def _tb(p_a: float, p_b: float, first_server_is_a: bool,
        score_a: int, score_b: int) -> float:
    if score_a >= 7 and score_a - score_b >= 2:
        return 1.0
    if score_b >= 7 and score_b - score_a >= 2:
        return 0.0

    if score_a == score_b and score_a >= 6:
        s = p_a * (1.0 - p_b)
        f = (1.0 - p_a) * p_b
        denom = s + f
        if denom <= 0.0:
            return 0.5
        return s / denom

    point_index = score_a + score_b
    if point_index == 0:
        first_server_serves = True
    else:
        first_server_serves = (((point_index - 1) // 2) % 2 == 1)

    a_serves = first_server_serves if first_server_is_a else (not first_server_serves)
    p_point_a = p_a if a_serves else (1.0 - p_b)

    return (
        p_point_a * _tb(p_a, p_b, first_server_is_a, score_a + 1, score_b)
        + (1.0 - p_point_a) * _tb(p_a, p_b, first_server_is_a, score_a, score_b + 1)
    )


_SET_CACHE: dict = {}


def clear_set_cache() -> None:
    _SET_CACHE.clear()


def set_win_prob(p_a: float, p_b: float, games_a: int, games_b: int,
                 next_server_is_a: bool) -> float:
    key = (p_a, p_b, games_a, games_b, next_server_is_a)
    cached = _SET_CACHE.get(key)
    if cached is not None:
        return cached

    if games_a >= 6 and games_a - games_b >= 2:
        result = 1.0
    elif games_b >= 6 and games_b - games_a >= 2:
        result = 0.0
    elif games_a >= 7 and games_b <= 6:
        result = 1.0
    elif games_b >= 7 and games_a <= 6:
        result = 0.0
    elif games_a == 6 and games_b == 6:
        result = tiebreak_win_prob(p_a, p_b, first_server_is_a=next_server_is_a)
    elif games_a == games_b and games_a >= 7:
        h_a = game_win_prob(p_a)
        h_b = game_win_prob(p_b)
        s = h_a * (1.0 - h_b)
        f = h_b * (1.0 - h_a)
        denom = s + f
        result = 0.5 if denom <= 0.0 else s / denom
    else:
        if next_server_is_a:
            hold_prob = game_win_prob(p_a)
            on_hold = set_win_prob(p_a, p_b, games_a + 1, games_b, not next_server_is_a)
            on_break = set_win_prob(p_a, p_b, games_a, games_b + 1, not next_server_is_a)
        else:
            hold_prob = game_win_prob(p_b)
            on_hold = set_win_prob(p_a, p_b, games_a, games_b + 1, not next_server_is_a)
            on_break = set_win_prob(p_a, p_b, games_a + 1, games_b, not next_server_is_a)
        result = hold_prob * on_hold + (1.0 - hold_prob) * on_break

    _SET_CACHE[key] = result
    return result
