"""Tests for src/live/collector.py — MatchWorker prediction wiring (Phase 6D).

All TennisFeed / MatchLogger / LivePredictionService / StateRowAdapter
interactions are mocked. No HTTP, no DB, no joblib load.

Groups (per Phase 6D Prompt 3):
  A — MatchWorker.__init__ field capture
  B — MatchCollector event extraction (data path event → worker attrs)
  C — _ensure_service_started gating
  D — _ensure_service_started success path
  E — _ensure_service_started failure paths
  F — _poll prediction hook
  G — _process_prediction_transition
  H — finalize on match end
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

# We import MatchWorker for type/class access. Real __init__ is bypassed in
# every test via __new__ or via patching TennisFeed/MatchLogger constructors.
from src.live.collector import MatchWorker
from src.live.state_row_adapter import NoLegalPathError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    *,
    match_id: int = 99,
    player_a: str = "Alcaraz",
    player_b: str = "Sinner",
    home_id: int | None = 1001,
    away_id: int | None = 2002,
    ground_type: str | None = "Red clay",
    country_a: str | None = "ESP",
    country_b: str | None = "ITA",
) -> dict:
    """Construct a synthetic discovery-event dict."""
    home_team: dict = {"name": player_a}
    if home_id is not None:
        home_team["id"] = home_id
    if country_a is not None:
        home_team["country"] = {"alpha2": country_a}
    away_team: dict = {"name": player_b}
    if away_id is not None:
        away_team["id"] = away_id
    if country_b is not None:
        away_team["country"] = {"alpha2": country_b}
    event: dict = {
        "id": match_id,
        "homeTeam": home_team,
        "awayTeam": away_team,
        "tournament": {
            "name": "Test Open",
            "category": {"slug": "atp"},
            "uniqueTournament": {"id": 1234},
        },
    }
    if ground_type is not None:
        event["groundType"] = ground_type
    return event


def _make_worker_init(
    event: dict | None = None,
    monkeypatch=None,
):
    """Construct a real MatchWorker with TennisFeed/MatchLogger mocked out.

    Returns the worker. Mocks the heavy constructors so __init__ runs but
    doesn't open real HTTP/DB connections. Env vars are NOT manipulated
    here — the caller is responsible for any monkeypatch.setenv calls
    before invoking this helper.
    """
    if event is None:
        event = _event()
    with patch("src.live.collector.TennisFeed") as feed_cls, \
         patch("src.live.collector.MatchLogger") as logger_cls:
        feed_cls.return_value = MagicMock(name="TennisFeed")
        logger_cls.return_value = MagicMock(name="MatchLogger")
        worker = MatchWorker(
            event=event,
            rapidapi_key="dummy",
            poll_interval=10,
            poll_logger=MagicMock(name="PollLogger"),
        )
    return worker


def _bare_worker(
    *,
    first_server: str | None = "home",
    player_a_id: int | None = 1001,
    player_b_id: int | None = 2002,
    raw_surface: str | None = "Red clay",
    predictions_enabled: bool = True,
    service=None,
    adapter=None,
    disabled: bool = False,
    poll_logger=None,
):
    """Construct a MatchWorker via __new__ with the minimum attributes
    required by the methods under test."""
    w = MatchWorker.__new__(MatchWorker)
    w._match_id = 99
    w._player_a = "Alcaraz"
    w._player_b = "Sinner"
    w._tournament_name = "Test Open"
    w._category = "atp"
    w._country_a = None
    w._country_b = None
    w._tournament_id = 1234
    w._poll_interval = 10
    w._running = True
    w._poll_logger = poll_logger
    w._spawned_at = time.time()
    w._max_runtime_seconds = 6 * 3600
    w._terminal_non_finished = {"canceled", "postponed", "walkover"}
    w._cumulative_points = 0
    w._last_score_state = None
    w._first_server = first_server
    w._first_server_attempted_count = 0
    w._first_server_max_attempts = 40
    w._player_a_id = player_a_id
    w._player_b_id = player_b_id
    w._raw_surface = raw_surface
    w._predictions_enabled = predictions_enabled
    w._service = service
    w._adapter = adapter
    w._prediction_layer_disabled = disabled
    w._service_construct_attempted = False
    w._feed = MagicMock(name="TennisFeed")
    w._logger = MagicMock(name="MatchLogger")
    return w


# ═══════════════════════════════════════════════════════════════════════════
# Group A — MatchWorker.__init__ field capture
# ═══════════════════════════════════════════════════════════════════════════

def test_init_captures_player_ids(monkeypatch):
    worker = _make_worker_init(
        event=_event(home_id=1234, away_id=5678),
        monkeypatch=monkeypatch,
    )
    assert worker._player_a_id == 1234
    assert worker._player_b_id == 5678


def test_init_captures_raw_surface(monkeypatch):
    worker = _make_worker_init(
        event=_event(ground_type="Hard"),
        monkeypatch=monkeypatch,
    )
    assert worker._raw_surface == "Hard"


def test_init_predictions_enabled_default_false(monkeypatch):
    """When the env var is not set, predictions are disabled by default."""
    monkeypatch.delenv("LIVE_PREDICTIONS_ENABLED", raising=False)
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is False


def test_init_predictions_enabled_true_with_env_flag_1(monkeypatch):
    monkeypatch.setenv("LIVE_PREDICTIONS_ENABLED", "1")
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is True


def test_init_predictions_enabled_true_with_env_flag_true(monkeypatch):
    monkeypatch.setenv("LIVE_PREDICTIONS_ENABLED", "true")
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is True


def test_init_predictions_enabled_true_with_env_flag_yes(monkeypatch):
    monkeypatch.setenv("LIVE_PREDICTIONS_ENABLED", "yes")
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is True


def test_init_predictions_enabled_case_insensitive(monkeypatch):
    monkeypatch.setenv("LIVE_PREDICTIONS_ENABLED", "TRUE")
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is True


def test_init_predictions_enabled_false_with_env_flag_0(monkeypatch):
    monkeypatch.setenv("LIVE_PREDICTIONS_ENABLED", "0")
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._predictions_enabled is False


def test_init_initial_service_is_none(monkeypatch):
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._service is None
    assert worker._adapter is None


def test_init_initial_disabled_flag_is_false(monkeypatch):
    worker = _make_worker_init(monkeypatch=monkeypatch)
    assert worker._prediction_layer_disabled is False
    assert worker._service_construct_attempted is False


# ═══════════════════════════════════════════════════════════════════════════
# Group B — MatchCollector event extraction (event dict → worker attrs)
# ═══════════════════════════════════════════════════════════════════════════

def test_collector_extracts_player_ids_from_event(monkeypatch):
    """The discovery event's homeTeam.id and awayTeam.id flow into the worker."""
    event = _event(home_id=4242, away_id=5353)
    worker = _make_worker_init(event=event, monkeypatch=monkeypatch)
    assert worker._player_a_id == 4242
    assert worker._player_b_id == 5353


