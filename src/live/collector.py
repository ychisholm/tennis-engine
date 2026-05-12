from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from src.live.logger import MatchLogger
from src.live.state_row_adapter import NoLegalPathError, StateRowAdapter
from src.live.tennis_feed import TennisFeed

if TYPE_CHECKING:
    from src.live_prediction_service import LivePredictionService

_log = logging.getLogger(__name__)

# Module-level lock guards all reads and writes to ACTIVE_MATCH_IDS.
_ACTIVE_IDS_LOCK = threading.Lock()
ACTIVE_MATCH_IDS: set[int] = set()

# Maps match_id → (country_a_alpha2, country_b_alpha2) for the live-match detail merge.
COUNTRY_MAP: dict[int, tuple[str | None, str | None]] = {}


class MatchWorker:
    """
    Polls one live match in a daemon thread, fetching match-detail snapshots
    and writing them to live.match_polls + live.match_states.
    Stops when the API reports a winner_code.
    """

    def __init__(
        self,
        event: dict,
        rapidapi_key: str,
        poll_interval: int = 10,
        poll_logger=None,
    ) -> None:
        self._match_id        = event["id"]
        self._player_a        = event["homeTeam"]["name"]
        self._player_b        = event["awayTeam"]["name"]
        self._country_a       = ((event.get("homeTeam") or {}).get("country") or {}).get("alpha2")
        self._country_b       = ((event.get("awayTeam") or {}).get("country") or {}).get("alpha2")
        COUNTRY_MAP[self._match_id] = (self._country_a, self._country_b)
        self._tournament_id   = (
            (event.get("tournament") or {})
            .get("uniqueTournament", {})
            .get("id")
        )
        self._tournament_name = (event.get("tournament") or {}).get("name", "Unknown")
        self._category        = (
            (event.get("tournament") or {})
            .get("category", {})
            .get("slug", "unknown")
        )
        # Prediction-layer metadata captured from the discovery event. All
        # three may be missing on the discovery payload (e.g. pre-spawn
        # before /match-detail has populated groundType); in that case the
        # worker stays in WAITING state until a later poll surfaces them.
        self._player_a_id     = (event.get("homeTeam") or {}).get("id")
        self._player_b_id     = (event.get("awayTeam") or {}).get("id")
        self._raw_surface     = event.get("groundType")
        self._poll_interval = poll_interval
        self._running       = True
        self._poll_logger   = poll_logger
        self._spawned_at = time.time()
        self._max_runtime_seconds = 6 * 3600
        self._terminal_non_finished = {"canceled", "postponed", "walkover"}
        # Counts score-state changes observed since this worker started polling,
        # NOT total points played in the match. Used as the points_count value
        # logged with POINTS_RECEIVED audit events.
        self._cumulative_points: int = 0
        self._last_score_state: tuple | None = None
        # Once learned from /point-by-point this is cached for the worker's
        # lifetime and persisted on every match_states row.
        self._first_server: Optional[str] = None
        self._first_server_attempted_count: int = 0
        # Cap how many times we'll poll point-by-point before giving up. 40
        # polls × 10s ≈ 6.5 minutes of attempts — well past when any real match
        # produces its first point.
        self._first_server_max_attempts: int = 40

        # Prediction-layer state machine (see Phase 6D recon).
        # Per-process opt-in env flag — default off for the first deploy.
        self._predictions_enabled = (
            os.environ.get("LIVE_PREDICTIONS_ENABLED", "0").lower()
            in {"1", "true", "yes"}
        )
        # WAITING:   _service is None AND _prediction_layer_disabled is False
        # ACTIVE:    _service is not None
        # DISABLED:  _prediction_layer_disabled is True (permanent for match)
        self._service: Optional["LivePredictionService"] = None
        self._adapter: Optional[StateRowAdapter] = None
        self._prediction_layer_disabled: bool = False
        self._service_construct_attempted: bool = False

        self._feed   = TennisFeed(api_key=rapidapi_key)
        self._logger = MatchLogger()
        with _ACTIVE_IDS_LOCK:
            ACTIVE_MATCH_IDS.add(self._match_id)

    def run(self) -> None:
        _log.info(
            "worker START: %s vs %s (%s, %s)",
            self._player_a, self._player_b, self._tournament_name, self._match_id,
        )
        try:
            while self._running:
                try:
                    self._poll()
                except Exception as exc:
                    _log.warning(
                        "worker error %s vs %s: %s",
                        self._player_a, self._player_b, exc,
                    )
                if self._running:
                    time.sleep(self._poll_interval)
        finally:
            with _ACTIVE_IDS_LOCK:
                ACTIVE_MATCH_IDS.discard(self._match_id)
            self._logger.close()
            _log.info(
                "worker STOP: %s vs %s (%s) — match finished",
                self._player_a, self._player_b, self._match_id,
            )

    def _poll(self) -> None:
        cycle_id = uuid.uuid4()
        polled_at = datetime.now(timezone.utc)
        try:
            raw_detail = self._feed.get_match_detail(
                self._match_id, poll_cycle_id=cycle_id,
            )
            parsed_detail = self._feed.parse_match_detail(
                raw_detail,
                match_id=self._match_id,
                player_a=self._player_a,
                player_b=self._player_b,
                tournament_name=self._tournament_name,
                category=self._category,
            )
            self._logger.log_match_detail(parsed_detail, polled_at=polled_at)
        except Exception as exc:
            _log.warning(
                "match_detail fetch/log error for %s vs %s: %s",
                self._player_a, self._player_b, exc,
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    match_id=self._match_id,
                    detail=str(exc)[:200],
                    poll_cycle_id=cycle_id,
                )
            parsed_detail = None

        if parsed_detail:
            status = parsed_detail.get("status")
            if status in self._terminal_non_finished:
                _log.warning(
                    "%s vs %s (match %s) — terminal non-finished status %s; stopping worker.",
                    self._player_a, self._player_b, self._match_id, status,
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

            if (time.time() - self._spawned_at) > self._max_runtime_seconds:
                _log.warning(
                    "%s vs %s (match %s) — exceeded max runtime (%ds); stopping worker.",
                    self._player_a, self._player_b, self._match_id,
                    self._max_runtime_seconds,
                )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

            score_state = (
                parsed_detail.get("home_sets"),
                parsed_detail.get("away_sets"),
                parsed_detail.get("home_period1"),
                parsed_detail.get("away_period1"),
                parsed_detail.get("home_period2"),
                parsed_detail.get("away_period2"),
                parsed_detail.get("home_period3"),
                parsed_detail.get("away_period3"),
                parsed_detail.get("home_current_point"),
                parsed_detail.get("away_current_point"),
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if status == "inprogress":
                if poll_logger is not None:
                    if score_state != getattr(self, "_last_score_state", None):
                        self._cumulative_points = getattr(self, "_cumulative_points", 0) + 1
                        poll_logger.log(
                            event_type="POINTS_RECEIVED",
                            match_id=self._match_id,
                            points_count=self._cumulative_points,
                            poll_cycle_id=cycle_id,
                        )
                    else:
                        poll_logger.log(
                            event_type="NO_NEW_POINTS",
                            match_id=self._match_id,
                            poll_cycle_id=cycle_id,
                        )
                self._last_score_state = score_state

            parsed_detail["country_a"] = getattr(self, "_country_a", None)
            parsed_detail["country_b"] = getattr(self, "_country_b", None)

            if (
                self._first_server is None
                and status == "inprogress"
                and self._first_server_attempted_count < self._first_server_max_attempts
            ):
                try:
                    self._first_server_attempted_count += 1
                    result = self._feed.get_first_server(
                        self._match_id, poll_cycle_id=cycle_id,
                    )
                    if result is not None:
                        self._first_server = result
                        self._logger.backfill_first_server(self._match_id, result)
                except Exception:
                    # Already audit-logged via TennisFeed; continue with poll.
                    pass

            upsert_result: Optional[dict] = None
            try:
                upsert_result = self._logger.upsert_match_detail_points(
                    parsed_detail, polled_at, first_server=self._first_server,
                )
            except Exception as exc:
                _log.warning(
                    "upsert_match_detail_points error for %s vs %s (match %s): %s",
                    self._player_a, self._player_b, self._match_id, exc,
                )
                poll_logger = getattr(self, "_poll_logger", None)
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="POLL_ERROR",
                        match_id=self._match_id,
                        detail=str(exc)[:200],
                        poll_cycle_id=cycle_id,
                    )

            # ── Prediction layer (Phase 6D) ─────────────────────────────
            # Only acts on polls that produced a fresh INSERT (upsert_result
            # is the inserted row's dict). Skip paths and ON-CONFLICT
            # no-ops return None — there's nothing new to feed the engine.
            if (
                self._predictions_enabled
                and not self._prediction_layer_disabled
                and upsert_result is not None
            ):
                if self._service is None:
                    # WAITING → try ACTIVE. On any unhandled error here,
                    # disable permanently for this match.
                    try:
                        self._ensure_service_started()
                    except Exception as exc:
                        _log.warning(
                            "prediction startup error for match %s: %s",
                            self._match_id, exc,
                        )
                        self._prediction_layer_disabled = True
                    # Replay (if it ran) covered the just-inserted row.
                    # If we're still WAITING, nothing else to do this poll.
                else:
                    # ACTIVE — feed the just-inserted row through the
                    # adapter and service.
                    self._process_prediction_transition(
                        upsert_result, cycle_id,
                    )

            if parsed_detail.get("winner_code"):
                _log.info(
                    "%s vs %s (match %s) — complete (winner_code=%s), stopping worker.",
                    self._player_a, self._player_b, self._match_id,
                    parsed_detail["winner_code"],
                )
                # Flush the final in-progress game's prediction. Never let
                # finalize() crash the match-end path.
                if self._service is not None:
                    try:
                        self._service.finalize()
                    except Exception as exc:
                        _log.warning(
                            "service.finalize error for match %s: %s",
                            self._match_id, exc,
                        )
                with _ACTIVE_IDS_LOCK:
                    ACTIVE_MATCH_IDS.discard(self._match_id)
                self._running = False
                return

        _log.debug(
            "%s vs %s — match detail polled",
            self._player_a, self._player_b,
        )

    def _ensure_service_started(self) -> None:
        """Transition prediction layer from WAITING → ACTIVE.

        Idempotent: returns immediately if the service is already running
        or the layer is permanently disabled for this match. Returns
        without side effects if any gate (first_server, player IDs,
        raw_surface) is unmet so a later poll can retry.

        On any unrecoverable error during construction or replay, sets
        ``self._prediction_layer_disabled = True`` so subsequent polls
        skip without retrying. Replay invariant violations also disable
        permanently — the historical state is unreplayable.
        """
        if self._service is not None or self._prediction_layer_disabled:
            return
        if self._first_server is None:
            return
        if self._player_a_id is None or self._player_b_id is None:
            return
        if self._raw_surface is None:
            return

        self._service_construct_attempted = True

        # Lazy-import the prediction service: joblib model load and
        # DuckDB read make it heavy to eagerly import at module level,
        # and test environments may not have those artifacts present.
        from src.live_prediction_service import LivePredictionService

        try:
            service = LivePredictionService(p0_lookup={})
            service.start_match(
                match_id_int=self._match_id,
                player_a=self._player_a,
                player_b=self._player_b,
                raw_surface=self._raw_surface,
                first_server_is_a=(self._first_server == "home"),
                player_a_id=self._player_a_id,
                player_b_id=self._player_b_id,
            )
            adapter = StateRowAdapter(first_server=self._first_server)
        except Exception as exc:
            _log.warning(
                "failed to construct prediction service for match %s: %s",
                self._match_id, exc,
            )
            self._prediction_layer_disabled = True
            return

        try:
            rows = self._logger.fetch_state_rows_for_match(self._match_id)
        except Exception as exc:
            _log.warning(
                "failed to fetch replay rows for match %s: %s",
                self._match_id, exc,
            )
            self._prediction_layer_disabled = True
            return

        # Mute the logger during replay so historical predictions do not
        # write to live.predictions. Direct attribute mutation per recon
        # §7 — the service supports None as a sentinel for "don't log."
        real_logger = service._prediction_logger
        service._prediction_logger = None
        try:
            prev: Optional[dict] = None
            for curr in rows:
                try:
                    points = adapter.transition(prev, curr)
                    for pt in points:
                        service.process_point(pt)
                except NoLegalPathError as exc:
                    _log.debug(
                        "glitch during replay for match %s: %s",
                        self._match_id, exc.reason,
                    )
                except RuntimeError as exc:
                    _log.warning(
                        "invariant violation during replay for match %s: %s",
                        self._match_id, exc,
                    )
                    self._prediction_layer_disabled = True
                    return
                prev = curr
        finally:
            service._prediction_logger = real_logger

        self._service = service
        self._adapter = adapter
        _log.info(
            "prediction service started for match %s (replayed %d rows)",
            self._match_id, len(rows),
        )

    def _process_prediction_transition(
        self,
        just_inserted: dict,
        cycle_id: uuid.UUID,
    ) -> None:
        """Run the adapter on (prev, curr) and feed Points to the service.

        - ``NoLegalPathError`` (data glitch): log debug, audit
          PREDICTION_GLITCH, continue.
        - ``RuntimeError`` from ``service.process_point`` (invariant
          violation): audit PREDICTION_INVARIANT_VIOLATION and
          permanently disable the prediction layer for this match.
        - Any other exception: audit POLL_ERROR and continue (or, for
          process_point, leave the service running).
        """
        prev = just_inserted.get("prev")
        curr = just_inserted
        try:
            points = self._adapter.transition(prev, curr)
        except NoLegalPathError as exc:
            _log.debug(
                "glitch transition for match %s: %s",
                self._match_id, exc.reason,
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="PREDICTION_GLITCH",
                    match_id=self._match_id,
                    detail=str(exc.reason)[:200],
                    poll_cycle_id=cycle_id,
                )
            return
        except Exception as exc:
            _log.warning(
                "adapter error for match %s: %s", self._match_id, exc,
            )
            poll_logger = getattr(self, "_poll_logger", None)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    match_id=self._match_id,
                    detail=f"adapter:{exc}"[:200],
                    poll_cycle_id=cycle_id,
                )
            return

        for pt in points:
            try:
                self._service.process_point(pt)
            except RuntimeError as exc:
                _log.warning(
                    "prediction invariant violated for match %s: %s",
                    self._match_id, exc,
                )
                poll_logger = getattr(self, "_poll_logger", None)
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="PREDICTION_INVARIANT_VIOLATION",
                        match_id=self._match_id,
                        detail=str(exc)[:200],
                        poll_cycle_id=cycle_id,
                    )
                self._prediction_layer_disabled = True
                self._service = None
                self._adapter = None
                return
            except Exception as exc:
                _log.warning(
                    "service.process_point error for match %s: %s",
                    self._match_id, exc,
                )
                poll_logger = getattr(self, "_poll_logger", None)
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="POLL_ERROR",
                        match_id=self._match_id,
                        detail=f"predict:{exc}"[:200],
                        poll_cycle_id=cycle_id,
                    )
                return

    def stop(self) -> None:
        self._running = False


