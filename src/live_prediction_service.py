"""Live prediction service for set-win probability.

``LivePredictionService`` is the runtime wrapper around the v1 trained
model.  It drives a :class:`StreamingMatchState` for the 52 signal
sub-components, tracks match-level score state under the asymmetric
convention used at training time (games_A/B post-game, sets_won_A/B
pre-set — see ``docs/live_prediction_service_recon.md`` §7), computes
the Markov baseline per emitted game with the same 5-arg signature
that wrote ``markov_set_win_prob_A`` into the training table (recon
§4b), and returns calibrated :class:`Prediction` objects via
``model.predict_proba``.

Predictions begin **after the first game of the match closes** — the
streaming signal engine emits no row before then (recon §10a), so the
service returns ``None`` from :meth:`LivePredictionService.process_point`
for every point of game 1.

Authoritative reference: ``docs/live_prediction_service_recon.md``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb
import joblib
import numpy as np
from rapidfuzz import fuzz, process

from src.markov_engine import clear_set_cache, set_win_prob
from src.signal_engine import SIGNAL_COLUMNS
from src.streaming_signal_engine import StreamingMatchState


_SURFACE_CATEGORIES = ("hard", "clay", "grass", "unknown")


@dataclass(frozen=True)
class Prediction:
    """One emitted prediction — produced at every game boundary and at finalize."""
    match_id_int: int
    set_number: int
    game_number_in_set: int
    probability_a: float
    confidence: float
    features: dict[str, float]


class _ScoreState:
    """Match-level score tracker matching training's asymmetric convention.

    ``games_A``/``games_B`` reflect the score *after* the row's just-closed
    game; ``sets_won_A``/``sets_won_B`` reflect sets won *before* the
    current set started, even on the set-clinching row.
    """

    def __init__(self) -> None:
        self.set_number: int = 1
        self.games_a: int = 0
        self.games_b: int = 0
        self.sets_won_a: int = 0
        self.sets_won_b: int = 0
        self.prev_set: int | None = None
        self.prev_game: int | None = None
        self.current_game_server: int | None = None
        self.current_game_pts_a: int = 0
        self.current_game_pts_b: int = 0
        self.finalized: bool = False

    def observe_point(self, pt) -> dict | None:
        """Feed one point; return the just-closed game's snapshot, or ``None``."""
        cur_set = int(pt.set_number)
        cur_game = int(pt.game_number_in_set)
        snapshot: dict | None = None

        if self.prev_set is not None and (cur_set, cur_game) != (
            self.prev_set, self.prev_game
        ):
            if self.current_game_pts_a > self.current_game_pts_b:
                self.games_a += 1
            else:
                self.games_b += 1

            snapshot = {
                "set_number": self.prev_set,
                "game_number_in_set": self.prev_game,
                "games_A": self.games_a,
                "games_B": self.games_b,
                "sets_won_A": self.sets_won_a,
                "sets_won_B": self.sets_won_b,
                "server_was_a": (self.current_game_server == 1),
            }

            if cur_set != self.prev_set:
                if self.games_a > self.games_b:
                    self.sets_won_a += 1
                else:
                    self.sets_won_b += 1
                self.games_a = 0
                self.games_b = 0
                self.set_number = cur_set

            self.current_game_pts_a = 0
            self.current_game_pts_b = 0
            self.current_game_server = int(pt.Svr)
        elif self.current_game_server is None:
            self.current_game_server = int(pt.Svr)

        self.prev_set, self.prev_game = cur_set, cur_game
        if int(pt.PtWinner) == 1:
            self.current_game_pts_a += 1
        else:
            self.current_game_pts_b += 1

        return snapshot

    def finalize(self) -> dict | None:
        """Close the final in-progress game and return its snapshot; idempotent."""
        if self.finalized:
            return None
        self.finalized = True
        if self.prev_set is None:
            return None

        if self.current_game_pts_a > self.current_game_pts_b:
            self.games_a += 1
        else:
            self.games_b += 1

        return {
            "set_number": self.prev_set,
            "game_number_in_set": self.prev_game,
            "games_A": self.games_a,
            "games_B": self.games_b,
            "sets_won_A": self.sets_won_a,
            "sets_won_B": self.sets_won_b,
            "server_was_a": (self.current_game_server == 1),
        }