def test_collector_handles_missing_team_id_gracefully(monkeypatch):
    """If the discovery event has no team IDs (e.g. pre-spawn payload),
    the worker stays in WAITING by carrying None for both IDs."""
    event = _event(home_id=None, away_id=None)
    worker = _make_worker_init(event=event, monkeypatch=monkeypatch)
    assert worker._player_a_id is None
    assert worker._player_b_id is None


def test_collector_handles_missing_hometeam_dict(monkeypatch):
    """Defensive — event with no homeTeam key at all shouldn't crash __init__."""
    event = _event()
    event["homeTeam"] = None
    # Now extraction must still produce a usable worker. But __init__ also
    # reads event["homeTeam"]["name"] earlier; let's only blank the optional
    # parts that the prediction-extraction touches.
    # (No crash if dict is empty.)
    event["homeTeam"] = {"name": "Alcaraz"}  # restore name, drop id/country
    worker = _make_worker_init(event=event, monkeypatch=monkeypatch)
    assert worker._player_a_id is None


def test_collector_extracts_ground_type(monkeypatch):
    event = _event(ground_type="Grass")
    worker = _make_worker_init(event=event, monkeypatch=monkeypatch)
    assert worker._raw_surface == "Grass"


def test_collector_handles_missing_ground_type(monkeypatch):
    """A discovery event with no groundType produces _raw_surface=None;
    the worker stays WAITING until a later poll surfaces surface info
    (in practice, /match-detail carries it)."""
    event = _event(ground_type=None)
    worker = _make_worker_init(event=event, monkeypatch=monkeypatch)
    assert worker._raw_surface is None