class MatchCollector:
    """
    Discovers all qualifying live ATP/WTA singles matches every
    ``discovery_interval`` seconds and manages a MatchWorker daemon thread
    for each one. Controlled by an external scheduler via start()/stop().
    """

    def __init__(
        self,
        rapidapi_key: str,
        discovery_interval: int = 60,
        worker_poll_interval: int = 10,
        poll_logger=None,
    ) -> None:
        self._rapidapi_key        = rapidapi_key
        self._discovery_interval  = discovery_interval
        self._worker_poll_interval = worker_poll_interval
        self._poll_logger         = poll_logger
        self._active_lock   = threading.Lock()
        self._active: dict[Any, MatchWorker] = {}
        self._feed = TennisFeed(api_key=rapidapi_key)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="collector-loop",
        )
        self._thread.start()
        _log.info("MatchCollector started")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        with self._active_lock:
            workers = list(self._active.values())
            self._active.clear()
        for w in workers:
            w.stop()
        _log.info("MatchCollector stopped (%d workers signalled)", len(workers))

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                _log.warning("collector cycle error: %s", exc)
            # Interruptible sleep so stop() takes effect promptly.
            for _ in range(self._discovery_interval):
                if not self._running:
                    return
                time.sleep(1)

    def _cycle(self) -> None:
        cycle_id = uuid.uuid4()
        poll_logger = getattr(self, "_poll_logger", None)
        try:
            raw_events = self._feed.get_live_matches_raw(poll_cycle_id=cycle_id)
        except Exception as exc:
            _log.warning("collector failed to fetch live matches: %s", exc)
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    detail=str(exc)[:200],
                    poll_cycle_id=cycle_id,
                )
            return

        live_ids: set = set()
        for event in raw_events:
            if not self._is_qualifying(event):
                continue
            match_id = event.get("id")
            live_ids.add(match_id)

            with self._active_lock:
                already_active = match_id in self._active

            if not already_active:
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="MATCH_DISCOVERED",
                        match_id=match_id,
                        poll_cycle_id=cycle_id,
                    )
                worker = MatchWorker(
                    event=event,
                    rapidapi_key=self._rapidapi_key,
                    poll_interval=self._worker_poll_interval,
                    poll_logger=poll_logger,
                )
                thread = threading.Thread(
                    target=worker.run,
                    daemon=True,
                    name=f"worker-{match_id}",
                )
                thread.start()
                with self._active_lock:
                    self._active[match_id] = worker

        with self._active_lock:
            finished = [
                mid for mid, w in self._active.items() if not w._running
            ]
            for mid in finished:
                self._active[mid].stop()
                del self._active[mid]
                if poll_logger is not None:
                    poll_logger.log(
                        event_type="MATCH_ENDED",
                        match_id=mid,
                        poll_cycle_id=cycle_id,
                    )

        with self._active_lock:
            match_labels = ", ".join(
                f"{w._player_a} vs {w._player_b}" for w in self._active.values()
            )
            active_count = len(self._active)

        _log.info(
            "collector cycle complete | active: %d matches | monitoring: %s",
            active_count, match_labels or "none",
        )

    def spawn_pre_match_worker(self, raw_event: dict) -> None:
        """Idempotently spin up a MatchWorker for a not-yet-live match so that
        match_detail polling is already running when the API flips status to
        inprogress. Safe to call repeatedly for the same match — if a worker
        already exists for this match_id, this is a no-op."""
        match_id = raw_event.get("id")
        if match_id is None:
            return

        with self._active_lock:
            if match_id in self._active:
                return

        poll_logger = getattr(self, "_poll_logger", None)
        worker = MatchWorker(
            event=raw_event,
            rapidapi_key=self._rapidapi_key,
            poll_interval=self._worker_poll_interval,
            poll_logger=poll_logger,
        )
        thread = threading.Thread(
            target=worker.run,
            daemon=True,
            name=f"worker-{match_id}",
        )
        thread.start()
        with self._active_lock:
            # Re-check under lock to prevent two pre-spawns from racing the
            # _active check above.
            if match_id in self._active:
                worker.stop()
                return
            self._active[match_id] = worker
        _log.info("pre-spawn worker for match %s", match_id)
        if poll_logger is not None:
            poll_logger.log(
                event_type="MATCH_DISCOVERED",
                match_id=match_id,
                detail="pre_spawn",
            )

    @staticmethod
    def _is_qualifying(event: dict) -> bool:
        # Rule 1: ATP or WTA only — excludes Challenger, ITF, exhibition
        try:
            category_slug = event["tournament"]["category"]["slug"]
        except (KeyError, TypeError):
            return False
        if category_slug not in ("atp", "wta"):
            return False

        # Rule 2: singles only — reject if "doubles" appears in eventFilters.category
        try:
            ef_category = event["eventFilters"]["category"]
        except (KeyError, TypeError):
            ef_category = ""
        if "doubles" in str(ef_category).lower():
            return False

        # Rule 3: match must currently be live
        try:
            status_type = event["status"]["type"]
        except (KeyError, TypeError):
            return False
        if status_type != "inprogress":
            return False

        return True
