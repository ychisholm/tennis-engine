"""State-to-Point adapter for the Step 6D live prediction wiring.

Pure module — no DB access, no I/O, no environment reads. Converts
(prev, curr) ``live.match_states`` row-dict pairs into a list of
:class:`Point` objects compatible with the streaming signal engine.

Authoritative spec: ``docs/state_machine_integration_recon.md`` §4.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

from src.live.tennis_feed import TennisFeed
from src.verification.validator import (
    GAME_A,
    GAME_B,
    _normalize_point,
    _REGULAR_GAME_EDGES,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    """Duck-typed Point compatible with the streaming signal engine.

    Matches the duck type described in ``src/signal_engine.py`` (around
    line 103 of its module docstring) and consumed by
    :class:`src.streaming_signal_engine.StreamingMatchState.process_point`.

    Fields
    ------
    set_number : int
        1-indexed match-level set number.
    game_number_in_set : int
        1..12 for regular games; 13 for tiebreak games.
    score_before : str
        Score string formatted ``"{home}-{away}"`` using regular-game
        vocabulary (``'0','15','30','40','AD'``) for regular games or
        integer strings (``'0','1','2',...``) for tiebreaks.
    Svr : int
        1 = home serves, 2 = away serves.
    PtWinner : int
        1 = home won the point, 2 = away won.
    is_tiebreak : bool
        True iff this point is part of a tiebreak game.
    """
    set_number: int
    game_number_in_set: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool


class NoLegalPathError(Exception):
    """Raised when a (prev, curr) transition has no legal point sequence.

    The worker treats this as a forensic event and skips emitting
    Points for this transition. The ``prev`` and ``curr`` attributes
    carry the row dicts (or ``None`` when raised from a path-finding
    helper that only had point-tuple context — the adapter attaches
    the full row dicts before re-raising).
    """

    def __init__(
        self,
        prev: Optional[dict],
        curr: Optional[dict],
        reason: str,
    ) -> None:
        self.prev = prev
        self.curr = curr
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalize_point_token(s) -> str:
    """``'A'``/``'ADV'`` → ``'AD'``; ``None`` / ``''`` → ``'0'``; pass through.

    Delegates to ``src.verification.validator._normalize_point`` so the
    canonical normalisation lives in exactly one place.
    """
    return _normalize_point(s)


def is_tiebreak_score(row: dict) -> bool:
    """True iff the row's current_point fields are in tiebreak vocabulary.

    Uses ``TennisFeed._is_tiebreak_score`` (the canonical detector). Both
    tokens are normalised first so ``'A'`` doesn't get mistaken for an
    integer-string outside the regular vocab.
    """
    h = normalize_point_token(row.get("home_current_point"))
    a = normalize_point_token(row.get("away_current_point"))
    return TennisFeed._is_tiebreak_score(h, a)


def format_score_before(home_pt: str, away_pt: str) -> str:
    """Format two point tokens as ``"home-away"`` for ``Point.score_before``.

    Both tokens are expected to be already normalised by the caller.
    """
    return f"{home_pt}-{away_pt}"


def count_completed_games_in_match(row: dict) -> int:
    """Total games completed in the match prior to the in-progress game.

    Sum of all (home_setN_games + away_setN_games) for completed sets
    (set indices 1..sets_won_a+sets_won_b inclusive), plus the
    in-progress set's home_current_games + away_current_games.

    On a status='finished' row, the writer duplicates the closing set's
    totals into current_games (see ``logger.py`` finished-row branch).
    In that case, current_games is omitted to avoid double-counting —
    the loop already includes the closing set via the per-set columns.
    """
    sa = row.get("home_sets_won") or 0
    sb = row.get("away_sets_won") or 0
    total = 0
    for i in range(1, sa + sb + 1):
        total += (
            (row.get(f"home_set{i}_games") or 0)
            + (row.get(f"away_set{i}_games") or 0)
        )
    if row.get("status") != "finished":
        total += (
            (row.get("home_current_games") or 0)
            + (row.get("away_current_games") or 0)
        )
    return total


def compute_server_for_game(
    first_server: str,
    total_completed_games: int,
) -> int:
    """Return 1 (home) or 2 (away) for the next game's server.

    Server of the (total_completed_games + 1)-th game is ``first_server``
    iff ``total_completed_games`` is even; otherwise the opposite side.
    """
    first_is_home = first_server == "home"
    if total_completed_games % 2 == 0:
        return 1 if first_is_home else 2
    return 2 if first_is_home else 1


def compute_set_and_game(row: dict) -> tuple[int, int]:
    """Return ``(set_number, game_number_in_set)`` for the in-progress
    game described by ``row``. set_number is 1-indexed.

    Tiebreaks (games == 6-6 with tiebreak-vocabulary points) return
    ``game_number_in_set == 13``.

    Only meaningful for in-progress rows. A row whose ``sets_won`` has
    been incremented (set-end / match-end shape) will return the *next*
    set number — callers that need the closing game's set should pass
    the prev row, not curr.
    """
    sa = row.get("home_sets_won") or 0
    sb = row.get("away_sets_won") or 0
    set_number = sa + sb + 1
    ga = row.get("home_current_games") or 0
    gb = row.get("away_current_games") or 0
    if ga == 6 and gb == 6 and is_tiebreak_score(row):
        return set_number, 13
    return set_number, ga + gb + 1


def classify_transition(
    prev: Optional[dict],
    curr: dict,
) -> str:
    """Classify the shape of a (prev, curr) transition.

    See the module-level docstring or recon §4 for the seven categories.
    """
    if prev is None:
        sa = curr.get("home_sets_won") or 0
        sb = curr.get("away_sets_won") or 0
        ga = curr.get("home_current_games") or 0
        gb = curr.get("away_current_games") or 0
        if sa == 0 and sb == 0 and ga == 0 and gb == 0:
            return "NO_PREV_TRIVIAL"
        return "NO_PREV_NONTRIVIAL"

    psa = prev.get("home_sets_won") or 0
    psb = prev.get("away_sets_won") or 0
    csa = curr.get("home_sets_won") or 0
    csb = curr.get("away_sets_won") or 0
    pga = prev.get("home_current_games") or 0
    pgb = prev.get("away_current_games") or 0
    cga = curr.get("home_current_games") or 0
    cgb = curr.get("away_current_games") or 0
    pph = normalize_point_token(prev.get("home_current_point"))
    ppa = normalize_point_token(prev.get("away_current_point"))
    cph = normalize_point_token(curr.get("home_current_point"))
    cpa = normalize_point_token(curr.get("away_current_point"))

    if (psa, psb, pga, pgb, pph, ppa) == (csa, csb, cga, cgb, cph, cpa):
        return "NO_CHANGE"

    sets_diff = (csa + csb) - (psa + psb)

    if sets_diff < 0:
        return "MALFORMED"

    if sets_diff == 0:
        # Same set: distinguish within-game from game-end.
        if (pga, pgb) == (cga, cgb):
            return "WITHIN_GAME"
        delta_pair = (cga - pga, cgb - pgb)
        if delta_pair in ((1, 0), (0, 1)):
            return "GAME_END"
        # Multi-game jump, or games regression.
        return "MALFORMED"

    if sets_diff == 1:
        # Set boundary. Two legal curr shapes (see validator.py
        # validate_match's fresh_set_start / match_end_shape):
        clean_fresh_set = (
            cga == 0 and cgb == 0 and cph == "0" and cpa == "0"
        )
        if clean_fresh_set:
            return "SET_END"
        if curr.get("status") == "finished" and cph == "0" and cpa == "0":
            # Match-end shape — closing set's games carried in
            # current_games; points zeroed by the writer.
            return "MATCH_END"
        return "MALFORMED"

    # sets_diff > 1 — caller skipped a set somehow
    return "MALFORMED"


# ---------------------------------------------------------------------------
# Path-finding helpers
# ---------------------------------------------------------------------------

def _enumerate_regular_paths(start, end):
    """Yield all shortest paths from ``start`` to ``end`` in the regular
    game graph as lists of ``(from_node, to_node, winner_side)`` edges.

    The graph is small (~18 nodes); enumeration is bounded.
    """
    # BFS forward distances.
    if start == end:
        yield []
        return

    if start not in _REGULAR_GAME_EDGES:
        # start is unknown / terminal — no outgoing edges, no paths.
        return

    dist: dict = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in _REGULAR_GAME_EDGES.get(node, []):
            if neighbor not in dist:
                dist[neighbor] = dist[node] + 1
                queue.append(neighbor)

    if end not in dist:
        return

    # Reverse adjacency annotated with the winner of each edge.
    # Edge index 0 of any node's outgoing list represents home winning
    # the next point; index 1 represents away winning. This holds
    # uniformly across the graph (including AD-40 and 40-AD where the
    # convention is "home edge regresses away's AD" and vice versa).
    reverse: dict = {}
    for from_node, neighbors in _REGULAR_GAME_EDGES.items():
        for idx, to_node in enumerate(neighbors):
            winner = "home" if idx == 0 else "away"
            reverse.setdefault(to_node, []).append((from_node, winner))

    def backtrack(node, partial):
        if node == start:
            yield list(reversed(partial))
            return
        node_d = dist.get(node)
        if node_d is None:
            return
        for pred, winner in reverse.get(node, []):
            if dist.get(pred) != node_d - 1:
                continue
            yield from backtrack(pred, partial + [(pred, node, winner)])

    yield from backtrack(end, [])


def find_legal_path(
    prev_pts: tuple[str, str],
    curr_pts: tuple[str, str],
    is_tiebreak: bool,
    preferred_last_side: Optional[str],
) -> list[tuple]:
    """Shortest path from ``prev_pts`` to ``curr_pts`` in the game graph.

    Each returned edge is ``(from_pts, to_pts, winner_side)`` where
    ``winner_side`` is ``'home'`` or ``'away'``.

    For regular games, walks ``_REGULAR_GAME_EDGES``. Enumerates all
    shortest paths and prefers the one whose last edge's winner matches
    ``preferred_last_side``; falls back to any shortest path otherwise
    (logging a debug-level warning).

    For tiebreaks, walks the integer lattice: from ``(h, a)`` legal
    successors are ``(h+1, a)`` (home) and ``(h, a+1)`` (away).

    Raises
    ------
    NoLegalPathError
        When ``curr_pts`` is unreachable from ``prev_pts``. The exception
        carries ``prev=None``, ``curr=None`` so the adapter caller can
        attach row context before re-raising.
    """
    if is_tiebreak:
        return _tiebreak_within_path(prev_pts, curr_pts, preferred_last_side)

    if prev_pts == curr_pts:
        return []

    paths = list(_enumerate_regular_paths(prev_pts, curr_pts))
    if not paths:
        raise NoLegalPathError(
            None, None,
            f"no legal regular-game path from {prev_pts} to {curr_pts}",
        )

    if preferred_last_side is None:
        return paths[0]

    for path in paths:
        if path and path[-1][2] == preferred_last_side:
            return path

    _log.debug(
        "find_legal_path: no shortest path from %s to %s ends with %s; "
        "falling back to %s",
        prev_pts, curr_pts, preferred_last_side, paths[0],
    )
    return paths[0]


def _tiebreak_within_path(
    prev_pts: tuple[str, str],
    curr_pts: tuple[str, str],
    preferred_last_side: Optional[str],
) -> list[tuple]:
    """Within-tiebreak path. Both sides may have advanced.

    Returns a deterministic ordering: when preferred_last_side is set
    and feasible, the non-preferred side's increments come first, then
    the preferred side's; otherwise home's increments come first.
    """
    try:
        ph = int(prev_pts[0])
        pa = int(prev_pts[1])
        ch = int(curr_pts[0])
        ca = int(curr_pts[1])
    except (TypeError, ValueError) as exc:
        raise NoLegalPathError(
            None, None,
            f"tiebreak point tokens not parseable: {prev_pts} -> {curr_pts}",
        ) from exc

    dh = ch - ph
    da = ca - pa
    if dh < 0 or da < 0:
        raise NoLegalPathError(
            None, None,
            f"tiebreak score regressed: {prev_pts} -> {curr_pts}",
        )
    if dh + da == 0:
        return []

    # Decide ordering: non-preferred first, preferred last.
    if preferred_last_side == "home" and dh >= 1:
        first_side, second_side = "away", "home"
        first_count, second_count = da, dh
    elif preferred_last_side == "away" and da >= 1:
        first_side, second_side = "home", "away"
        first_count, second_count = dh, da
    else:
        # No preference, or preference contradicts the deltas.
        if preferred_last_side is not None and (
            (preferred_last_side == "home" and dh == 0)
            or (preferred_last_side == "away" and da == 0)
        ):
            _log.debug(
                "_tiebreak_within_path: preferred=%s contradicts deltas "
                "(dh=%d, da=%d); falling back to home-first ordering",
                preferred_last_side, dh, da,
            )
        first_side, second_side = "home", "away"
        first_count, second_count = dh, da

    edges = []
    cur_h, cur_a = ph, pa
    for _ in range(first_count):
        from_pts = (str(cur_h), str(cur_a))
        if first_side == "home":
            cur_h += 1
        else:
            cur_a += 1
        to_pts = (str(cur_h), str(cur_a))
        edges.append((from_pts, to_pts, first_side))
    for _ in range(second_count):
        from_pts = (str(cur_h), str(cur_a))
        if second_side == "home":
            cur_h += 1
        else:
            cur_a += 1
        to_pts = (str(cur_h), str(cur_a))
        edges.append((from_pts, to_pts, second_side))

    return edges


def find_game_end_path(
    prev_pts: tuple[str, str],
    game_winner: str,
    is_tiebreak: bool,
) -> list[tuple]:
    """Shortest path from ``prev_pts`` to the game-end terminal.

    For regular games: BFS to ``GAME_A`` (home wins) or ``GAME_B``
    (away wins). Every shortest path's last edge is the game_winner
    side by construction of the graph, so any shortest path works.

    For tiebreaks: straight-line synthesis. Target is
    ``max(7, loser_pts + 2)``. Number of synthesized points equals
    the gap to that target, all credited to ``game_winner``. (We can
    only know the minimum unobserved tail; the actual sequence may
    have included loser wins, but the shortest path is straight-line.)

    Returns ``[]`` when prev is already at or beyond the game-end
    threshold (i.e. the engine close was missed in a prior transition
    and will be triggered by the next process_point call).

    Raises
    ------
    NoLegalPathError
        For regular games when ``prev_pts`` is not in the game graph.
        For tiebreaks when point tokens are not integer-parseable.
    """
    if is_tiebreak:
        return _tiebreak_game_end_path(prev_pts, game_winner)

    if game_winner == "home":
        target = GAME_A
    elif game_winner == "away":
        target = GAME_B
    else:
        raise NoLegalPathError(
            None, None,
            f"game_winner must be 'home' or 'away', got {game_winner!r}",
        )

    paths = list(_enumerate_regular_paths(prev_pts, target))
    if not paths:
        raise NoLegalPathError(
            None, None,
            f"no legal game-end path from {prev_pts} to {target}",
        )
    return paths[0]


def _tiebreak_game_end_path(
    prev_pts: tuple[str, str],
    game_winner: str,
) -> list[tuple]:
    try:
        ph = int(prev_pts[0])
        pa = int(prev_pts[1])
    except (TypeError, ValueError) as exc:
        raise NoLegalPathError(
            None, None,
            f"tiebreak prev point tokens not parseable: {prev_pts}",
        ) from exc

    if game_winner == "home":
        loser = pa
        winner_score = ph
    elif game_winner == "away":
        loser = ph
        winner_score = pa
    else:
        raise NoLegalPathError(
            None, None,
            f"game_winner must be 'home' or 'away', got {game_winner!r}",
        )

    target = max(7, loser + 2)
    delta = target - winner_score
    if delta <= 0:
        # Winner already at or past threshold — the game close was
        # missed on a prior transition. Engine close will fire on the
        # next process_point with a new (set, game).
        return []

    edges = []
    cur_h, cur_a = ph, pa
    for _ in range(delta):
        from_pts = (str(cur_h), str(cur_a))
        if game_winner == "home":
            cur_h += 1
        else:
            cur_a += 1
        to_pts = (str(cur_h), str(cur_a))
        edges.append((from_pts, to_pts, game_winner))
    return edges


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

# Map for "home" / "away" → Svr / PtWinner integer (1 = home, 2 = away).
_SIDE_TO_INT = {"home": 1, "away": 2}


def _winner_int(side: str) -> int:
    return _SIDE_TO_INT[side]


def _ambiguous_rank_delta(prev: dict, curr: dict, is_tb: bool) -> bool:
    """True when prev → curr's rank delta cannot pin down a single
    point_winner via the writer's _derive_point_winner logic.

    Used only when curr['point_winner'] is None — to decide whether to
    derive a winner ourselves or raise NoLegalPathError.
    """
    pph = normalize_point_token(prev.get("home_current_point"))
    ppa = normalize_point_token(prev.get("away_current_point"))
    cph = normalize_point_token(curr.get("home_current_point"))
    cpa = normalize_point_token(curr.get("away_current_point"))
    if is_tb:
        try:
            ph, pa = int(pph), int(ppa)
            ch, ca = int(cph), int(cpa)
        except (TypeError, ValueError):
            return True
        dh, da = ch - ph, ca - pa
        return dh > 0 and da > 0
    # Regular: ambiguous when both sides advanced (a compression that
    # the writer would still label, but if it's NULL we don't trust it).
    rank = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4}
    ph = rank.get(pph, -1)
    pa = rank.get(ppa, -1)
    ch = rank.get(cph, -1)
    ca = rank.get(cpa, -1)
    if ph < 0 or pa < 0 or ch < 0 or ca < 0:
        return True
    return (ch - ph) > 0 and (ca - pa) > 0


def _infer_winner_from_rank_delta(prev: dict, curr: dict, is_tb: bool) -> Optional[str]:
    """Mirror the writer's _derive_point_winner for unambiguous deltas."""
    pph = normalize_point_token(prev.get("home_current_point"))
    ppa = normalize_point_token(prev.get("away_current_point"))
    cph = normalize_point_token(curr.get("home_current_point"))
    cpa = normalize_point_token(curr.get("away_current_point"))
    if is_tb:
        try:
            ph, pa = int(pph), int(ppa)
            ch, ca = int(cph), int(cpa)
        except (TypeError, ValueError):
            return None
        if ch > ph and ca == pa:
            return "home"
        if ca > pa and ch == ph:
            return "away"
        return None
    rank = {"0": 0, "15": 1, "30": 2, "40": 3, "AD": 4}
    ph = rank.get(pph, -1)
    pa = rank.get(ppa, -1)
    ch = rank.get(cph, -1)
    ca = rank.get(cpa, -1)
    if ph < 0 or pa < 0 or ch < 0 or ca < 0:
        return None
    if ch > ph or ca < pa:
        return "home"
    if ca > pa or ch < ph:
        return "away"
    return None