# ═══════════════════════════════════════════════════════════════════════════
# Group C — _ensure_service_started gating
# ═══════════════════════════════════════════════════════════════════════════

def test_ensure_skips_if_already_running():
    existing_service = MagicMock(name="LivePredictionService")
    w = _bare_worker(service=existing_service)
    w._ensure_service_started()
    # No-op: service already set.
    assert w._service is existing_service
    w._logger.fetch_state_rows_for_match.assert_not_called()


def test_ensure_skips_if_disabled():
    w = _bare_worker(disabled=True)
    w._ensure_service_started()
    assert w._service is None
    w._logger.fetch_state_rows_for_match.assert_not_called()


def test_ensure_skips_if_first_server_missing():
    w = _bare_worker(first_server=None)
    w._ensure_service_started()
    assert w._service is None
    assert w._service_construct_attempted is False  # didn't even attempt
    w._logger.fetch_state_rows_for_match.assert_not_called()


def test_ensure_skips_if_player_a_id_missing():
    w = _bare_worker(player_a_id=None)
    w._ensure_service_started()
    assert w._service is None
    assert w._service_construct_attempted is False
    w._logger.fetch_state_rows_for_match.assert_not_called()


def test_ensure_skips_if_player_b_id_missing():
    w = _bare_worker(player_b_id=None)
    w._ensure_service_started()
    assert w._service is None
    assert w._service_construct_attempted is False


def test_ensure_skips_if_raw_surface_missing():
    w = _bare_worker(raw_surface=None)
    w._ensure_service_started()
    assert w._service is None
    assert w._service_construct_attempted is False


# ═══════════════════════════════════════════════════════════════════════════
# Group D — _ensure_service_started success path
# ═══════════════════════════════════════════════════════════════════════════