class LivePredictionService:
    """Drives streaming signals + score state + Markov + calibrated model.

    Predictions begin only after the first game of a match closes — the
    streaming signal engine emits no row before then, so the service
    returns ``None`` for every point of game 1.
    """

    def __init__(
        self,
        model_path: str = "data/processed/model_v1.joblib",
        metadata_path: str = "data/processed/model_v1_metadata.json",
        p0_lookup: dict[str, float] | None = None,
        db_path: str = "data/processed/tennis.duckdb",
        league_avg_p0: float = 0.6266,
    ) -> None:
        self.model = joblib.load(model_path)
        if type(self.model).__name__ != "CalibratedClassifierCV":
            raise RuntimeError(
                f"expected CalibratedClassifierCV, got {type(self.model).__name__}"
            )
        if getattr(self.model, "n_features_in_", None) != 63:
            raise RuntimeError(
                f"expected n_features_in_=63, got {getattr(self.model, 'n_features_in_', None)}"
            )
        if tuple(self.model.classes_) != (0, 1):
            raise RuntimeError(
                f"expected classes_=(0, 1), got {tuple(self.model.classes_)}"
            )

        with open(metadata_path) as f:
            meta = json.load(f)
        self.feature_names: list[str] = list(meta["feature_names"])
        if len(self.feature_names) != 63:
            raise RuntimeError(
                f"expected 63 feature_names in metadata, got {len(self.feature_names)}"
            )

        if p0_lookup is None:
            con = duckdb.connect(db_path, read_only=True)
            try:
                rows = con.execute(
                    "SELECT player_name, p0 FROM core.player_p0"
                ).fetchall()
            finally:
                con.close()
            self.p0_lookup: dict[str, float] = {
                name: float(p0) for name, p0 in rows
            }
        else:
            self.p0_lookup = dict(p0_lookup)

        self.league_avg_p0: float = float(league_avg_p0)

        self.match_id_int: int | None = None
        self.match_state: StreamingMatchState | None = None
        self.score_state: _ScoreState | None = None
        self.p0_a: float | None = None
        self.p0_b: float | None = None
        self.surface_dummies: dict[str, int] | None = None
        self.first_server_is_a: bool | None = None
        self.finalized: bool = False
        self._last_emitted_server_was_a: bool | None = None

    def start_match(
        self,
        match_id_int: int,
        player_a: str,
        player_b: str,
        surface: str,
        first_server_is_a: bool,
        p0_a: float | None = None,
        p0_b: float | None = None,
    ) -> None:
        """Reset per-match state, resolve p0 for both players, and bucket the surface."""
        self.match_id_int = match_id_int
        self.match_state = StreamingMatchState(match_id_int)
        self.score_state = _ScoreState()
        self.finalized = False
        self._last_emitted_server_was_a = None

        self.p0_a = float(p0_a) if p0_a is not None else self._resolve_p0(player_a)
        self.p0_b = float(p0_b) if p0_b is not None else self._resolve_p0(player_b)

        surface_norm = (surface or "unknown").lower()
        if surface_norm not in _SURFACE_CATEGORIES:
            surface_norm = "unknown"
        self.surface_dummies = {
            f"surface_{cat}": 1 if surface_norm == cat else 0
            for cat in _SURFACE_CATEGORIES
        }

        self.first_server_is_a = bool(first_server_is_a)

        clear_set_cache()

    def process_point(self, pt) -> Prediction | None:
        """Feed one point; return a Prediction iff a game just closed."""
        if self.finalized:
            raise RuntimeError("Cannot process_point after finalize")
        if self.match_state is None or self.score_state is None:
            raise RuntimeError("start_match must be called before process_point")

        snapshot = self.score_state.observe_point(pt)
        signal_dict = self.match_state.process_point(pt)

        if bool(snapshot) != bool(signal_dict):
            emitter = (
                "score_state" if snapshot is not None else "match_state"
            )
            raise RuntimeError(
                f"score-state / signal-engine emission disagreement at "
                f"set {int(pt.set_number)} game {int(pt.game_number_in_set)}: "
                f"only {emitter} emitted"
            )

        if signal_dict is None:
            return None
        return self._make_prediction(snapshot, signal_dict)

    def finalize(self) -> Prediction | None:
        """Flush the final in-progress game; idempotent after the first call."""
        if self.finalized:
            return None
        self.finalized = True

        signal_dict = (
            self.match_state.finalize() if self.match_state is not None else None
        )
        snapshot = (
            self.score_state.finalize() if self.score_state is not None else None
        )

        if bool(snapshot) != bool(signal_dict):
            emitter = (
                "score_state" if snapshot is not None else "match_state"
            )
            raise RuntimeError(
                f"score-state / signal-engine finalize disagreement: "
                f"only {emitter} emitted"
            )

        if signal_dict is None:
            return None
        return self._make_prediction(snapshot, signal_dict)

    def _resolve_p0(self, player_name: str) -> float:
        if player_name in self.p0_lookup:
            return self.p0_lookup[player_name]
        candidates = list(self.p0_lookup.keys())
        if candidates:
            hit = process.extractOne(
                player_name, candidates, scorer=fuzz.ratio, score_cutoff=90
            )
            if hit is not None:
                return self.p0_lookup[hit[0]]
        return self.league_avg_p0

    def _make_prediction(
        self, snapshot: dict, signal_dict: dict
    ) -> Prediction:
        cur_server_was_a = bool(snapshot["server_was_a"])
        if (
            self._last_emitted_server_was_a is not None
            and cur_server_was_a == self._last_emitted_server_was_a
        ):
            raise RuntimeError(
                f"Serve-alternation invariant violated at "
                f"set {snapshot['set_number']} game "
                f"{snapshot['game_number_in_set']}: server did not "
                f"flip from previous emitted row"
            )
        self._last_emitted_server_was_a = cur_server_was_a

        next_server_is_a = not cur_server_was_a
        markov = set_win_prob(
            self.p0_a,
            self.p0_b,
            snapshot["games_A"],
            snapshot["games_B"],
            next_server_is_a,
        )

        features: dict[str, float] = {
            "games_A": float(snapshot["games_A"]),
            "games_B": float(snapshot["games_B"]),
            "set_number": float(snapshot["set_number"]),
            "sets_won_A": float(snapshot["sets_won_A"]),
            "sets_won_B": float(snapshot["sets_won_B"]),
            "game_number_in_set": float(snapshot["game_number_in_set"]),
            **{k: float(v) for k, v in self.surface_dummies.items()},
            **{col: float(signal_dict[col]) for col in SIGNAL_COLUMNS},
            "markov_set_win_prob_A": float(markov),
        }
        assert set(features) == set(self.feature_names), (
            "feature key mismatch — would silently misalign predict_proba"
        )

        X = np.array(
            [features[name] for name in self.feature_names],
            dtype=np.float64,
        ).reshape(1, -1)
        proba = self.model.predict_proba(X)[0]
        probability_a = float(proba[1])
        confidence = max(probability_a, 1.0 - probability_a)

        return Prediction(
            match_id_int=self.match_id_int,
            set_number=int(snapshot["set_number"]),
            game_number_in_set=int(snapshot["game_number_in_set"]),
            probability_a=probability_a,
            confidence=confidence,
            features=features,
        )