def _virtual_prev_for_match_start() -> dict:
    """Construct a synthetic prev representing the match start.

    Used for NO_PREV_TRIVIAL when curr is non-zero — we recurse with
    this virtual prev so the rest of the WITHIN_GAME logic is reused.
    """
    return {
        "home_sets_won": 0,
        "away_sets_won": 0,
        "home_current_games": 0,
        "away_current_games": 0,
        "home_current_point": "0",
        "away_current_point": "0",
        "home_set1_games": 0,
        "away_set1_games": 0,
        "home_set2_games": 0,
        "away_set2_games": 0,
        "home_set3_games": 0,
        "away_set3_games": 0,
        "status": "inprogress",
        "point_winner": None,
        "first_server": None,
    }


class StateRowAdapter:
    """Convert ``live.match_states`` row pairs into Point sequences.

    Stateless across transitions — the only per-match state is
    ``first_server``, captured at construction.
    """

    def __init__(self, first_server: str) -> None:
        if first_server not in ("home", "away"):
            raise ValueError(
                f"first_server must be 'home' or 'away', got {first_server!r}"
            )
        self.first_server = first_server

    def transition(
        self,
        prev: Optional[dict],
        curr: dict,
    ) -> list[Point]:
        """Convert a (prev, curr) state-row pair to a list of Points.

        ``prev`` may be ``None`` (first row of a match).

        Returns the list of Points in emission order — possibly empty
        when the transition produces no new observable points (cold
        start with non-trivial curr, or a transition whose engine close
        is deferred to the next call).

        Raises :class:`NoLegalPathError` on any transition with no legal
        BFS path, NULL point_winner inside an ambiguous compression, or
        a malformed shape (games regression, multi-game jump, set
        regression, weird set-end).
        """
        kind = classify_transition(prev, curr)

        if kind == "NO_PREV_TRIVIAL":
            return self._emit_within_game(_virtual_prev_for_match_start(), curr)

        if kind == "NO_PREV_NONTRIVIAL":
            _log.debug(
                "StateRowAdapter: worker spawned mid-match, prev=None and "
                "curr has progress (sets=%s-%s games=%s-%s); emitting empty",
                curr.get("home_sets_won"), curr.get("away_sets_won"),
                curr.get("home_current_games"), curr.get("away_current_games"),
            )
            return []

        if kind == "NO_CHANGE":
            return []

        if kind == "WITHIN_GAME":
            return self._emit_within_game(prev, curr)

        if kind == "GAME_END":
            return self._emit_game_end(prev, curr)

        if kind == "SET_END" or kind == "MATCH_END":
            return self._emit_set_end(prev, curr)

        # MALFORMED
        raise NoLegalPathError(
            prev, curr,
            self._describe_malformed(prev, curr),
        )

    # ------------------------------------------------------------------
    # Per-kind emitters
    # ------------------------------------------------------------------

    def _emit_within_game(
        self,
        prev: dict,
        curr: dict,
    ) -> list[Point]:
        prev_pts = (
            normalize_point_token(prev.get("home_current_point")),
            normalize_point_token(prev.get("away_current_point")),
        )
        curr_pts = (
            normalize_point_token(curr.get("home_current_point")),
            normalize_point_token(curr.get("away_current_point")),
        )

        if prev_pts == curr_pts:
            return []

        # Tiebreak iff either side of the transition is in tiebreak vocab.
        is_tb = is_tiebreak_score(prev) or is_tiebreak_score(curr)

        # Determine winner side.
        pw = curr.get("point_winner")
        if pw in ("home", "away"):
            preferred = pw
        else:
            # Writer left NULL: derive from rank delta if unambiguous,
            # else raise.
            if _ambiguous_rank_delta(prev, curr, is_tb):
                raise NoLegalPathError(
                    prev, curr,
                    "NULL point_winner with ambiguous rank delta "
                    f"({prev_pts} -> {curr_pts}, is_tiebreak={is_tb})",
                )
            preferred = _infer_winner_from_rank_delta(prev, curr, is_tb)
            if preferred is None:
                raise NoLegalPathError(
                    prev, curr,
                    "NULL point_winner; rank delta did not yield a "
                    f"winner ({prev_pts} -> {curr_pts})",
                )

        try:
            edges = find_legal_path(prev_pts, curr_pts, is_tb, preferred)
        except NoLegalPathError as exc:
            exc.prev = prev
            exc.curr = curr
            raise

        if not edges:
            return []

        set_number, game_number = compute_set_and_game(prev)
        completed = count_completed_games_in_match(prev)
        svr = compute_server_for_game(self.first_server, completed)

        return [
            Point(
                set_number=set_number,
                game_number_in_set=game_number,
                score_before=format_score_before(edge[0][0], edge[0][1]),
                Svr=svr,
                PtWinner=_winner_int(edge[2]),
                is_tiebreak=is_tb,
            )
            for edge in edges
        ]

    def _emit_game_end(
        self,
        prev: dict,
        curr: dict,
    ) -> list[Point]:
        # Detect which side's games incremented.
        pga = prev.get("home_current_games") or 0
        pgb = prev.get("away_current_games") or 0
        cga = curr.get("home_current_games") or 0
        cgb = curr.get("away_current_games") or 0
        if cga > pga:
            game_winner = "home"
        elif cgb > pgb:
            game_winner = "away"
        else:
            raise NoLegalPathError(
                prev, curr,
                "GAME_END classified but neither side's games incremented",
            )

        return self._synth_closing_game(prev, curr, game_winner)

    def _emit_set_end(
        self,
        prev: dict,
        curr: dict,
    ) -> list[Point]:
        psa = prev.get("home_sets_won") or 0
        psb = prev.get("away_sets_won") or 0
        csa = curr.get("home_sets_won") or 0
        csb = curr.get("away_sets_won") or 0
        if csa > psa:
            set_winner = "home"
        elif csb > psb:
            set_winner = "away"
        else:
            raise NoLegalPathError(
                prev, curr,
                "SET_END classified but neither side's sets incremented",
            )

        # The closing game's winner is the set winner (a set ends only
        # on a game won).
        return self._synth_closing_game(prev, curr, set_winner)

    def _synth_closing_game(
        self,
        prev: dict,
        curr: dict,
        game_winner: str,
    ) -> list[Point]:
        prev_pts = (
            normalize_point_token(prev.get("home_current_point")),
            normalize_point_token(prev.get("away_current_point")),
        )
        is_tb = is_tiebreak_score(prev)

        try:
            edges = find_game_end_path(prev_pts, game_winner, is_tb)
        except NoLegalPathError as exc:
            exc.prev = prev
            exc.curr = curr
            raise

        if not edges:
            return []

        set_number, game_number = compute_set_and_game(prev)
        completed = count_completed_games_in_match(prev)
        svr = compute_server_for_game(self.first_server, completed)

        return [
            Point(
                set_number=set_number,
                game_number_in_set=game_number,
                score_before=format_score_before(edge[0][0], edge[0][1]),
                Svr=svr,
                PtWinner=_winner_int(edge[2]),
                is_tiebreak=is_tb,
            )
            for edge in edges
        ]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_malformed(prev: Optional[dict], curr: dict) -> str:
        psa = prev.get("home_sets_won") or 0
        psb = prev.get("away_sets_won") or 0
        pga = prev.get("home_current_games") or 0
        pgb = prev.get("away_current_games") or 0
        csa = curr.get("home_sets_won") or 0
        csb = curr.get("away_sets_won") or 0
        cga = curr.get("home_current_games") or 0
        cgb = curr.get("away_current_games") or 0
        return (
            f"MALFORMED_TRANSITION: prev sets={psa}-{psb} games={pga}-{pgb} "
            f"-> curr sets={csa}-{csb} games={cga}-{cgb}"
        )