@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_constructs_service_when_gates_met(svc_cls, adapter_cls):
    svc_instance = MagicMock(name="ServiceInstance")
    svc_instance._prediction_logger = MagicMock(name="RealLogger")
    svc_cls.return_value = svc_instance
    adapter_instance = MagicMock(name="AdapterInstance")
    adapter_instance.transition.return_value = []
    adapter_cls.return_value = adapter_instance

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = []

    w._ensure_service_started()

    assert w._service is svc_instance
    assert w._adapter is adapter_instance
    assert w._service_construct_attempted is True
    assert w._prediction_layer_disabled is False
    svc_cls.assert_called_once_with()


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_calls_start_match_with_correct_kwargs(svc_cls, adapter_cls):
    svc_instance = MagicMock()
    svc_instance._prediction_logger = MagicMock()
    svc_cls.return_value = svc_instance
    adapter_cls.return_value = MagicMock(transition=MagicMock(return_value=[]))

    w = _bare_worker(
        first_server="away",
        player_a_id=11,
        player_b_id=22,
        raw_surface="Hard",
    )
    w._match_id = 555
    w._player_a = "A"
    w._player_b = "B"
    w._logger.fetch_state_rows_for_match.return_value = []

    w._ensure_service_started()

    svc_instance.start_match.assert_called_once_with(
        match_id_int=555,
        player_a="A",
        player_b="B",
        raw_surface="Hard",
        first_server_is_a=False,  # first_server == 'away'
        player_a_id=11,
        player_b_id=22,
    )


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_calls_fetch_state_rows_for_match(svc_cls, adapter_cls):
    svc_cls.return_value = MagicMock(_prediction_logger=MagicMock())
    adapter_cls.return_value = MagicMock(transition=MagicMock(return_value=[]))

    w = _bare_worker()
    w._match_id = 777
    w._logger.fetch_state_rows_for_match.return_value = []

    w._ensure_service_started()

    w._logger.fetch_state_rows_for_match.assert_called_once_with(777)


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_replays_rows_through_adapter(svc_cls, adapter_cls):
    svc = MagicMock()
    svc._prediction_logger = MagicMock()
    svc_cls.return_value = svc
    fake_point = MagicMock(name="Point")
    adapter_inst = MagicMock()
    adapter_inst.transition.return_value = [fake_point]
    adapter_cls.return_value = adapter_inst

    w = _bare_worker()
    row1 = {"polled_at": "t1"}
    row2 = {"polled_at": "t2"}
    row3 = {"polled_at": "t3"}
    w._logger.fetch_state_rows_for_match.return_value = [row1, row2, row3]

    w._ensure_service_started()

    # Adapter called with (prev=None, curr=row1), then (row1, row2), then (row2, row3).
    assert adapter_inst.transition.call_args_list[0].args == (None, row1)
    assert adapter_inst.transition.call_args_list[1].args == (row1, row2)
    assert adapter_inst.transition.call_args_list[2].args == (row2, row3)
    # Each transition emitted one Point; service.process_point called 3x.
    assert svc.process_point.call_count == 3


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_mutes_logger_during_replay_and_restores_after(svc_cls, adapter_cls):
    """While replay is running, service._prediction_logger must be None;
    after replay it must be restored to its original value."""
    real_logger = MagicMock(name="RealLogger")
    svc = MagicMock()
    svc._prediction_logger = real_logger
    svc_cls.return_value = svc

    captured = []

    def transition_side_effect(prev, curr):
        # During the call, the logger should be muted (None).
        captured.append(svc._prediction_logger)
        return []

    adapter_inst = MagicMock()
    adapter_inst.transition.side_effect = transition_side_effect
    adapter_cls.return_value = adapter_inst

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = [{"r": 1}, {"r": 2}]

    w._ensure_service_started()

    # During every transition the logger was muted.
    assert captured == [None, None]
    # And it was restored after.
    assert svc._prediction_logger is real_logger


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_promotes_to_active_after_successful_replay(svc_cls, adapter_cls):
    svc = MagicMock()
    svc._prediction_logger = MagicMock()
    svc_cls.return_value = svc
    adapter_inst = MagicMock(transition=MagicMock(return_value=[]))
    adapter_cls.return_value = adapter_inst

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = []

    assert w._service is None  # WAITING
    w._ensure_service_started()
    # ACTIVE.
    assert w._service is svc
    assert w._adapter is adapter_inst
    assert w._prediction_layer_disabled is False


# ═══════════════════════════════════════════════════════════════════════════
# Group E — _ensure_service_started failure paths
# ═══════════════════════════════════════════════════════════════════════════

@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_handles_construct_exception(svc_cls):
    svc_cls.side_effect = RuntimeError("model not found")
    w = _bare_worker()

    w._ensure_service_started()

    assert w._service is None
    assert w._prediction_layer_disabled is True
    # Logger fetch was never reached.
    w._logger.fetch_state_rows_for_match.assert_not_called()


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_handles_fetch_exception(svc_cls, adapter_cls):
    svc_cls.return_value = MagicMock(_prediction_logger=MagicMock())
    adapter_cls.return_value = MagicMock()

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.side_effect = RuntimeError("db down")

    w._ensure_service_started()

    assert w._service is None
    assert w._prediction_layer_disabled is True


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_continues_replay_on_NoLegalPathError(svc_cls, adapter_cls):
    """A glitch in historical data is logged and skipped; replay continues."""
    svc = MagicMock()
    svc._prediction_logger = MagicMock()
    svc_cls.return_value = svc
    fake_point = MagicMock()
    adapter_inst = MagicMock()
    # Row 1: glitch. Row 2: clean (one point).
    adapter_inst.transition.side_effect = [
        NoLegalPathError({"r": 1}, {"r": 2}, "glitch"),
        [fake_point],
    ]
    adapter_cls.return_value = adapter_inst

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = [{"r": 1}, {"r": 2}]

    w._ensure_service_started()

    # Promoted to ACTIVE despite the glitch.
    assert w._service is svc
    # Replay attempted two transitions; the second's point reached the service.
    assert adapter_inst.transition.call_count == 2
    svc.process_point.assert_called_once_with(fake_point)


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_disables_on_replay_RuntimeError(svc_cls, adapter_cls):
    """An invariant violation in historical replay disables permanently."""
    svc = MagicMock()
    svc._prediction_logger = MagicMock(name="RealLogger")
    svc_cls.return_value = svc

    adapter_inst = MagicMock()
    fake_point = MagicMock()
    adapter_inst.transition.return_value = [fake_point]
    adapter_cls.return_value = adapter_inst

    # process_point raises RuntimeError on the first call.
    svc.process_point.side_effect = RuntimeError("invariant violated")

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = [{"r": 1}, {"r": 2}]

    w._ensure_service_started()

    assert w._service is None  # not promoted to ACTIVE
    assert w._prediction_layer_disabled is True


