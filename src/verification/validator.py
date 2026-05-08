"""
Sequence validator for the live tennis tracker.

Pure functions only — no DB access, no environment reads, no I/O. Inputs
arrive as Python data, outputs leave as Python data. The orchestrator (a
later phase) is responsible for fetching match_states / match_polls rows
out of the database and for writing the validator's results back into
audit.verification_reports / audit.live_gap_reports.

Source schema (from `\\d live.match_states`):

    match_id              integer
    polled_at             timestamptz
    status                varchar
    home_sets_won         integer
    away_sets_won         integer
    home_set1_games       integer    (and away_set1_games, set2, set3)
    home_current_games    integer
    away_current_games    integer
    home_current_point    varchar    "0" | "15" | "30" | "40" | "A"
                                     or integer-as-string in tiebreaks
    away_current_point    varchar
    point_winner, winner_code, ...

`home` maps to player A, `away` maps to player B. The point columns use
"A" (single char) for advantage; parse_state_row normalizes that to "AD"
so the rest of the module uses canonical tennis vocabulary.

Recorded final score (from live.match_polls) is supplied to validate_match
as a pre-formatted string, e.g. "6-4 7-6" or "6-4 6-7 6-3" — the
orchestrator handles the formatting before calling in.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateRow:
    """Normalized representation of one live.match_states row."""
    sets_a: int
    sets_b: int
    games_a: int
    games_b: int
    score_a: str  # "0" | "15" | "30" | "40" | "AD" or integer-as-string in tiebreaks
    score_b: str
    polled_at: Optional[object] = None  # timestamp, kept for ordering only


@dataclass(frozen=True)
class Gap:
    gap_type: str       # see _GAP_TYPES below
    severity: str       # "low" | "medium" | "high"
    description: str
    before_state: dict
    after_state: dict
    inferred_skipped_points: Optional[int]
    set_number: Optional[int]
    game_number: Optional[int]


@dataclass
class ValidationSummary:
    live_point_count: int
    inferred_missing_points: int
    live_final_score: str
    recorded_final_score: str
    final_score_match: bool
    total_sets: int
    clean_set_count: int
    gapped_set_count: int
    gap_count: int
    severity_max: Optional[str]
    verdict: str
    set_breakdown: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regular-game state graph
#
# Nodes are (score_a, score_b) tuples plus the two terminal markers
# "GAME_A" and "GAME_B". Edges encode every legal point outcome from each
# state. Deuce oscillation (40-40 ↔ AD-X) is intentionally bidirectional:
# from AD-40 you can either close out the game (GAME_A) OR the opponent
# wins the next point and you slide back to 40-40 — that's not a
# regression.
# ---------------------------------------------------------------------------

GAME_A = "GAME_A"
GAME_B = "GAME_B"

_REGULAR_GAME_EDGES: dict = {
    ("0",  "0"):  [("15", "0"), ("0",  "15")],
    ("15", "0"):  [("30", "0"), ("15", "15")],
    ("0",  "15"): [("15", "15"), ("0",  "30")],
    ("30", "0"):  [("40", "0"), ("30", "15")],
    ("15", "15"): [("30", "15"), ("15", "30")],
    ("0",  "30"): [("15", "30"), ("0",  "40")],
    ("40", "0"):  [GAME_A,       ("40", "15")],
    ("30", "15"): [("40", "15"), ("30", "30")],
    ("15", "30"): [("30", "30"), ("15", "40")],
    ("0",  "40"): [("15", "40"), GAME_B],
    ("40", "15"): [GAME_A,       ("40", "30")],
    ("30", "30"): [("40", "30"), ("30", "40")],
    ("15", "40"): [("30", "40"), GAME_B],
    ("40", "30"): [GAME_A,       ("40", "40")],
    ("30", "40"): [("40", "40"), GAME_B],
    ("40", "40"): [("AD", "40"), ("40", "AD")],
    ("AD", "40"): [GAME_A,       ("40", "40")],
    ("40", "AD"): [("40", "40"), GAME_B],
}

_REGULAR_VOCAB = {"0", "15", "30", "40", "AD"}


def _bfs_path_length(edges: dict, start, end) -> Optional[int]:
    """Return number of edges in the shortest path from start to end, or None."""
    if start == end:
        return 0
    if start not in edges and start != end:
        # start is an unknown / terminal node — no outgoing edges
        return None
    visited = {start}
    queue = deque([(start, 0)])
    while queue:
        node, dist = queue.popleft()
        for neighbor in edges.get(node, []):
            if neighbor in visited:
                continue
            if neighbor == end:
                return dist + 1
            visited.add(neighbor)
            queue.append((neighbor, dist + 1))
    return None


def _looks_tiebreak(score_a: str, score_b: str) -> bool:
    """
    Strict tiebreak detector: at least one side carries an integer-string
    score that's NOT in the regular game vocabulary {"0","15","30","40","AD"}.
    A pair of "0"/"0" is treated as ambiguous — the caller decides via the
    games count.
    """
    non_regular = {score_a, score_b} - _REGULAR_VOCAB
    if not non_regular:
        return False
    for v in non_regular:
        try:
            int(v)
        except (ValueError, TypeError):
            return False
    return True


def _was_in_tiebreak(state: StateRow) -> bool:
    """True if `state` is plausibly inside a tiebreak game."""
    if state.games_a != 6 or state.games_b != 6:
        return False
    return _looks_tiebreak(state.score_a, state.score_b)


def _is_tiebreak_transition(walk: StateRow, nxt: StateRow) -> bool:
    """
    A transition is analyzed under tiebreak rules iff both walk and nxt sit
    at games 6-6 AND at least one of them shows unambiguous tiebreak
    digits. Otherwise we treat the transition as part of a regular game
    (which covers advantage-set scoring as well, since that uses regular
    point values past 6-6).
    """
    if (walk.games_a, walk.games_b) != (6, 6):
        return False
    if (nxt.games_a, nxt.games_b) != (6, 6):
        return False
    return _looks_tiebreak(walk.score_a, walk.score_b) or _looks_tiebreak(
        nxt.score_a, nxt.score_b
    )


# ---------------------------------------------------------------------------
# Severity / verdict helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _score_jump_severity(missing_points: int) -> str:
    if missing_points <= 2:
        return "low"
    if missing_points <= 5:
        return "medium"
    return "high"


def _max_severity(severities: list[str]) -> Optional[str]:
    if not severities:
        return None
    return max(severities, key=lambda s: _SEVERITY_RANK[s])


def _verdict(severities: list[str]) -> str:
    if not severities:
        return "clean"
    top = _max_severity(severities)
    if top == "high":
        return "major_gaps"
    if top == "medium":
        return "material_gaps"
    return "minor_gaps"


# ---------------------------------------------------------------------------
# parse_state_row
# ---------------------------------------------------------------------------

def _normalize_point(value) -> str:
    if value is None or value == "":
        return "0"
    s = str(value).strip().upper()
    if s in {"A", "AD", "ADV"}:
        return "AD"
    return s


def parse_state_row(raw_row: dict) -> StateRow:
    """
    Convert a live.match_states row dict into a StateRow.

    Maps home → player A, away → player B and normalizes the live
    tracker's "A" advantage indicator to "AD".
    """
    sets_a = int(raw_row.get("home_sets_won") or 0)
    sets_b = int(raw_row.get("away_sets_won") or 0)
    games_a = raw_row.get("home_current_games")
    games_b = raw_row.get("away_current_games")
    if games_a is None or games_b is None:
        # Fall back to the per-set games columns for the in-progress set.
        idx = sets_a + sets_b + 1
        games_a = raw_row.get(f"home_set{idx}_games") or 0
        games_b = raw_row.get(f"away_set{idx}_games") or 0
    score_a = _normalize_point(raw_row.get("home_current_point"))
    score_b = _normalize_point(raw_row.get("away_current_point"))
    return StateRow(
        sets_a=sets_a,
        sets_b=sets_b,
        games_a=int(games_a),
        games_b=int(games_b),
        score_a=score_a,
        score_b=score_b,
        polled_at=raw_row.get("polled_at"),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _state_dict(s: StateRow) -> dict:
    return {
        "sets_a": s.sets_a, "sets_b": s.sets_b,
        "games_a": s.games_a, "games_b": s.games_b,
        "score_a": s.score_a, "score_b": s.score_b,
    }


def _is_valid_set_end(games_a: int, games_b: int, winner: str) -> bool:
    """
    True if (games_a, games_b) is a legal set-ending score for `winner`
    (either at "A" or "B"). Covers regular sets (6-X with 2-game lead),
    7-5, tiebreak 7-6, and arbitrarily-long advantage sets.
    """
    if winner == "A":
        if games_a >= 6 and games_a >= games_b + 2:
            return True
        if games_a == 7 and games_b == 6:
            return True
        return False
    if games_b >= 6 and games_b >= games_a + 2:
        return True
    if games_b == 7 and games_a == 6:
        return True
    return False


def _check_game_end_score(walk: StateRow, winner: str, current_set: int) -> Optional[Gap]:
    """
    A game just ended in favour of `winner`. If the walk's last observed
    in-game score wasn't already at a one-point-from-winning position,
    emit a score_jump representing the unobserved tail of the game.

    Returns None if the walk was already a single point from victory
    (path length 1 in the regular-game graph, or already at the tiebreak
    win threshold).
    """
    game_number = walk.games_a + walk.games_b + 1

    if _was_in_tiebreak(walk):
        try:
            a = int(walk.score_a)
            b = int(walk.score_b)
        except (ValueError, TypeError):
            return None
        # `target` = walker's score required just before the winning point so
        # that one more point puts winner at >=7 with a 2-point lead.
        if winner == "A":
            target = max(6, b + 1)
            inferred = max(target - a, 0)
        else:
            target = max(6, a + 1)
            inferred = max(target - b, 0)
        if inferred == 0:
            return None
        return Gap(
            gap_type="score_jump",
            severity=_score_jump_severity(inferred),
            description=(
                f"Tiebreak game ended without observing the closing points "
                f"(walk at {walk.score_a}-{walk.score_b}, won by {winner})"
            ),
            before_state=_state_dict(walk),
            after_state={"game_won_by": winner},
            inferred_skipped_points=inferred,
            set_number=current_set,
            game_number=game_number,
        )

    # Regular game.
    target = GAME_A if winner == "A" else GAME_B
    walk_score = (walk.score_a, walk.score_b)
    path = _bfs_path_length(_REGULAR_GAME_EDGES, walk_score, target)
    if path is None or path <= 1:
        return None
    inferred = path - 1
    return Gap(
        gap_type="score_jump",
        severity=_score_jump_severity(inferred),
        description=(
            f"Game ended without observing the closing points "
            f"(walk at {walk.score_a}-{walk.score_b}, won by {winner})"
        ),
        before_state=_state_dict(walk),
        after_state={"game_won_by": winner},
        inferred_skipped_points=inferred,
        set_number=current_set,
        game_number=game_number,
    )


# ---------------------------------------------------------------------------
# validate_match
# ---------------------------------------------------------------------------

def validate_match(
    state_rows: list[StateRow],
    recorded_final_score: str,
    match_id: str,
) -> tuple[list[Gap], ValidationSummary]:
    """
    Walk `state_rows` in order. Validate every transition against the
    legal-tennis-scoring graph. Produce a list of Gap objects and a
    ValidationSummary.

    Re-anchoring: when a transition is illegal, we still advance the walk
    state to the new row. This keeps one bad row from cascading into
    every subsequent comparison.

    `match_id` is accepted for API symmetry with the orchestrator and is
    not used by the pure validator beyond being available for callers
    that wish to embed it in descriptions; we leave it out of the
    canonical descriptions to keep them locality-independent.
    """
    _ = match_id  # reserved; intentionally unused by the pure validator

    gaps: list[Gap] = []
    set_breakdown_map: dict[int, dict] = {}
    finished_sets: list[tuple[int, int]] = []

    def ensure_set(sn: int) -> dict:
        if sn not in set_breakdown_map:
            set_breakdown_map[sn] = {
                "set_num": sn,
                "live_pts": 0,
                "gap_count": 0,
                "clean": True,
            }
        return set_breakdown_map[sn]

    def emit_gap(g: Gap) -> None:
        gaps.append(g)
        sn = g.set_number if g.set_number is not None else current_set
        bucket = ensure_set(sn)
        bucket["gap_count"] += 1
        bucket["clean"] = False

    if not state_rows:
        summary = ValidationSummary(
            live_point_count=0,
            inferred_missing_points=0,
            live_final_score="",
            recorded_final_score=recorded_final_score,
            final_score_match=(recorded_final_score == ""),
            total_sets=0,
            clean_set_count=0,
            gapped_set_count=0,
            gap_count=0,
            severity_max=None,
            verdict="clean",
            set_breakdown=[],
        )
        return [], summary

    walk = state_rows[0]
    current_set = walk.sets_a + walk.sets_b + 1
    ensure_set(current_set)

    for nxt in state_rows[1:]:
        # Snapshot the set this transition belongs to (the walk's set,
        # before any advancement happens this iteration).
        transition_set = current_set
        ensure_set(transition_set)["live_pts"] += 1

        game_number = walk.games_a + walk.games_b + 1
        before = _state_dict(walk)
        after = _state_dict(nxt)

        # ----- 1. Duplicate -----------------------------------------------
        if (
            walk.sets_a == nxt.sets_a and walk.sets_b == nxt.sets_b
            and walk.games_a == nxt.games_a and walk.games_b == nxt.games_b
            and walk.score_a == nxt.score_a and walk.score_b == nxt.score_b
        ):
            emit_gap(Gap(
                gap_type="duplicate_state",
                severity="low",
                description=(
                    f"Duplicate state at sets {walk.sets_a}-{walk.sets_b} "
                    f"games {walk.games_a}-{walk.games_b} "
                    f"score {walk.score_a}-{walk.score_b}"
                ),
                before_state=before,
                after_state=after,
                inferred_skipped_points=None,
                set_number=transition_set,
                game_number=game_number,
            ))
            walk = nxt
            continue

        # ----- 2. Set transition ------------------------------------------
        sets_total_walk = walk.sets_a + walk.sets_b
        sets_total_nxt = nxt.sets_a + nxt.sets_b
        if sets_total_nxt > sets_total_walk:
            sets_diff = sets_total_nxt - sets_total_walk
            if nxt.sets_a > walk.sets_a and nxt.sets_b == walk.sets_b:
                winner = "A"
            elif nxt.sets_b > walk.sets_b and nxt.sets_a == walk.sets_a:
                winner = "B"
            else:
                winner = None

            valid_jump = (
                sets_diff == 1
                and winner is not None
                and nxt.games_a == 0 and nxt.games_b == 0
                and nxt.score_a == "0" and nxt.score_b == "0"
            )
            if valid_jump:
                if winner == "A":
                    implied_games = (walk.games_a + 1, walk.games_b)
                else:
                    implied_games = (walk.games_a, walk.games_b + 1)
                if not _is_valid_set_end(implied_games[0], implied_games[1], winner):
                    emit_gap(Gap(
                        gap_type="set_jump",
                        severity="high",
                        description=(
                            f"Set ended with invalid games count "
                            f"{implied_games[0]}-{implied_games[1]} "
                            f"(winner={winner})"
                        ),
                        before_state=before,
                        after_state=after,
                        inferred_skipped_points=None,
                        set_number=transition_set,
                        game_number=game_number,
                    ))
                    finished_sets.append((walk.games_a, walk.games_b))
                else:
                    inner = _check_game_end_score(walk, winner, transition_set)
                    if inner is not None:
                        emit_gap(inner)
                    finished_sets.append(implied_games)
            else:
                emit_gap(Gap(
                    gap_type="set_jump",
                    severity="high",
                    description=(
                        f"Sets jumped from {walk.sets_a}-{walk.sets_b} to "
                        f"{nxt.sets_a}-{nxt.sets_b} without a clean set transition"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=None,
                    set_number=transition_set,
                    game_number=game_number,
                ))
                finished_sets.append((walk.games_a, walk.games_b))

            current_set = nxt.sets_a + nxt.sets_b + 1
            walk = nxt
            continue

        if sets_total_nxt < sets_total_walk:
            # Sets count went backwards — emit set_jump as a regression.
            emit_gap(Gap(
                gap_type="set_jump",
                severity="high",
                description=(
                    f"Sets regressed from {walk.sets_a}-{walk.sets_b} to "
                    f"{nxt.sets_a}-{nxt.sets_b}"
                ),
                before_state=before,
                after_state=after,
                inferred_skipped_points=None,
                set_number=transition_set,
                game_number=game_number,
            ))
            current_set = nxt.sets_a + nxt.sets_b + 1
            walk = nxt
            continue

        # ----- 3. Same set, game change -----------------------------------
        games_total_walk = walk.games_a + walk.games_b
        games_total_nxt = nxt.games_a + nxt.games_b
        if games_total_nxt != games_total_walk:
            if games_total_nxt < games_total_walk:
                emit_gap(Gap(
                    gap_type="score_regression",
                    severity="medium",
                    description=(
                        f"Games regressed from {walk.games_a}-{walk.games_b} "
                        f"to {nxt.games_a}-{nxt.games_b} within set {transition_set}"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=None,
                    set_number=transition_set,
                    game_number=game_number,
                ))
                walk = nxt
                continue

            games_diff = games_total_nxt - games_total_walk
            if nxt.games_a > walk.games_a and nxt.games_b == walk.games_b:
                winner = "A"
                expected = (walk.games_a + 1, walk.games_b)
            elif nxt.games_b > walk.games_b and nxt.games_a == walk.games_a:
                winner = "B"
                expected = (walk.games_a, walk.games_b + 1)
            else:
                winner = None
                expected = None

            clean_increment = (
                games_diff == 1
                and winner is not None
                and (nxt.games_a, nxt.games_b) == expected
                and nxt.score_a == "0" and nxt.score_b == "0"
            )
            if not clean_increment:
                emit_gap(Gap(
                    gap_type="game_jump",
                    severity="medium",
                    description=(
                        f"Games jumped from {walk.games_a}-{walk.games_b} "
                        f"to {nxt.games_a}-{nxt.games_b}"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=None,
                    set_number=transition_set,
                    game_number=game_number,
                ))
            else:
                inner = _check_game_end_score(walk, winner, transition_set)
                if inner is not None:
                    emit_gap(inner)

            walk = nxt
            continue

        # ----- 4. Same set, same game, score change -----------------------
        if _is_tiebreak_transition(walk, nxt):
            try:
                a_walk = int(walk.score_a)
                b_walk = int(walk.score_b)
                a_nxt = int(nxt.score_a)
                b_nxt = int(nxt.score_b)
            except (ValueError, TypeError):
                emit_gap(Gap(
                    gap_type="score_regression",
                    severity="medium",
                    description=(
                        f"Tiebreak score not parseable: "
                        f"{walk.score_a}-{walk.score_b} -> "
                        f"{nxt.score_a}-{nxt.score_b}"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=None,
                    set_number=transition_set,
                    game_number=game_number,
                ))
                walk = nxt
                continue

            if a_nxt < a_walk or b_nxt < b_walk:
                emit_gap(Gap(
                    gap_type="score_regression",
                    severity="medium",
                    description=(
                        f"Tiebreak score regressed from "
                        f"{walk.score_a}-{walk.score_b} to "
                        f"{nxt.score_a}-{nxt.score_b}"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=None,
                    set_number=transition_set,
                    game_number=game_number,
                ))
                walk = nxt
                continue

            diff = (a_nxt - a_walk) + (b_nxt - b_walk)
            if diff == 0:
                # Caught earlier as duplicate, but defensive.
                walk = nxt
                continue
            if diff > 1:
                inferred = diff - 1
                emit_gap(Gap(
                    gap_type="score_jump",
                    severity=_score_jump_severity(inferred),
                    description=(
                        f"Tiebreak jumped from {walk.score_a}-{walk.score_b} "
                        f"to {nxt.score_a}-{nxt.score_b}"
                    ),
                    before_state=before,
                    after_state=after,
                    inferred_skipped_points=inferred,
                    set_number=transition_set,
                    game_number=game_number,
                ))
            walk = nxt
            continue

        # Regular within-game transition: BFS on the regular-game graph.
        walk_score = (walk.score_a, walk.score_b)
        nxt_score = (nxt.score_a, nxt.score_b)
        path = _bfs_path_length(_REGULAR_GAME_EDGES, walk_score, nxt_score)
        if path is None:
            emit_gap(Gap(
                gap_type="score_regression",
                severity="medium",
                description=(
                    f"Score regressed (no legal forward path) from "
                    f"{walk.score_a}-{walk.score_b} to "
                    f"{nxt.score_a}-{nxt.score_b}"
                ),
                before_state=before,
                after_state=after,
                inferred_skipped_points=None,
                set_number=transition_set,
                game_number=game_number,
            ))
        elif path > 1:
            inferred = path - 1
            emit_gap(Gap(
                gap_type="score_jump",
                severity=_score_jump_severity(inferred),
                description=(
                    f"Score jumped from {walk.score_a}-{walk.score_b} "
                    f"to {nxt.score_a}-{nxt.score_b}"
                ),
                before_state=before,
                after_state=after,
                inferred_skipped_points=inferred,
                set_number=transition_set,
                game_number=game_number,
            ))
        walk = nxt

    # -----------------------------------------------------------------
    # Build final-score and summary
    # -----------------------------------------------------------------
    live_final_score = " ".join(f"{a}-{b}" for a, b in finished_sets)
    final_score_match = live_final_score == recorded_final_score

    if not final_score_match:
        gaps.append(Gap(
            gap_type="final_state_mismatch",
            severity="high",
            description=(
                f"Live walk produced final score '{live_final_score}' but "
                f"recorded final score is '{recorded_final_score}'"
            ),
            before_state={"live_final_score": live_final_score},
            after_state={"recorded_final_score": recorded_final_score},
            inferred_skipped_points=None,
            set_number=None,
            game_number=None,
        ))

    set_breakdown = [
        set_breakdown_map[k] for k in sorted(set_breakdown_map.keys())
    ]
    severities = [g.severity for g in gaps]
    inferred_total = sum(
        g.inferred_skipped_points or 0 for g in gaps
        if g.inferred_skipped_points is not None
    )

    summary = ValidationSummary(
        live_point_count=len(state_rows),
        inferred_missing_points=inferred_total,
        live_final_score=live_final_score,
        recorded_final_score=recorded_final_score,
        final_score_match=final_score_match,
        total_sets=len(set_breakdown),
        clean_set_count=sum(1 for s in set_breakdown if s["clean"]),
        gapped_set_count=sum(1 for s in set_breakdown if not s["clean"]),
        gap_count=len(gaps),
        severity_max=_max_severity(severities),
        verdict=_verdict(severities),
        set_breakdown=set_breakdown,
    )
    return gaps, summary