@patch("src.live.collector.StateRowAdapter")
@patch("src.live_prediction_service.LivePredictionService")
def test_ensure_restores_logger_even_if_replay_raises(svc_cls, adapter_cls):
    """The logger-mute must be restored via finally, even on RuntimeError."""
    real_logger = MagicMock(name="RealLogger")
    svc = MagicMock()
    svc._prediction_logger = real_logger
    svc_cls.return_value = svc

    fake_point = MagicMock()
    adapter_inst = MagicMock()
    adapter_inst.transition.return_value = [fake_point]
    adapter_cls.return_value = adapter_inst
    svc.process_point.side_effect = RuntimeError("oops")

    w = _bare_worker()
    w._logger.fetch_state_rows_for_match.return_value = [{"r": 1}]

    w._ensure_service_started()

    # finally must have restored the real logger.
    assert svc._prediction_logger is real_logger


# ═══════════════════════════════════════════════════════════════════════════
# Group F — _poll prediction hook
# ═══════════════════════════════════════════════════════════════════════════

def _stub_poll_prereqs(worker, *, status="inprogress", winner_code=None):
    """Patch the worker's feed/logger so _poll runs to (and past) the
    prediction hook without exercising real I/O.

    Returns the `upsert_match_detail_points` mock and the parsed_detail
    dict the worker will see.
    """
    parsed_detail = {
        "match_id": worker._match_id,
        "status": status,
        "home_sets": 0, "away_sets": 0,
        "home_period1": 1, "away_period1": 0,
        "home_period2": None, "away_period2": None,
        "home_period3": None, "away_period3": None,
        "home_current_point": "15", "away_current_point": "0",
        "winner_code": winner_code,
        "tournament_name": "Test Open", "category": "atp",
    }
    worker._feed.get_match_detail.return_value = {}
    worker._feed.parse_match_detail.return_value = parsed_detail
    worker._feed.get_first_server.return_value = "home"
    # Make logger.log_match_detail a no-op.
    worker._logger.log_match_detail.return_value = None
    return worker._logger.upsert_match_detail_points, parsed_detail


def test_poll_skips_prediction_when_predictions_disabled_flag_off():
    w = _bare_worker(predictions_enabled=False)
    upsert_mock, _ = _stub_poll_prereqs(w)
    upsert_mock.return_value = {"point_winner": "home", "prev": None}

    with patch.object(w, "_ensure_service_started") as ensure_mock, \
         patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    ensure_mock.assert_not_called()
    proc_mock.assert_not_called()


def test_poll_skips_prediction_when_permanently_disabled():
    w = _bare_worker(disabled=True)
    upsert_mock, _ = _stub_poll_prereqs(w)
    upsert_mock.return_value = {"point_winner": "home", "prev": None}

    with patch.object(w, "_ensure_service_started") as ensure_mock, \
         patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    ensure_mock.assert_not_called()
    proc_mock.assert_not_called()


def test_poll_skips_prediction_when_upsert_returns_none():
    """If the writer returned None (skip path), there's no new row to feed."""
    w = _bare_worker()
    upsert_mock, _ = _stub_poll_prereqs(w)
    upsert_mock.return_value = None

    with patch.object(w, "_ensure_service_started") as ensure_mock, \
         patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    ensure_mock.assert_not_called()
    proc_mock.assert_not_called()


def test_poll_calls_ensure_service_started_in_WAITING_state():
    """When service is None and gates are met, _poll calls _ensure_service_started."""
    w = _bare_worker()  # service=None, predictions_enabled=True
    upsert_mock, _ = _stub_poll_prereqs(w)
    upsert_mock.return_value = {"point_winner": "home", "prev": None}

    with patch.object(w, "_ensure_service_started") as ensure_mock, \
         patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    ensure_mock.assert_called_once()
    # We're in WAITING, not ACTIVE, so no process call.
    proc_mock.assert_not_called()


def test_poll_calls_process_prediction_in_ACTIVE_state():
    """When service is non-None, _poll calls _process_prediction_transition."""
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    w = _bare_worker(service=fake_svc, adapter=fake_adapter)
    upsert_mock, _ = _stub_poll_prereqs(w)
    inserted = {"point_winner": "home", "prev": None, "polled_at": "t"}
    upsert_mock.return_value = inserted

    with patch.object(w, "_ensure_service_started") as ensure_mock, \
         patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    ensure_mock.assert_not_called()
    proc_mock.assert_called_once()
    # Confirm it was called with the dict + the cycle_id.
    args = proc_mock.call_args.args
    assert args[0] is inserted
    assert isinstance(args[1], uuid.UUID)


def test_poll_passes_returned_dict_prev_to_adapter():
    """The dict the writer returns carries a `prev` field consumed by the
    adapter (via _process_prediction_transition). Verify the dict is passed
    through unchanged."""
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    w = _bare_worker(service=fake_svc, adapter=fake_adapter)
    upsert_mock, _ = _stub_poll_prereqs(w)
    prev_row = {"home_current_point": "0", "away_current_point": "0"}
    inserted = {
        "point_winner": "home",
        "prev": prev_row,
        "polled_at": "t",
        "home_current_point": "15",
        "away_current_point": "0",
    }
    upsert_mock.return_value = inserted

    with patch.object(w, "_process_prediction_transition") as proc_mock:
        w._poll()

    # The dict received by _process_prediction_transition is the same object
    # the writer returned — preserving the prev field for the adapter.
    assert proc_mock.call_args.args[0] is inserted
    assert proc_mock.call_args.args[0]["prev"] is prev_row


# ═══════════════════════════════════════════════════════════════════════════
# Group G — _process_prediction_transition
# ═══════════════════════════════════════════════════════════════════════════

def test_process_emits_points_to_service():
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    p1, p2 = MagicMock(), MagicMock()
    fake_adapter.transition.return_value = [p1, p2]
    w = _bare_worker(service=fake_svc, adapter=fake_adapter)

    inserted = {"prev": {"r": 1}, "polled_at": "t"}
    w._process_prediction_transition(inserted, uuid.uuid4())

    fake_adapter.transition.assert_called_once_with({"r": 1}, inserted)
    fake_svc.process_point.assert_any_call(p1)
    fake_svc.process_point.assert_any_call(p2)
    assert fake_svc.process_point.call_count == 2


def test_process_skips_on_NoLegalPathError_and_logs_PREDICTION_GLITCH():
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    fake_adapter.transition.side_effect = NoLegalPathError(
        {"r": 1}, {"r": 2}, "impossible regression"
    )
    poll_logger = MagicMock()
    w = _bare_worker(
        service=fake_svc, adapter=fake_adapter, poll_logger=poll_logger,
    )

    inserted = {"prev": {"r": 1}}
    cycle = uuid.uuid4()
    w._process_prediction_transition(inserted, cycle)

    # No points fed.
    fake_svc.process_point.assert_not_called()
    # Service still running (glitch is recoverable).
    assert w._service is fake_svc
    assert w._prediction_layer_disabled is False
    # PREDICTION_GLITCH event logged.
    poll_logger.log.assert_called_once()
    call = poll_logger.log.call_args
    assert call.kwargs["event_type"] == "PREDICTION_GLITCH"
    assert call.kwargs["match_id"] == w._match_id
    assert call.kwargs["poll_cycle_id"] == cycle
    assert "impossible regression" in call.kwargs["detail"]


def test_process_disables_on_RuntimeError_and_logs_PREDICTION_INVARIANT_VIOLATION():
    fake_svc = MagicMock()
    p1 = MagicMock()
    fake_adapter = MagicMock()
    fake_adapter.transition.return_value = [p1]
    fake_svc.process_point.side_effect = RuntimeError("serve alternation")
    poll_logger = MagicMock()
    w = _bare_worker(
        service=fake_svc, adapter=fake_adapter, poll_logger=poll_logger,
    )

    inserted = {"prev": None}
    cycle = uuid.uuid4()
    w._process_prediction_transition(inserted, cycle)

    # Permanent disable.
    assert w._prediction_layer_disabled is True
    assert w._service is None
    assert w._adapter is None
    # PREDICTION_INVARIANT_VIOLATION event logged.
    poll_logger.log.assert_called_once()
    call = poll_logger.log.call_args
    assert call.kwargs["event_type"] == "PREDICTION_INVARIANT_VIOLATION"
    assert call.kwargs["match_id"] == w._match_id
    assert call.kwargs["poll_cycle_id"] == cycle


def test_process_logs_POLL_ERROR_on_unexpected_adapter_exception_and_continues():
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    fake_adapter.transition.side_effect = ValueError("unexpected bug")
    poll_logger = MagicMock()
    w = _bare_worker(
        service=fake_svc, adapter=fake_adapter, poll_logger=poll_logger,
    )

    inserted = {"prev": None}
    cycle = uuid.uuid4()
    w._process_prediction_transition(inserted, cycle)

    # No points fed; service NOT disabled (unknown error is recoverable).
    fake_svc.process_point.assert_not_called()
    assert w._prediction_layer_disabled is False
    poll_logger.log.assert_called_once()
    call = poll_logger.log.call_args
    assert call.kwargs["event_type"] == "POLL_ERROR"
    assert "adapter:" in call.kwargs["detail"]


def test_process_logs_POLL_ERROR_on_unexpected_service_exception():
    fake_svc = MagicMock()
    p1 = MagicMock()
    fake_adapter = MagicMock()
    fake_adapter.transition.return_value = [p1]
    fake_svc.process_point.side_effect = ValueError("unexpected")
    poll_logger = MagicMock()
    w = _bare_worker(
        service=fake_svc, adapter=fake_adapter, poll_logger=poll_logger,
    )

    w._process_prediction_transition({"prev": None}, uuid.uuid4())

    # Service NOT disabled — only RuntimeError triggers permanent disable.
    assert w._prediction_layer_disabled is False
    assert w._service is fake_svc
    poll_logger.log.assert_called_once()
    call = poll_logger.log.call_args
    assert call.kwargs["event_type"] == "POLL_ERROR"
    assert "predict:" in call.kwargs["detail"]


# ═══════════════════════════════════════════════════════════════════════════
# Group H — finalize on match end
# ═══════════════════════════════════════════════════════════════════════════

def test_winner_code_calls_service_finalize_when_active():
    fake_svc = MagicMock()
    fake_adapter = MagicMock()
    w = _bare_worker(service=fake_svc, adapter=fake_adapter)
    upsert_mock, _ = _stub_poll_prereqs(w, winner_code=1)
    upsert_mock.return_value = None  # winner-code path doesn't need a fresh row

    w._poll()

    fake_svc.finalize.assert_called_once()
    assert w._running is False


def test_winner_code_does_not_call_finalize_when_service_is_None():
    w = _bare_worker(service=None)
    upsert_mock, _ = _stub_poll_prereqs(w, winner_code=2)
    upsert_mock.return_value = None

    w._poll()

    # No service to finalize on.
    assert w._running is False
    # No exception, no attribute error.


def test_winner_code_swallows_finalize_exception():
    fake_svc = MagicMock()
    fake_svc.finalize.side_effect = RuntimeError("finalize blew up")
    fake_adapter = MagicMock()
    w = _bare_worker(service=fake_svc, adapter=fake_adapter)
    upsert_mock, _ = _stub_poll_prereqs(w, winner_code=1)
    upsert_mock.return_value = None

    # Must NOT raise.
    w._poll()

    fake_svc.finalize.assert_called_once()
    assert w._running is False
