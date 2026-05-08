# Tennis Engine — Live Pipeline Audit Report

Generated: 2026-05-08
Audit scope: read-only review of every file in `src/`, `scripts/`, `tests/`, root, plus the live PostgreSQL database
Methodology: every active code path was read in full; the database schema, row counts, and a worked example match were inspected directly.

---

## EXECUTIVE SUMMARY

**What the system does.** The tennis-engine project is a real-time tracker for ATP and WTA singles tennis matches. A single Python process runs continuously on a DigitalOcean droplet under systemd. It calls a third-party tennis data API (TennisAPI1, hosted on RapidAPI) to discover live and upcoming matches, polls each in-progress match every 15 seconds for its current score, parses the response into a structured score state, and writes that state to a managed PostgreSQL database. A FastAPI web server runs in the same process; it reads from the database and serves a React-based dashboard at `/dashboard` so a human can watch the live scoreboard. The data being captured is intended to feed a (separately developed) machine-learning model that predicts set winners, but the prediction model is not yet deployed in the live path — what is running today is purely the data-collection layer plus the dashboard.

**Moving parts.** A single Python entry point (`scripts/run_collector.py`) starts:
1. one APScheduler `BackgroundScheduler` running a state machine (`MatchScheduler`),
2. a `MatchCollector` daemon thread that, when state is LIVE, discovers qualifying matches every 60 seconds,
3. one `MatchWorker` daemon thread per match-in-progress (15-second polling),
4. one Uvicorn-hosted FastAPI app on port 8000 serving the dashboard and four REST endpoints,
5. three audit loggers — an `ApiLogger` (process-wide singleton), a `PollLogger`, and per-thread `MatchLogger` instances — each writing to its own dedicated table.

The database has four active schemas (`live`, `audit`, `core`, `rankings`) plus three legacy schemas (`live_raw`, `live_processed`, `book`) holding either pre-migration data or empty placeholders. Eleven tables are actively read or written; six are dormant.

**What is healthy.**
- The live ingestion path is working end-to-end as of 2026-05-08: 24,705 score-state rows across 185 distinct matches over the past week, with active polling visible in `audit.poll_audit_log` within seconds of the report time.
- Auditing is comprehensive: every API call is logged (29,770 rows in `audit.api_call_log`), 28,066 raw payloads are archived (`audit.api_response_archive`), and 31,322 polling-loop events are recorded (`audit.poll_audit_log`).
- The state machine, pre-spawn, score-state deduplication, and idempotent worker management all behave correctly. The `triggering_call_id` column being NULL is the only audit-table issue and is harmless.
- Phase 1 schema migration (moving tables out of `live_raw`/`live_processed` into `live`/`audit`/`book`) is complete; production code paths reference only the new schemas.
- Phase 2 audit-table scaffolding (`audit.verification_reports`, `audit.live_gap_reports`) is built and the validator is unit-tested, but no orchestrator runs it yet.

**Top three things to know about current state.**
1. **The data-collection engine is fully operational; the prediction layer is not yet wired in.** Everything from API polling to dashboard rendering works. The "Model coming soon" placeholders in the dashboard are accurate — the trained XGBoost model exists offline but no live inference happens in production. The `dashboard_log` table that historically held model predictions still has rows from earlier builds, but no current code writes to it.
2. **There is significant orphaned code and three dormant database schemas left over from Phase 1.** The `live_raw` and `live_processed` schemas still contain real data (tens of thousands of rows of pre-migration history) but nothing reads or writes them. The `book` schema is created at startup but holds zero tables. The whole `src/engine/`, `src/backtesting/`, `src/baseline.py`, etc., subtree is offline-only utility code, not on the live path.
3. **The verification/validation pipeline is half-built.** `src/verification/validator.py` is a working pure-Python score-graph validator with a passing test suite. The `audit.verification_reports` and `audit.live_gap_reports` tables exist. But there is no scheduler job, no script, and no orchestrator that calls the validator and writes its output to those tables — both are at zero rows.

---

## SECTION 1 — FILE INVENTORY

Status legend: **ACTIVE** = reachable from the running production process via `scripts/run_collector.py`. **UTILITY** = used only by manual/dev/setup/backtest scripts. **INACTIVE** = not imported by anything live, used only for tests or fully orphaned.

### Root-level files

| File | Status | Purpose |
| --- | --- | --- |
| `tennis-engine.service` | ACTIVE | systemd unit; `ExecStart` = `/home/ubuntu/tennis-engine/.venv/bin/python scripts/run_collector.py`; restarts on failure with 10 s delay; runs as user `ubuntu`. |
| `requirements.txt` | ACTIVE | Python deps — fastapi, uvicorn, apscheduler, psycopg2-binary, requests, dotenv, plus offline-only ones (xgboost, scikit-learn, joblib, duckdb, rapidfuzz, matplotlib). |
| `.env` | ACTIVE | Holds RAPIDAPI_KEY, ODDS_API_KEY, DATABASE_URL, PG_CA_CERT_PATH. |
| `.env.example` | UTILITY | Template for collaborators. |
| `ca-certificate.crt` | UTILITY | Digital Ocean managed PG CA cert; referenced by env var only — psycopg2 connects with `sslmode=require` (no client cert today). |
| `PROJECT_LOG.md` | UTILITY | Historical project log, last updated early 2026; describes Markov engine work; out of date relative to current state. |
| `tennis_engine_reference_guide_v4.md` | UTILITY | Top-level architecture and strategy doc, version 4 (May 2026). The de facto product spec. Mentions a `p0_engine.py` and `streaming_signal_engine.py` that do not exist in the repo. |

### `scripts/`

| File | Status | Purpose |
| --- | --- | --- |
| `scripts/run_collector.py` | **ACTIVE — entry point** | Boots logging, starts FastAPI in a daemon thread, instantiates PollLogger/TennisFeed/MatchCollector/MatchLogger/MatchScheduler, runs forever. |
| `scripts/__init__.py` | ACTIVE | Empty package marker. |
| `scripts/backtesting/backfill_today.py` | UTILITY | Interactive CLI to backfill `live.backfill_points` from TennisAPI1 `/event/{id}/point-by-point` for a chosen tournament/date. Implements the same game-num derivation algorithm used by `tennis_feed.translate_to_engine_format`. |
| `scripts/backtesting/backfill_tournament_odds.py` | UTILITY | Interactive CLI; uses The Odds API historical snapshot endpoint to backfill `live.backfill_odds_polls` at 5-minute intervals across a tournament window. |
| `scripts/backtesting/run_backtest.py` | UTILITY | Runs `src/backtesting/backtester.py` against historical Sackmann data; offline ML-pipeline tooling. |
| `scripts/backtesting/run_full_backfill.py` | UTILITY | Orchestrator combining `backfill_today.py` (Phase 1: points) and `backfill_tournament_odds.py` (Phase 2: odds) for one tournament-day. |
| `scripts/dev/audit_match.py` | UTILITY | Diagnostic CLI; prints chronological timeline from `audit.poll_audit_log` for one match_id. |
| `scripts/dev/audit_schema_usage.py` | UTILITY | One-off DB introspection script that produced `data/audits/schema_audit_2026-05-07.md`. |
| `scripts/dev/build_ml_game_level.py` | UTILITY | Builds `core.ml_game_level` ML training set; offline. |
| `scripts/dev/build_signals.py` | UTILITY | Computes 52 signal sub-components; offline. |
| `scripts/dev/evaluate_model.py` | UTILITY | Calibration/Brier evaluation; offline. |
| `scripts/dev/run_baseline.py` | UTILITY | Hard-pick + Markov baseline metrics; offline. |
| `scripts/dev/train_model.py` | UTILITY | XGBoost train + calibrate + save; offline. |
| `scripts/setup/migrate_to_v2_schemas.py` | UTILITY | One-time Phase 1 schema migration. |
| `scripts/setup/migrate_phase2_audit_tables.py` | UTILITY | One-time DDL migration creating `audit.verification_reports` and `audit.live_gap_reports`. |
| `scripts/setup/setup_rankings_and_schemas.py` | UTILITY | One-time download of ATP player/ranking data. |

### `src/live/` — the live pipeline package

| File | Status | Purpose |
| --- | --- | --- |
| `src/live/__init__.py` | ACTIVE | Package marker (empty). |
| `src/live/scheduler.py` | ACTIVE | `MatchScheduler` — APScheduler-driven IDLE → PRE_MATCH → LIVE state machine. |
| `src/live/collector.py` | ACTIVE | `MatchCollector` (discovery loop) and `MatchWorker` (per-match poller). Owns the in-process `ACTIVE_MATCH_IDS` set and `COUNTRY_MAP`. |
| `src/live/tennis_feed.py` | ACTIVE | `TennisFeed` — HTTP wrapper around TennisAPI1 with retry, audit-logging hook, and the point-by-point parser used both live (offline today) and by the backfill scripts. |
| `src/live/api_logger.py` | ACTIVE | `ApiLogger` (process-wide singleton) writing `audit.api_call_log` for every HTTP call and conditionally `audit.api_response_archive`. |
| `src/live/logger.py` | ACTIVE | `MatchLogger` — owns DDL for `live.*` and `audit.api_*`; writes `live.match_polls` (raw) and `live.match_states` (deduped). Each `MatchWorker` instantiates its own MatchLogger. |
| `src/live/backend.py` | ACTIVE | FastAPI app: `/matches`, `/live_matches`, `/match/{id}`, `/upcoming_matches`, `/dashboard`, `/`. |
| `src/live/odds_fetcher.py` | INACTIVE | The Odds API client. Not called by the live pipeline today; only `backfill_tournament_odds.py` imports `TOURNAMENT_MAP`, `_compute_consensus`, and `_TENNIS_SPORT_KEYS` from it. ORPHANED in production: was originally intended for live event-triggered odds polling per the scheduler.py docstring ("event-trigger odds fetches"), but no scheduler hook calls it. |
| `src/live/player_lookup.py` | INACTIVE | Computes player p0 and archetype vector from `core.{atp,wta}_matches`. Imported only by the engine subtree; engine subtree is itself offline. ORPHANED in production. |

### `src/poll_logger.py`

| File | Status | Purpose |
| --- | --- | --- |
| `src/poll_logger.py` | ACTIVE | `PollLogger` writing `audit.poll_audit_log`. Single instance shared across scheduler/collector/worker. Failures swallowed silently by design. |

### `src/verification/`

| File | Status | Purpose |
| --- | --- | --- |
| `src/verification/__init__.py` | UTILITY | Empty marker. |
| `src/verification/db_setup.py` | UTILITY | Idempotent DDL helper used by `migrate_phase2_audit_tables.py`. |
| `src/verification/validator.py` | UTILITY | Pure-function score-state graph walker producing `Gap` and `ValidationSummary` objects. **Built and unit-tested but NEVER CALLED** by production. No orchestrator wires its output to `audit.verification_reports`. |

### `src/dashboard/`

| File | Status | Purpose |
| --- | --- | --- |
| `src/dashboard/index.html` | ACTIVE | Single-page React/Recharts dashboard; served by `/dashboard`. ~1800 lines, embeds Babel-standalone for in-browser JSX compilation. |
| `src/dashboard/index.backup.html` | INACTIVE | Older copy of the dashboard. ORPHANED. |

### `src/engine/` and other offline ML modules

These are the ML pipeline (Steps 1–5 in the project's reference guide). None of them are reachable from `scripts/run_collector.py`; they are exercised only by `scripts/dev/*` (training, evaluation) and `scripts/backtesting/run_backtest.py` (backtesting).

| File | Status | Purpose |
| --- | --- | --- |
| `src/baseline.py` | UTILITY | Naive Markov + hard-pick baseline. |
| `src/data_loader.py` | UTILITY | Loads `core.ml_game_level` into train/test arrays. |
| `src/evaluation.py` | UTILITY | Calibration + Brier-by-game-num + feature importance. |
| `src/model_training.py` | UTILITY | XGBoost train + calibrate. |
| `src/signal_engine.py` | UTILITY | Batch signal computation walker (52 sub-components). |
| `src/engine/archetype_engine.py` | INACTIVE | Originally intended to feed live archetype computation. ORPHANED — nothing imports it. |
| `src/engine/live_match.py` | UTILITY | Composes signals/temporal/markov/phat into one orchestrator; used only by the backtester. |
| `src/engine/markov_engine.py` | UTILITY | Pre-computed lookup tables for game/tiebreak/set/match probabilities. |
| `src/engine/phat_adjuster.py` | UTILITY | Sigmoid p-hat adjustment. |
| `src/engine/temporal_engine.py` | UTILITY | Recency-weighted dominance. |
| `src/engine/signals/{gps,nmi,pms,rms,sms}.py` | UTILITY | Five signal calculators. |
| `src/pipeline/audit_points.py` | UTILITY | Coverage/null diagnostics for `core.atp_points_enhanced`. |
| `src/pipeline/data_pipeline.py` | UTILITY | Sackmann CSV → DuckDB ingestion (`tennis.duckdb`). |
| `src/backtesting/backtester.py` | UTILITY | Replays historical matches through `live_match.py`. |
| `src/backtesting/parameter_tuning.py` | UTILITY | Grid-search over engine params. |

### `tests/`

All 28 test files are UTILITY (developer-only). Mapping to source modules:

| Test file | Module under test |
| --- | --- |
| `tests/test_api_logger.py` | `src/live/api_logger.py` |
| `tests/test_logger.py` | `src/live/logger.py` |
| `tests/test_odds_fetcher.py` | `src/live/odds_fetcher.py` |
| `tests/test_poll_logger.py` | `src/poll_logger.py` |
| `tests/test_pre_spawn.py` | scheduler ↔ collector pre-spawn integration |
| `tests/test_scheduler.py` | `src/live/scheduler.py` |
| `tests/test_tennis_feed.py` | `src/live/tennis_feed.py` |
| `tests/test_validator.py` | `src/verification/validator.py` |
| `tests/test_archetype_engine.py` | `src/engine/archetype_engine.py` |
| `tests/test_backtester.py` | `src/backtesting/backtester.py` |
| `tests/test_baseline.py` | `src/baseline.py` |
| `tests/test_data_loader.py` | `src/data_loader.py` |
| `tests/test_data_pipeline.py` | `src/pipeline/data_pipeline.py` |
| `tests/test_evaluation.py` | `src/evaluation.py` |
| `tests/test_gps.py`, `test_nmi.py`, `test_pms.py`, `test_rms.py`, `test_sms.py` | each signal in `src/engine/signals/` |
| `tests/test_live_match.py` | `src/engine/live_match.py` |
| `tests/test_markov_engine.py` | `src/engine/markov_engine.py` |
| `tests/test_migration.py` | `scripts/setup/migrate_to_v2_schemas.py` |
| `tests/test_ml_game_level.py` | `core.ml_game_level` (table integrity) |
| `tests/test_model_training.py` | `src/model_training.py` |
| `tests/test_parameter_tuning.py` | `src/backtesting/parameter_tuning.py` |
| `tests/test_phat_adjuster.py` | `src/engine/phat_adjuster.py` |
| `tests/test_signal_engine.py` | `src/signal_engine.py` |
| `tests/test_temporal_engine.py` | `src/engine/temporal_engine.py` |

---

## SECTION 2 — SYSTEM ENTRY POINT AND PROCESS ARCHITECTURE

### Service definition

`/etc/systemd/system/tennis-engine.service` (a copy is committed at the repo root as `tennis-engine.service`):

- **Type:** `simple`
- **User:** `ubuntu`
- **WorkingDirectory:** `/home/ubuntu/tennis-engine`
- **ExecStart:** `/home/ubuntu/tennis-engine/.venv/bin/python scripts/run_collector.py`
- **Restart:** `on-failure`, `RestartSec=10`
- **EnvironmentFile:** `/home/ubuntu/tennis-engine/.env` (loads RAPIDAPI_KEY, ODDS_API_KEY, DATABASE_URL)

Everything below runs inside that single Python process.

### Startup sequence (`scripts/run_collector.py`)

1. Load `.env` from project root.
2. Configure root logger to write both stdout (which systemd captures) and `logs/collector.log`.
3. Verify `RAPIDAPI_KEY` env var is set; raise on missing.
4. Spawn a daemon thread named `backend` running `uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")` against the FastAPI `app` from `src/live/backend.py`. Sleep 1 second so the dashboard is reachable before logging the URL.
5. Construct `PollLogger()` and call `poll_logger.setup()` — this issues the `CREATE SCHEMA IF NOT EXISTS audit` and `CREATE TABLE IF NOT EXISTS audit.poll_audit_log` DDL.
6. Construct `TennisFeed(api_key=...)`. The feed's constructor lazy-initialises the process-wide singleton `ApiLogger` via `get_default_logger()` on first audit-log call.
7. Construct `MatchCollector(rapidapi_key=..., poll_logger=poll_logger)`. The collector internally builds its own `TennisFeed` for discovery.
8. Construct `MatchLogger()` (this one is owned by the scheduler — separate instances exist per `MatchWorker`). The MatchLogger constructor runs all the DDL for `live.*`, `audit.api_*` schemas/tables/indexes.
9. Construct `MatchScheduler(feed, collector, logger=scheduler_logger, poll_logger=poll_logger)`.
10. Call `scheduler.start()` — registers a single APScheduler `interval` job called `check_schedule` (initial interval 3600 s in IDLE) and immediately invokes `_check_schedule()`.
11. Sit in `while True: time.sleep(60)` until SIGINT/SIGTERM.

### Steady-state thread inventory

When the system is in the LIVE state with N matches in progress:

1. **Main thread.** Idles on `time.sleep(60)`.
2. **`backend` thread (daemon).** Uvicorn worker serving FastAPI on port 8000.
3. **APScheduler `BackgroundScheduler` thread + 1 worker thread.** Runs `MatchScheduler._check_schedule` every 60 s in PRE_MATCH/LIVE, every 3600 s in IDLE.
4. **`collector-loop` thread (daemon).** Started by `MatchCollector.start()` when the scheduler enters LIVE; runs `_cycle()` every 60 s discovering qualifying live matches.
5. **`worker-{match_id}` threads (daemon, one per active match).** Each is a `MatchWorker.run()` loop polling `/api/tennis/event/{match_id}` every 15 s. Stops when the API reports a `winnerCode`, or status flips to a terminal non-finished value (canceled/postponed/walkover), or runtime exceeds 6 h.

So the steady-state thread count is roughly `4 + N`, where N is the number of live matches.

### Shared state

- `src/live/collector.ACTIVE_MATCH_IDS: set[int]` — guarded by `_ACTIVE_IDS_LOCK`. Workers add their match_id on construction and remove it on shutdown. The FastAPI `/live_matches` endpoint reads this set to filter the database query.
- `src/live/collector.COUNTRY_MAP: dict[int, (str|None, str|None)]` — populated by each MatchWorker on construction. Used by `/live_matches` as a fallback when a row's country columns haven't yet been persisted.
- The process-wide `ApiLogger` singleton (one psycopg2 connection, lock-protected).

### Plain-English diagram of the process architecture

1. systemd starts one Python process.
2. That process spawns a uvicorn web server on port 8000 (the dashboard backend).
3. It also constructs a database-backed event logger (PollLogger) that records every significant moment in the polling loop.
4. It then constructs a TennisFeed, which is the wrapper around the third-party tennis API.
5. It constructs a MatchCollector that knows how to discover live matches, and a MatchScheduler that decides when the collector should be running.
6. The MatchScheduler kicks off a recurring check, every 60 seconds when matches are happening or about to start, and every 3600 seconds when nothing is going on.
7. Each check pulls down the list of upcoming matches and the list of currently-live matches.
8. If any live ATP/WTA singles match is in progress, the scheduler enters its LIVE state and tells the collector to run.
9. The collector spawns one daemon thread per live match. That thread (a MatchWorker) hits the per-match endpoint every 15 seconds, parses the score, and writes both a raw snapshot (live.match_polls) and a deduped score-state row (live.match_states) into PostgreSQL.
10. When the API reports a winner, the worker stops itself and the collector cleans it up on the next 60-second cycle.
11. The dashboard, served from the same process, polls its own backend every 15 seconds for live matches and every 5 minutes for upcoming matches, displaying everything to the user.

---

## SECTION 3 — THE LIVE MATCH TRACKING PIPELINE — END TO END

### 3A. Match Discovery

**Who polls.** `MatchScheduler._check_schedule` (the APScheduler-driven loop) AND the `MatchCollector._cycle` (which only runs when state is LIVE) both call into `TennisFeed.get_live_matches_raw`.

**Endpoint called.** `GET https://tennisapi1.p.rapidapi.com/api/tennis/events/live`.

**Interval.** The scheduler hits it every 3600 s in IDLE, every 60 s in PRE_MATCH/LIVE. The collector hits it every 60 s independently while LIVE. So during LIVE there are two parallel discovery loops — both write to `audit.api_call_log`, which is why `live_matches` shows up there with 1,546 calls but `match_details` shows 26,539.

**Filtering rules** (in `MatchCollector._is_qualifying`):
1. `event["tournament"]["category"]["slug"]` must be `"atp"` or `"wta"`.
2. `event["eventFilters"]["category"]` must NOT contain `"doubles"` (case-insensitive).
3. `event["status"]["type"]` must be `"inprogress"`.

**Fields used from each event.**

| Path in API event | Used for |
| --- | --- |
| `id` | match_id (primary key throughout the system) |
| `homeTeam.name` | player_a |
| `awayTeam.name` | player_b |
| `homeTeam.country.alpha2` | country_a (saved to `live.match_states` and `COUNTRY_MAP`) |
| `awayTeam.country.alpha2` | country_b |
| `tournament.uniqueTournament.id` | tournament_id (used for odds lookup) |
| `tournament.name` | tournament_name |
| `tournament.category.slug` | category (`"atp"` or `"wta"`) |
| `tournament.category.name` | used by backfill scripts to filter |
| `eventFilters.category` | used to exclude doubles |
| `status.type` | used to gate qualification |
| `startTimestamp` (upcoming endpoint) | scheduled start (used by scheduler & dashboard) |
| `winnerCode` (match-detail endpoint) | terminator for the worker |
| `homeScore.{current,period1,period2,period3,point}` | sets/games/point score |
| `awayScore.{current,period1,period2,period3,point}` | sets/games/point score |

**Newness check.** `MatchCollector._cycle` keeps a `dict[match_id → MatchWorker]` called `self._active`. For each event in the qualifying set, it checks `match_id in self._active` under a lock. If absent, it spawns a new worker; if present, no-op. There is also a separate idempotent path used during PRE_MATCH: `MatchScheduler._pre_spawn_imminent` calls `MatchCollector.spawn_pre_match_worker(raw_event)` for any upcoming match within 300 s of start. That method re-checks under lock to guarantee no double-spawn.

**State on discovery.**
- A `MatchWorker` is constructed (its `__init__` adds `match_id` to `ACTIVE_MATCH_IDS` and writes `(country_a, country_b)` into `COUNTRY_MAP`).
- A daemon thread is started running `worker.run()`.
- The worker's first poll fires almost immediately. The first row gets written to `live.match_polls` and (assuming non-null point scores) to `live.match_states`.
- The `PollLogger` records a `MATCH_DISCOVERED` event in `audit.poll_audit_log`. (Pre-spawn discoveries are tagged with `detail="pre_spawn"`.)

### 3B. Per-Match Polling

**Who polls.** `MatchWorker.run` calls `MatchWorker._poll` in a loop with 15-second sleeps.

**Endpoint called.** `GET https://tennisapi1.p.rapidapi.com/api/tennis/event/{match_id}`.

**Interval.** 15 s (`worker_poll_interval` in MatchCollector defaults to 15). The worker sleeps interruptibly between polls; on shutdown it exits within one second.

**Termination conditions** (any one stops the worker):
- `parsed_detail["winner_code"]` is non-null,
- `parsed_detail["status"]` is in `{"canceled", "postponed", "walkover"}`,
- runtime since spawn exceeds 6 hours (`_max_runtime_seconds = 6*3600`),
- `MatchCollector.stop()` was called (state machine left LIVE).

**Fields actually consumed by the parser** (`TennisFeed.parse_match_detail`):

| Path in `event` (response is `{event: {...}}` or `{...}`) | Stored as |
| --- | --- |
| `homeScore.current` | home_sets |
| `awayScore.current` | away_sets |
| `homeScore.period1`, `period2`, `period3` | home_set1_games, home_set2_games, home_set3_games |
| `awayScore.period1`, `period2`, `period3` | away_set1_games, away_set2_games, away_set3_games |
| `homeScore.point` | home_current_point ("0", "15", "30", "40", "A", or integer-as-string in tiebreaks) |
| `awayScore.point` | away_current_point |
| `status.type` | status |
| `winnerCode` | winner_code (1, 2, or null) |

**Parsing assumptions.**
- `home_*` is treated as Player A and `away_*` is treated as Player B throughout the pipeline. There is no logic that checks which side is actually serving the first point of the match — the system trusts the API's home/away assignment.
- The "current set" is computed downstream as `home_sets + away_sets + 1`. If neither side has won a set yet, it is set 1; if it's 1-1, it's set 3; etc.
- The parser does NOT call the player_lookup or odds fetcher — it just rolls up score state.

The parser yields a single dict per poll. That dict is then passed to `MatchLogger.log_match_detail` (always inserts a row in `live.match_polls`) and `MatchLogger.upsert_match_detail_points` (conditionally inserts into `live.match_states`).

### 3C. Score and State Tracking

**Detecting a new point.** Score-state delta detection is done in `MatchLogger.upsert_match_detail_points`. The dedup logic skips writes when:
1. **Idempotent repoll:** the most recent row in `live.match_states` for this match has IDENTICAL (sets_won, current_games, current_point) — the API just hasn't ticked.
2. **Bogus 0-0:** a `(0,0)` point row at a `(sets, games)` state where some non-`(0,0)` row already exists. This is a known TennisAPI1 quirk where the API briefly emits a stale 0-0 mid-game.

A non-skipped row gets a fresh PRIMARY KEY `(match_id, polled_at)`. If the score-state tuple changed since the last row, that's "a new point" — and the `MatchWorker._poll` writes a `POINTS_RECEIVED` event to `audit.poll_audit_log` with an incrementing `points_count` (a worker-local counter, NOT a global-match counter).

**Detecting a game end.** Implicit. When the next stored row's `home_current_games + away_current_games > previous_row's`, a game just ended. The new row will (in a clean transition) carry `home_current_point=0, away_current_point=0` and the games-counter incremented for whichever side won. The logger's `_retro_winner_for_prev_game` looks at the game-counter delta and retroactively populates `point_winner` for the PREVIOUS row — but only if that previous row had `point_winner = NULL`. This recovers point-winner attribution for game-deciding points that were never captured in their own intra-game row.

**Detecting a set end.** Same mechanism, one level up: `home_sets_won` or `away_sets_won` increments while `current_games` resets to 0. The logger handles set-boundary attribution via `_retro_winner_for_prev_game`'s set branch (which reads the completed set's totals from the new row's `home_setN_games`/`away_setN_games` columns).

**Detecting a match end.** `winner_code` becomes non-null. `MatchWorker._poll` checks for this on every poll and stops the worker:
```
if parsed_detail.get("winner_code"):
    self._running = False
    return
```
The collector's next 60-s cycle notices `worker._running == False`, removes it from `self._active`, and writes a `MATCH_ENDED` event to `audit.poll_audit_log`.

**The "game_num bug" workaround.** The TennisAPI1 documentation flags it directly (per `tennis_engine_reference_guide_v4.md` §10): "the game_num field in point-by-point feed doesn't always increment on game end." Two consequences:
- Within `pointByPoint[i].games[]`, consecutive games sometimes share the same `game` index, or the `score.serving` field goes stale.
- Naively trusting the API field corrupts game numbers and breaks the per-game grouping.

The workaround lives in `TennisFeed.translate_to_engine_format` (live path, currently unused in production — only the backfill path uses point-by-point) and `derive_points` in `scripts/backtesting/backfill_today.py`. Both implement the same algorithm: within each set, start `local_game_num = 1`, then increment by 1 every time a point's score is exactly "15-0" or "0-15" (which is unambiguously the first point of a new game). For tiebreaks, the game number is hardcoded to 13. The first point of each set is exempted from incrementing so game 1 isn't double-counted.

This algorithm is the live system's source of truth for game numbering, NOT the API's `game` field. Note: the LIVE production path (`/api/tennis/event/{id}`) does not return point-by-point detail at all — it only returns aggregate score state. The game-num workaround is therefore only relevant for backfill (which calls the point-by-point endpoint).

**Player A / Player B.** Player A = `homeTeam` from the API; Player B = `awayTeam`. This is fixed at worker construction. Server identity is NOT directly captured in `live.match_states` — instead the dashboard derives it from total-games-played parity (`home`-served-game-1 vs `away`-served-game-1, plus the parity rule that server alternates each game). The first server is currently hardcoded to `"home"` in `_enrich_detail_points` (`backend.py:81`) — this is the documented gap: the live endpoint doesn't reveal which side served game 1, so the dashboard guesses, and is wrong roughly 50% of the time when game 1 was served by the away player. Tiebreak server rotation is correctly handled by the dashboard's `inferGameWinner` (which understands tiebreak digits) but server identity in tiebreaks inherits the same parity assumption.

### 3D. Writing to the Database

The live polling path writes to TWO `live.*` tables:

#### `live.match_polls` — raw per-poll snapshots

Every successful poll inserts one row. No dedup, no upsert.

| Column | Type | Meaning |
| --- | --- | --- |
| match_id | INTEGER | from `event.id` |
| player_a | VARCHAR | homeTeam.name |
| player_b | VARCHAR | awayTeam.name |
| polled_at | TIMESTAMPTZ | server-local clock at poll time |
| status | VARCHAR | event.status.type |
| home_sets, away_sets | INTEGER | homeScore.current, awayScore.current |
| home_period1..3, away_period1..3 | INTEGER | per-set games |
| home_current_point, away_current_point | VARCHAR | "0"|"15"|"30"|"40"|"A" or tiebreak digit |
| winner_code | INTEGER | 1, 2, or NULL |
| tournament_name | VARCHAR | tournament.name |
| category | VARCHAR | "atp" / "wta" |

**Insert SQL** (parametrized): `INSERT INTO live.match_polls (...) VALUES (...)` — `MatchLogger._INSERT_MATCH_DETAIL`.

**No primary key, no indexes.** Currently 98,921 rows.

#### `live.match_states` — deduped score-state ledger (the live truth)

Inserted only when the score state actually changed AND the row passes the bogus-0-0 filter.

| Column | Type | Meaning |
| --- | --- | --- |
| match_id | INTEGER NOT NULL | (PK part 1) |
| player_a, player_b | VARCHAR | from worker init |
| polled_at | TIMESTAMPTZ NOT NULL | (PK part 2) |
| status | VARCHAR | inprogress / finished / interrupted / etc. |
| home_sets_won, away_sets_won | INTEGER NOT NULL | sets currently won |
| home_set{1,2,3}_games, away_set{1,2,3}_games | INTEGER | per-set games totals |
| home_current_games, away_current_games | INTEGER NOT NULL | games in the current set |
| home_current_point, away_current_point | VARCHAR NOT NULL | normalized point score |
| point_winner | VARCHAR | "home" or "away" — derived (not from API) |
| winner_code | INTEGER | 1, 2, or NULL |
| tournament_name, category | VARCHAR | passthrough |
| country_a, country_b | VARCHAR | alpha2 country code |

**Primary key:** `(match_id, polled_at)`.
**Index:** `match_states_score_idx (match_id, home_sets_won, away_sets_won, home_current_games, away_current_games, home_current_point, away_current_point)`.

**Insert SQL** (`MatchLogger._UPSERT_MATCH_STATE`):
```sql
INSERT INTO live.match_states (...)
VALUES (...)
ON CONFLICT (match_id, polled_at) DO NOTHING
```
The conflict clause is defensive; with 15-s polling and microsecond timestamps, collisions never happen in practice.

After the insert, an immediate UPDATE may run if the new row begins a new game and the prior row's `point_winner` is NULL, retroactively assigning `point_winner` from the games-count delta.

Currently 24,705 rows across 185 distinct matches over a one-week window.

---

## SECTION 4 — THE BACKFILL PIPELINE

The backfill pipeline is **manual and on-demand only**. There is no scheduler hook, no automatic completion-trigger. A human runs `scripts/backtesting/backfill_today.py` (or `run_full_backfill.py`) interactively and selects a tour, date, and tournament.

### Trigger

Manual: human invokes `.venv/bin/python scripts/backtesting/backfill_today.py` → answers prompts (ATP/WTA → date → tournament number).

### Endpoint

`GET https://tennisapi1.p.rapidapi.com/api/tennis/event/{match_id}/point-by-point` (the same endpoint that `tennis_feed.get_point_by_point` wraps; the backfill script reimplements the HTTP/retry logic locally rather than reusing TennisFeed).

The matches to backfill are first discovered by hitting `GET /api/tennis/events/{day}/{month}/{year}` and filtering to `status.type == "finished"` events for the chosen tournament.

### Fields read from the point-by-point response

The response shape is `{ pointByPoint: [{ set: 1, games: [{ game: 1, score: {serving: 1|2}, points: [{...}] }] }] }`.

| Path | Used for |
| --- | --- |
| `pointByPoint[i].set` | set_num (trusted directly) |
| `pointByPoint[i].games[j].score.serving` | server (1=home, 2=away) |
| `pointByPoint[i].games[j].game` | NOT trusted (see game_num bug); a local counter is derived from score resets |
| `points[k].homePoint`, `awayPoint` | string scores; "A" normalised to "AD" |
| `points[k].homePointType`, `awayPointType` | 1 means that side won the point |
| `points[k].pointDescription` | 1 = ace, 2 = double-fault |

Game numbering is derived locally with the algorithm described in §3C.

### Destination: `live.backfill_points`

| Column | Type |
| --- | --- |
| ts | TIMESTAMPTZ (synthetic — base + 45 s × point index) |
| match_id | INTEGER |
| player_a, player_b | VARCHAR |
| point_num | INTEGER |
| set_num | INTEGER |
| game_num | INTEGER |
| home_point, away_point | VARCHAR |
| server | VARCHAR ("home"/"away") |
| point_winner | VARCHAR |
| is_ace, is_double_fault | BOOLEAN |
| ingestion_source | VARCHAR (always `"backfill"`) |
| tournament_name, category | VARCHAR |

No primary key, no indexes. Currently 25,040 rows.

Timestamps are synthetic — a fictional 45-second-per-point spacing starting from the API's `startTimestamp` for the match. The real-time gap between points is not preserved by backfill.

### Relationship to `live.match_states`

**Backfill data and live data live in completely separate tables.** They are not merged or compared anywhere in the active code:
- `live.backfill_points` has one row per point-played, with set_num/game_num/point_num and a synthetic timestamp. Source: the point-by-point endpoint.
- `live.match_states` has one row per score-state-change, with sets/games/per-set columns and a real polling timestamp. Source: the match-detail endpoint, polled live every 15 s.

The two tables coexist because the live API doesn't expose point-by-point granularity in real time — only aggregate score. So the live system captures what it can in real time (score states), and backfill recovers the missing per-point detail after the match ends. The dashboard reads only `live.match_states` (via the `/match/{id}` route). Backfill is consumed by the offline ML training pipeline (which reads `live_processed.points` — currently stale and not refreshed from `live.backfill_points`).

### Phase 2 verification pipeline — current state

**Built but not running.** The Phase 2 work creates a sequence-validator that walks `live.match_states` rows for one match, checks each transition against the legal tennis-scoring graph, and emits `Gap` records for illegal transitions:

- `src/verification/validator.py` — pure functions, fully implemented and unit-tested by `tests/test_validator.py`. Walks rows, applies a BFS over the regular-game scoring graph, classifies every illegal transition (duplicate_state, set_jump, score_regression, game_jump, score_jump, final_state_mismatch) with severity (low/medium/high), produces a `ValidationSummary`.
- `src/verification/db_setup.py` — DDL helper for `audit.verification_reports` and `audit.live_gap_reports`, idempotent.
- `scripts/setup/migrate_phase2_audit_tables.py` — one-time migration that creates the two tables. Already run; both tables exist.
- Status as of 2026-05-08: **`audit.verification_reports` has 0 rows, `audit.live_gap_reports` has 0 rows.** No code in `scripts/run_collector.py`, `src/live/`, or anywhere else calls `validate_match()` and writes the result. The orchestrator that would (a) decide which matches to validate, (b) load their state rows from `live.match_states`, (c) format the recorded final score from `live.match_polls`, and (d) insert the gaps + summary into the audit tables does not exist.

In short: Phase 2 is shovel-ready — the validator is correct and the tables are waiting — but there is no daily/post-match job that fires it.

---

## SECTION 5 — THE AUDIT LOGGING SYSTEM

Three loggers run in parallel, each with its own table and frequency.

### `ApiLogger` — every HTTP call

**Owns:** `audit.api_call_log` and `audit.api_response_archive`.

**Singleton.** `src/live/api_logger.get_default_logger()` lazy-initialises one shared `ApiLogger` per process (`Phase 3.1` per a comment, dated). Every `TennisFeed` instance in this process — the scheduler's, the collector's, and each MatchWorker's — uses this same logger and therefore the same psycopg2 connection. The reason given in the docstring: "This prevents connection-pool exhaustion on managed databases with low max_connections caps." The construction is double-checked-locked and attempted once per process; if it fails, audit logging is permanently disabled until restart.

**What it logs.** Every call inside `TennisFeed._get` invokes `_log_attempt` after each HTTP attempt (success OR failure). The columns:

| Column | Source |
| --- | --- |
| `id` | BIGSERIAL |
| `timestamp` | NOW() |
| `endpoint` | one of `live_matches`, `events_by_date`, `match_details`, `point_by_point` |
| `request_path` | the path component (e.g. `/api/tennis/event/16139270`) |
| `request_params` | JSONB of the parameter dict passed to the call |
| `match_id` | when present |
| `http_status` | HTTP code, NULL if a network error happened before any response |
| `latency_ms` | wall-clock time |
| `response_summary` | JSONB summary produced by `summarize_*` per endpoint |
| `raw_response_id` | FK into `api_response_archive` (NULL when not archived) |
| `error` | text of any exception |
| `poll_cycle_id` | UUID stamping all calls made in the same logical request cycle |

**Indexes:** match_id, timestamp, endpoint, poll_cycle_id.

**Which endpoints get raw archiving** (`_ARCHIVE_ENDPOINTS = {"live_matches", "match_details"}`): only those two endpoints have their full JSON body inserted into `audit.api_response_archive`. The reasons given in the source comment:
- `events_by_date` is excluded because each response is 2-5 MB (hundreds of events) and is polled every 5 minutes by the dashboard / hourly by the scheduler — archiving them all would balloon the table to no diagnostic benefit.
- `point_by_point` is also not archived (only the backfill scripts call it; metadata in `api_call_log` is enough).

**Note on existing data:** there are 12 `events_by_date` rows in `api_response_archive` from a one-hour window on 2026-05-07 (16:12 → 16:54 UTC). Likely an artifact of an earlier code version; not a current production behavior.

**Counts (2026-05-08):**
- `audit.api_call_log`: 29,770 (match_details: 26,539; events_by_date: 1,692; live_matches: 1,546)
- `audit.api_response_archive`: 28,066 (match_details: 26,515; live_matches: 1,544; events_by_date: 12)

### `PollLogger` — every significant pipeline event

**Owns:** `audit.poll_audit_log`.

**Single instance** in the process, shared across scheduler, collector, and workers (passed in constructor argument). The scheduler also has its own `MatchLogger` for the `live.match_polls`/`live.match_states` writes — they are NOT the same object.

**What it logs:** discrete events with hard-coded `event_type` strings. Observed event types and counts:

| event_type | count | when emitted |
| --- | --- | --- |
| `NO_NEW_POINTS` | 20,333 | every poll where the score state didn't change |
| `POINTS_RECEIVED` | 10,027 | every poll where the score state changed (carries `points_count` = worker-local change counter) |
| `TICK_START` | 709 | every scheduler tick (`_check_schedule`); detail = current state |
| `MATCH_DISCOVERED` | 147 | new match enters the active set (organic) or pre-spawn |
| `MATCH_ENDED` | 88 | a worker finished and the collector cleaned it up |
| `STATE_TRANSITION` | 21 | scheduler crossed states (IDLE→PRE_MATCH, PRE_MATCH→LIVE, etc.); detail = `"PREV->NEW"` |
| `POLL_ERROR` | (varies) | exception in poll/discovery; detail = first 200 chars of error |

**Schema:**
```
id SERIAL PK
timestamp TIMESTAMPTZ default NOW()
event_type VARCHAR(50)
match_id VARCHAR(100)
detail TEXT
points_count INTEGER
triggering_call_id BIGINT          -- never written; always NULL
reason TEXT                         -- never written; always NULL
metadata JSONB                      -- never written; always NULL
poll_cycle_id UUID                  -- correlates with api_call_log
```

Indexes on match_id, timestamp, poll_cycle_id.

**Failure mode:** `PollLogger.log()` swallows every exception and rolls back; the comment says "a logging hiccup must never crash the scheduler or worker that called us." This is intentional; cost is silent data loss.

### `MatchLogger` — the live data writers

**Owns:** all of `live.*` plus the audit-table DDL (it's the file where every CREATE TABLE for the live schemas lives).

**One instance per worker** (each `MatchWorker._logger = MatchLogger()` in the worker's `__init__`) plus one in the scheduler (`scheduler_logger`). Each has its own psycopg2 connection. With N active workers, there are N+1 PostgreSQL connections from MatchLogger alone — this is the "MatchLogger/PollLogger idle-in-transaction issue" referred to in the prompt: each MatchWorker holds a long-lived connection that is mostly idle (a poll happens once every 15 s, and the connection sits idle in between). On the managed database this can drift toward `idle in transaction` if any code path forgets to commit or rollback. The MatchLogger code does explicitly commit/rollback inside each public method's try/finally, so the risk is bounded; but the architecture of one connection per worker is the underlying concern.

**Public methods:**
- `log_raw_odds(match_id, player_a, player_b, odds_result)` — writes `live.backfill_odds_polls`. Currently unused by the live path (no caller).
- `log_match_detail(parsed_detail, polled_at)` — inserts one row into `live.match_polls`.
- `upsert_match_detail_points(parsed_detail, polled_at)` — the heavy one. Filters bogus 0-0, dedups idempotent repolls, computes `point_winner`, inserts to `live.match_states`, optionally backfills the previous row's point_winner.

### `poll_cycle_id` flow

A UUID is generated at each "logical request boundary" and threaded through:
- `MatchScheduler._check_schedule` generates `cycle_id = uuid.uuid4()` per tick. The cycle_id is passed to `feed.get_upcoming_matches_raw` AND `feed.get_live_matches_raw`, so all the per-day events_by_date calls plus the live_matches call in one tick share a cycle_id. Same UUID also goes on the `TICK_START` and any subsequent `STATE_TRANSITION` poll-audit rows.
- `MatchCollector._cycle` generates its own cycle_id per discovery cycle. Same UUID stamps the `MATCH_DISCOVERED` / `MATCH_ENDED` / `POLL_ERROR` poll-audit rows from that cycle and the `live_matches` API call.
- `MatchWorker._poll` generates a cycle_id per poll. Same UUID stamps the `match_details` API call and the `POINTS_RECEIVED`/`NO_NEW_POINTS`/`POLL_ERROR` poll-audit rows.

So `poll_cycle_id` is not "one per match" — it's "one per polling-loop iteration." Joining `audit.api_call_log` to `audit.poll_audit_log` on `poll_cycle_id` reconstructs the full trace of a single poll: which API endpoint(s) were hit, with what parameters, what the latency was, what events the poll triggered.

### Why singleton ApiLogger but not singleton MatchLogger?

The singleton was added (per the comment, "Phase 3.1") specifically to solve PostgreSQL connection-pool exhaustion. It centralises ALL TennisAPI1 audit writes through one shared connection. MatchLogger has not been made singleton — each worker holds its own — but it could be, and that's a likely future fix for the idle-in-transaction concern.

---

## SECTION 6 — THE DATABASE SCHEMA — COMPLETE

The PostgreSQL instance is hosted on Digital Ocean (SSL-required). Seven schemas are present.

### Schema `live` — active operational data

#### `live.match_polls` — 98,921 rows

| Column | Type | Notes |
| --- | --- | --- |
| match_id | INTEGER | |
| player_a | VARCHAR | |
| player_b | VARCHAR | |
| polled_at | TIMESTAMPTZ | |
| status | VARCHAR | |
| home_sets, away_sets | INTEGER | sets currently won |
| home_period1..3, away_period1..3 | INTEGER | per-set games |
| home_current_point, away_current_point | VARCHAR | |
| winner_code | INTEGER | |
| tournament_name | VARCHAR | |
| category | VARCHAR | |

**Writers:** `MatchLogger.log_match_detail` (per poll, in `MatchWorker._poll`).
**Readers:** none in active code. The dashboard reads `live.match_states`, not this table.
**Indexes:** none.
**Status:** functioning as a raw-poll archive; row count grows ~20× faster than `match_states` because it isn't deduped.

#### `live.match_states` — 24,704 rows, 185 distinct match_ids

The live truth.

| Column | Type | Notes |
| --- | --- | --- |
| match_id | INTEGER NOT NULL | PK 1/2 |
| polled_at | TIMESTAMPTZ NOT NULL | PK 2/2 |
| player_a, player_b | VARCHAR | |
| status | VARCHAR | inprogress / finished / interrupted / etc. |
| home_sets_won, away_sets_won | INTEGER NOT NULL | |
| home_set1_games, away_set1_games | INTEGER | |
| home_set2_games, away_set2_games | INTEGER | |
| home_set3_games, away_set3_games | INTEGER | |
| home_current_games, away_current_games | INTEGER NOT NULL | |
| home_current_point, away_current_point | VARCHAR NOT NULL | |
| point_winner | VARCHAR | derived ("home"/"away"/NULL) |
| winner_code | INTEGER | |
| tournament_name, category | VARCHAR | |
| country_a, country_b | VARCHAR | alpha2 |

**Writers:** `MatchLogger.upsert_match_detail_points` only.
**Readers:** `backend.py` — `/matches`, `/live_matches`, `/match/{match_id}`. `validator.py` (offline tests).
**Indexes:** `match_states_pkey (match_id, polled_at)` + `match_states_score_idx (match_id, home_sets_won, away_sets_won, home_current_games, away_current_games, home_current_point, away_current_point)`.

#### `live.backfill_points` — 25,040 rows

| Column | Type |
| --- | --- |
| ts, match_id, player_a, player_b | TIMESTAMPTZ, INTEGER, VARCHAR, VARCHAR |
| point_num, set_num, game_num | INTEGER |
| home_point, away_point, server, point_winner | VARCHAR |
| is_ace, is_double_fault | BOOLEAN |
| ingestion_source, tournament_name, category | VARCHAR |

**Writers:** `scripts/backtesting/backfill_today.py` (and via `run_full_backfill.py`).
**Readers:** `backfill_tournament_odds.py` (to compute the odds-sweep window from real point timestamps); offline ML pipeline (via `live_processed.points` lineage; not actively refreshed today).
**Indexes:** none.

#### `live.backfill_odds_polls` — 561 rows

| Column | Type |
| --- | --- |
| ts | TIMESTAMPTZ |
| match_id | INTEGER |
| player_a, player_b | VARCHAR |
| bookmaker_prob_a | FLOAT |
| num_bookmakers | INTEGER |
| api_credits_remaining | INTEGER |

**Writers:** `backfill_tournament_odds.py` only.
**Readers:** none currently active.
**Indexes:** none.

### Schema `audit` — the audit log

#### `audit.api_call_log` — 29,770 rows

| Column | Type | Notes |
| --- | --- | --- |
| id | BIGSERIAL | PK |
| timestamp | TIMESTAMPTZ | NOW() default |
| endpoint | TEXT | one of `live_matches`, `events_by_date`, `match_details`, `point_by_point` |
| request_path | TEXT | |
| request_params | JSONB | |
| match_id | VARCHAR(100) | |
| http_status | INT | |
| latency_ms | INT | |
| response_summary | JSONB | per-endpoint summarizer output |
| raw_response_id | BIGINT | FK into api_response_archive (when archived) |
| error | TEXT | |
| poll_cycle_id | UUID | |

**Writers:** `ApiLogger.log_call` (the singleton; called from `TennisFeed._log_attempt`).
**Readers:** `scripts/dev/audit_match.py` for diagnostic timelines.
**Indexes:** id (PK), match_id, timestamp, endpoint, poll_cycle_id.

#### `audit.api_response_archive` — 28,066 rows

| Column | Type |
| --- | --- |
| id | BIGSERIAL PK |
| timestamp | TIMESTAMPTZ NOT NULL |
| endpoint | TEXT |
| match_id | VARCHAR(100) |
| raw_json | JSONB NOT NULL |
| byte_size | INT |

**Writers:** `ApiLogger.log_call` (only when endpoint ∈ `{live_matches, match_details}`).
**Readers:** none in active code (manual debugging only).
**Indexes:** id (PK), match_id, timestamp.

#### `audit.poll_audit_log` — 31,322 rows

| Column | Type |
| --- | --- |
| id | SERIAL PK |
| timestamp | TIMESTAMPTZ NOT NULL DEFAULT NOW() |
| event_type | VARCHAR(50) |
| match_id | VARCHAR(100) |
| detail | TEXT |
| points_count | INTEGER |
| triggering_call_id | BIGINT — always NULL |
| reason | TEXT — always NULL |
| metadata | JSONB — always NULL |
| poll_cycle_id | UUID |

**Writers:** `PollLogger.log` only.
**Readers:** `scripts/dev/audit_match.py`.
**Indexes:** id (PK), match_id, timestamp, poll_cycle_id.

#### `audit.verification_reports` — 0 rows

| Column | Type |
| --- | --- |
| id | BIGSERIAL PK |
| verification_run_id | UUID NOT NULL |
| run_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() |
| match_id | VARCHAR(100) NOT NULL |
| live_point_count | INT NOT NULL |
| inferred_missing_points | INT NOT NULL DEFAULT 0 |
| live_final_score | TEXT |
| recorded_final_score | TEXT |
| final_score_match | BOOLEAN NOT NULL |
| total_sets | INT |
| clean_set_count, gapped_set_count, gap_count | INT NOT NULL |
| severity_max | TEXT |
| verdict | TEXT |
| set_breakdown | JSONB |
| notes | JSONB |

**Writers:** intended `validator.py` orchestrator — not yet built.
**Readers:** none yet.
**Indexes:** id (PK), match_id, verification_run_id, run_at, verdict.

#### `audit.live_gap_reports` — 0 rows

| Column | Type |
| --- | --- |
| id | BIGSERIAL PK |
| verification_run_id | UUID NOT NULL |
| match_id | VARCHAR(100) NOT NULL |
| gap_type | TEXT NOT NULL |
| severity | TEXT |
| description | TEXT |
| before_state, after_state | JSONB |
| inferred_skipped_points | INT |
| set_number, game_number | INT |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() |

**Writers:** none yet (intended validator orchestrator).
**Indexes:** id (PK), match_id, verification_run_id, gap_type, severity.

### Schema `book` — placeholder

Schema exists (created by `MatchLogger.__init__` via `CREATE SCHEMA IF NOT EXISTS book`). **Zero tables.** Reserved for bookmaker odds data per the project's intended architecture, but no current code creates anything in it.

### Schema `core` — historical training data

| Table | Rows | Purpose |
| --- | --- | --- |
| `core.atp_matches` | 194,996 | Sackmann ATP dataset |
| `core.wta_matches` | 158,092 | Sackmann WTA dataset |

**Active readers in production code:** `src/live/player_lookup.py` (which is itself not on the live path today). So the `core` schema is read by offline ML scripts and the (orphaned) player_lookup module.

### Schema `rankings` — historical player metadata

| Table | Rows | Purpose |
| --- | --- | --- |
| `rankings.atp_players` | 65,989 | Player IDs, names, hand, dob, IOC, height, wikidata IDs |

**Active readers in production code:** none. Used by `setup_rankings_and_schemas.py` and offline ML training only.

### Schema `live_raw` — Phase 1 legacy (not in active code path)

| Table | Rows | Phase 1 plan | Used today? |
| --- | --- | --- | --- |
| `live_raw.match_details` | 1,263 | RENAME+MOVE → live.match_polls | NO |
| `live_raw.tennisapi_points` | 1,969 | RENAME+MOVE → live.backfill_points | NO |
| `live_raw.oddsapi_polls` | 0 | RENAME+MOVE → live.backfill_odds_polls | NO |

The audit at `data/audits/schema_audit_2026-05-07.md` confirms code references to these tables exist only in pytest cache files, the worktree subdirectory, and tests — no production runtime reference. The Phase 1 migration is described as "renaming" but in fact creates new tables in `live.*` and copies data; the originals remain in place (with stale row counts). Safe to drop.

### Schema `live_processed` — Phase 1 legacy

| Table | Rows | Phase 1 plan | Used today? |
| --- | --- | --- | --- |
| `live_processed.match_detail_points` | 485 | RENAME+MOVE → live.match_states | NO |
| `live_processed.dashboard_log` | 1,969 | DROP (orphan) | NO |
| `live_processed.points` | 1,123 | DROP (orphan) | NO |

Same status — safe to drop after confirmation.

---

## SECTION 7 — THE FASTAPI BACKEND AND DASHBOARD

### 7A. Backend routes (`src/live/backend.py`)

The FastAPI app `app` is a singleton declared at module top; `scripts/run_collector.py` mounts it under uvicorn on port 8000.

| Method + path | Function | What it does | Tables hit |
| --- | --- | --- | --- |
| GET `/` | `root` | Returns an HTML meta-refresh redirect to `/dashboard`. | — |
| GET `/dashboard` | `dashboard` | Serves `src/dashboard/index.html` as a `FileResponse`. | — |
| GET `/matches` | `list_matches` | Returns the latest score state (one row per match) from `live.match_states`, with `set_scores_a`/`set_scores_b` arrays trimmed to played-only sets. Used by the dashboard's "Completed" and full-history view. | `live.match_states` |
| GET `/live_matches` | `list_live_matches` | Returns the latest score state for matches whose `match_id` is in the in-memory `ACTIVE_MATCH_IDS` set AND whose latest status is `'inprogress'`. Falls back to in-memory `COUNTRY_MAP` when DB country columns are still null. | `live.match_states` |
| GET `/match/{match_id}` | `get_match` | Returns ALL `live.match_states` rows for one match, ordered by sets/games/points/polled_at, then enriched into per-point dicts (set_num, game_num, point_num, server derived from parity). | `live.match_states` |
| GET `/upcoming_matches` | `upcoming_matches` | Calls `TennisFeed.get_upcoming_matches(days_ahead=1)` — fetches today + tomorrow's `events_by_date` and filters to ATP/WTA singles with a startTimestamp. | TennisAPI1 (NOT the DB) |

Notes:
- The backend uses a fresh `psycopg2.connect()` per request (`_conn()` factory + `closing(...)` context manager) rather than a connection pool. With the dashboard's polling pattern (5 fetches every 15 s × 1 dashboard tab = ~20 connection-opens/min), this is fine.
- All SQL is executed via `_safe_query` which catches `psycopg2.ProgrammingError` (e.g. table missing on a fresh DB) and rolls back, returning `[]`.

### Country flag rendering

There is **no backend "flags endpoint."** Country codes live as alpha-2 strings (e.g. `"US"`, `"IT"`) in `live.match_states.country_a` / `country_b` and in the `/upcoming_matches` API response (from `homeTeam.country.alpha2`). The dashboard JavaScript renders them as Unicode regional indicator emoji client-side:

```js
const countryFlag = (alpha2) => {
  if (!alpha2) return '';
  return alpha2.toUpperCase().split('').map(c =>
    String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65)
  ).join('');
};
```

`"US"` → `🇺🇸`. No external image, no external service.

### Player name resolution

The dashboard uses player names exactly as the API returns them — no DB lookup, no normalisation. Names come from `homeTeam.name` and `awayTeam.name` and are written as-is to `player_a`/`player_b` columns and rendered as-is on the dashboard. The offline `src/live/player_lookup.py` module does fuzzy matching against the Sackmann historical data, but that module is not on the live path.

### 7B. Dashboard frontend (`src/dashboard/index.html`)

A single self-contained HTML file. ~1800 lines. Loads React 18, ReactDOM, Recharts, and Babel-standalone from CDN. JSX is compiled in-browser; there is no build step.

**Polling cadence (from React `useEffect`):**
- `fetch('/live_matches')` every 15,000 ms
- `fetch('/upcoming_matches')` every 5 × 60 × 1000 = 300,000 ms (5 minutes)
- `fetch('/matches')` once on mount
- `fetch('/match/{match_id}')` on the match-detail tab: every 15,000 ms while the match is live; suspended while scrubbing or when the match is final.

**Walk-through: what a user sees for a live match.**

1. The user opens `/dashboard`. React mounts `<App>`, which renders `<MatchesTab>`.
2. `<MatchesTab>` fires three fetches in parallel: `/live_matches`, `/upcoming_matches`, `/matches`. The first two re-fire on a timer.
3. The component computes three lists from the merged data: `liveItems`, `upcomingItems`, `completedItems`. The `live` set is the source of truth for which match_ids are currently in progress; `liveById` is built from it and used to suppress duplicates from `/matches` and `/upcoming_matches`.
4. Each match is rendered as a `<MatchCard>` inside a tour-grouped `<TourSubsection>` (ATP / WTA columns). The card shows:
   - Country flags (rendered client-side from `country_a` / `country_b`),
   - Player names (`player_a` / `player_b`),
   - Per-set scores from `set_scores_a` / `set_scores_b` arrays (built by the backend from the latest `home_setN_games`/`away_setN_games` columns),
   - For LIVE: current-set games and current point (`home_current_games`, `home_current_point` from the latest `live.match_states` row),
   - "▶" serving indicator on the player's row when `match.server === 'home'` or `=== 'away'` — but this field is not populated by the backend's `/live_matches` endpoint (it's `NULL` in the response), so the indicator is currently always off in the live list,
   - "Live · {ordinal} Set" header for live matches; "Final" / "Walkover" / scheduled-time for others.
5. On click, `<App>` switches to `<MatchDetail>`. That component fetches `/match/{match_id}` and re-fetches every 15 s. The response is per-point rows, with set_num/game_num/point_num assigned by the backend's `_enrich_detail_points` and SERVER assigned via game-parity (assuming home served game 1).
6. The detail view renders:
   - `<HeroCard>` with the current scoreboard plus three placeholders ("Model", "Book", "Edge") — all show "—" because no model is wired in.
   - `<MomentumBar>` driven by `latest.d_a` / `latest.d_b` — also always "—" today.
   - `<ProbChart>` (Recharts) — same; shows "Model coming soon" overlay.
   - `<SignalsCard>` displaying NMI/SMS/RMS/PMS/GPS — same.
   - `<PointByPoint>` — fully functional, reads only score data.
   - `<MatchStatsCard>` — derives stats from `point_winner` and `server` columns: points won, won-on-serve, won-on-return, service-game holds, break points won/saved, current serve streak.
7. The user can scrub through points using a range input — when scrubbing, polling pauses and the visible point list is sliced to `[0..scrubIdx]`.

### 7C. Upcoming matches

**Yes**, there's an Upcoming section on the matches landing page. Powered by `GET /upcoming_matches`.

**Backend behaviour.** `_get_feed().get_upcoming_matches(days_ahead=1)` constructs a TennisFeed singleton on first call (lazy because RAPIDAPI_KEY may not be set at import time), then iterates `today` and `today + 1 day`, calling `GET /api/tennis/events/{day}/{month}/{year}` for each. It filters to ATP/WTA singles and returns:

```
{
  match_id, player_a, player_b,
  country_a, country_b,
  tournament,
  scheduled_start_unix,
  tour: "ATP" | "WTA"
}
```

**Polling interval.** Every 5 minutes from the dashboard.
**Rate-limit consideration.** Each call costs 2 API requests (today + tomorrow). At one dashboard tab, that's roughly 24 events_by_date calls/hour. At several dashboard tabs, this multiplies — there's no caching layer on the backend side. The audit log shows 1,692 events_by_date calls over the active window (about a week), consistent with this.

**Field rendering.** The card shows: tournament, country flags, player names, scheduled local time computed client-side from `scheduled_start_unix`.

---

## SECTION 8 — ODDS DATA

**Status: NOT active in the live pipeline.**

The Odds API integration exists (`src/live/odds_fetcher.py`) and was clearly intended for live event-triggered odds polling — the scheduler module's docstring explicitly mentions "event-trigger odds fetches (rate-gated)" — but there is no caller for it in the running pipeline today. No scheduler job, no MatchWorker hook, no FastAPI route invokes `get_match_odds` or `get_bookmaker_prob`.

**The live `MatchLogger.log_raw_odds` method exists**, owns the DDL for `live.backfill_odds_polls`, but is not called anywhere in `src/live/`. A grep across the codebase confirms it.

**What the offline path does:**

1. `scripts/backtesting/backfill_tournament_odds.py` is the ONLY caller of `odds_fetcher` machinery in production code paths. It uses the historical-snapshot endpoint of The Odds API (not the live odds endpoint that `get_bookmaker_prob` targets) to backfill `live.backfill_odds_polls` for a chosen tournament-day.
2. The script imports three private helpers from `odds_fetcher.py`: `TOURNAMENT_MAP` (map of TennisAPI1 `uniqueTournament.id` → Odds API `sport_key`), `_TENNIS_SPORT_KEYS` (ordered list for fall-through scanning), and `_compute_consensus` (the de-overrounding logic that turns h2h decimal odds from N bookmakers into a single consensus probability).

**`live.backfill_odds_polls` schema:**

| Column | Type |
| --- | --- |
| ts | TIMESTAMPTZ |
| match_id | INTEGER |
| player_a, player_b | VARCHAR |
| bookmaker_prob_a | FLOAT |
| num_bookmakers | INTEGER |
| api_credits_remaining | INTEGER |

Currently 561 rows. Last write was 2026-04-30 (per the schema audit). No active production writes.

**Triggers.** None in the live path. The interactive backfill script is the only writer and it only fires when a human runs it.

**Dashboard.** Odds are not displayed anywhere on the dashboard. The "Book" placeholder in the hero card / win-probability chart is hardcoded to render `bookmaker_prob_a` from each row, but the underlying field is always NULL because no live row carries it.

---

## SECTION 9 — EXAMPLE MATCH WALKTHROUGH

**Match selected:** Emma Navarro (US) vs. Elisabetta Cocciaretto (IT), match_id `16139270`. WTA, completed 2026-05-08 with `winner_code = 2` (Cocciaretto won 6-3, 6-3 — see final-row data below).

### Discovery

- Worker first wrote to `live.match_polls` at **2026-05-08 17:05:21 UTC**.
- The `audit.poll_audit_log` row for `MATCH_DISCOVERED` for this match_id confirms one discovery event. Whether the discovery was organic (live-matches endpoint flipped status to inprogress) or pre-spawn (within 5 minutes of scheduled start) is recorded in `detail` — examination of the full row is needed to disambiguate, but pre-spawn is most likely given the early discovery time.

### Coverage

| Source table | Row count for this match |
| --- | --- |
| `live.match_polls` (raw) | 757 polls (one per ~15 s) |
| `live.match_states` (deduped) | 122 unique score states |
| `audit.api_call_log` (match_details endpoint) | 758 calls |
| `audit.poll_audit_log` POINTS_RECEIVED | 122 events |
| `audit.poll_audit_log` NO_NEW_POINTS | 238 events |
| `audit.poll_audit_log` MATCH_DISCOVERED | 1 |
| `audit.poll_audit_log` MATCH_ENDED | 1 |
| `live.backfill_points` | 0 (backfill not yet run for this match) |

Total polls: 757. Of those, 122 saw a score change (the value of the "POINTS_RECEIVED" counter aligns exactly with the 122 deduped match_states rows), 238 saw no change. The remaining ≈397 polls during the early part of the match presumably carried `inprogress` with stale 0-0 — these are the rows the bogus-0-0 filter rejects without writing anything.

The match_polls / match_details API call gap of 1 (757 rows vs 758 calls) is the post-final-state poll right when the API status flipped to "finished" — the worker writes one final `live.match_polls` row carrying `winner_code=2` and immediately stops. The discrepancy is exactly explained by the worker's poll-then-check-winner-code flow.

Span: 17:05:21 UTC → 20:16:49 UTC ≈ 3 hours 11 minutes. Average polls/minute: 757 / 191 ≈ 4 polls/min, matching the 15-s cadence (4 polls/min = 1 poll every 15 s).

### Final score (from the last `live.match_states` row)

| Field | Value |
| --- | --- |
| polled_at | 2026-05-08 20:16:49 UTC |
| status | finished |
| home_sets_won | 0 |
| away_sets_won | 2 |
| home_set1_games | 3 |
| away_set1_games | 6 |
| home_set2_games | 3 |
| away_set2_games | 6 |
| home_current_games | 0 |
| away_current_games | 0 |
| home_current_point | 40 |
| away_current_point | A |
| winner_code | 2 |

So the final score was 6-3, 6-3 to Cocciaretto. The "trailing point" (40-A on Cocciaretto's serve) is the last live point captured, frozen into the post-match snapshot.

### Coverage gaps detectable from data

The tail end shows the captured set-1 finish. From rows 60–65 (in the middle of set 1):
```
19:33:18  sets 0-1, set1 games 3-6, current 0-0, point 40-40 (back at deuce)
19:33:48  ...           current 0-0, point 40-A  (Cocciaretto AD)
19:34:33  ...           current 0-0, point 40-40 (back to deuce)
19:35:04  ...           current 0-0, point 40-A
19:35:19  set1 games 3-6, NEW SET begins, set2 games 0-1, away won
```

Set 1 ended (Cocciaretto won the deuce-fest) and set 2 immediately incremented to 0-1. The gap between 19:35:04 (40-A in pre-set-end deuce) and 19:35:19 (start of set 2 with set2 games 0-1) is 15 s — one poll. The actual point that ended set 1 was therefore not captured as a separate score-state row; the logger's `_retro_winner_for_prev_game` retroactively assigned `point_winner='away'` to the 19:35:04 row when the 19:35:19 row arrived.

### Sample of 8 actual rows (`live.match_states` for match_id 16139270)

The first 5 captured points and the last 3 are below. Format: `polled_at | sets_a-sets_b | per-set games (s1, s2) | current games | current point | point_winner | status | winner_code`.

| polled_at | sets | s1 a-b | s2 a-b | curr g | curr pt | winner | status | wc |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 18:45:58 | 0-0 | 0-0 | 0-0 | 0-0 | 15-0 | NULL | inprogress | NULL |
| 18:46:29 | 0-0 | 0-0 | 0-0 | 0-0 | 15-15 | away | inprogress | NULL |
| 18:47:14 | 0-0 | 0-0 | 0-0 | 0-0 | 30-15 | home | inprogress | NULL |
| 18:47:29 | 0-0 | 0-0 | 0-0 | 0-0 | 40-15 | home | inprogress | NULL |
| 18:48:15 | 0-0 | 0-0 | 0-0 | 0-0 | 40-30 | away | inprogress | NULL |
| 18:48:45 | 0-0 | 1-0 | 0-0 | 1-0 | 0-0   | home | inprogress | NULL |
| ... 116 rows omitted ... | | | | | | | | |
| 20:15:33 | 0-1 | 3-6 | 3-5 | 3-5 | 40-40 | away | inprogress | NULL |
| 20:16:18 | 0-1 | 3-6 | 3-5 | 3-5 | 40-A  | away | inprogress | NULL |
| 20:16:49 | 0-2 | 3-6 | 3-6 | 0-0 | 40-A  | away | finished   | 2  |

The first row (18:45:58) shows a 15-0 score with `point_winner = NULL` — that's because there's no prior row to compute the delta from. The next row (18:46:29) shows 15-15, and `point_winner = "away"` was derived from comparing (15,0) → (15,15). Row at 18:48:45 is the game-1 boundary: home_current_games incremented from 0 to 1, current_point reset to 0-0, and `point_winner = "home"` was retroactively assigned via the games-count delta.

The very last row carries `winner_code = 2` (away won) and `status = finished`. The dashboard's "complete marker" branch keys off this row.

---

## SECTION 10 — WHAT IS NOT BEING USED

### Files not on the production path

- `src/dashboard/index.backup.html` — older dashboard copy.
- `src/engine/` (entire subtree) — archetype/markov/temporal/phat/signals. Not imported by `src/live/*` or the FastAPI app.
- `src/baseline.py`, `src/data_loader.py`, `src/evaluation.py`, `src/model_training.py`, `src/signal_engine.py` — offline ML pipeline.
- `src/pipeline/audit_points.py`, `src/pipeline/data_pipeline.py` — Sackmann CSV loaders + offline diagnostics.
- `src/backtesting/backtester.py`, `src/backtesting/parameter_tuning.py` — offline ML.
- `src/live/odds_fetcher.py` — defined and imported by `backfill_tournament_odds.py` but never reached from `run_collector.py`.
- `src/live/player_lookup.py` — only the offline engine subtree imports it.
- `src/verification/validator.py`, `db_setup.py` — no orchestrator yet.
- `scripts/dev/*` (every file in there) — manual dev tools.
- `scripts/setup/*` — one-time migrations / setup; not on the runtime path.
- `scripts/backtesting/*` — manual backfill / backtest; not driven by production scheduler.

### Database tables not on the production path

| Table | Rows | Status |
| --- | --- | --- |
| `live_raw.match_details` | 1,263 | Pre-Phase 1 archive; nothing reads. |
| `live_raw.tennisapi_points` | 1,969 | Pre-Phase 1 archive; nothing reads. |
| `live_raw.oddsapi_polls` | 0 | Pre-Phase 1 placeholder; nothing reads. |
| `live_processed.match_detail_points` | 485 | Pre-Phase 1 archive; nothing reads. |
| `live_processed.dashboard_log` | 1,969 | Pre-Phase 1 ML log; nothing reads or writes. |
| `live_processed.points` | 1,123 | Pre-Phase 1 ML processed table; nothing reads or writes. |
| `book.*` | (no tables) | Empty schema reserved for future bookmaker data. |
| `live.backfill_odds_polls` | 561 | Written only by the offline backfill script; no live reader. |
| `audit.verification_reports` | 0 | Phase 2 table; orchestrator not built. |
| `audit.live_gap_reports` | 0 | Phase 2 table; orchestrator not built. |
| `rankings.atp_players` | 65,989 | Read only by offline ML; not on live path. |
| `core.atp_matches` | 194,996 | Read only by offline ML and orphaned `player_lookup.py`. |
| `core.wta_matches` | 158,092 | Same. |

### Backend routes never called by the dashboard

All five active routes (`/`, `/dashboard`, `/matches`, `/live_matches`, `/match/{id}`, `/upcoming_matches`) are called by the dashboard. There are no orphaned routes.

### Dead code blocks within active files

- `src/live/logger.py:log_raw_odds` — public method, no caller in production code. (Used by tests only.)
- `MatchLogger`'s setup runs `CREATE SCHEMA IF NOT EXISTS book` and `CREATE TABLE IF NOT EXISTS live.backfill_odds_polls`, but the `book` schema is never written and `backfill_odds_polls` is only written by the offline backfill script.
- `TennisFeed.get_live_matches` (the parsed-summary version, distinct from `get_live_matches_raw`) — unused by production code (the scheduler and collector both use `get_live_matches_raw`). Tests still reference it.
- `TennisFeed.translate_to_engine_format` — currently invoked nowhere in production. The point-by-point parser is used only by the backfill script's `derive_points` (which is a separate near-duplicate). If the live engine were to consume point-by-point data, this method would be the entry point.
- `TennisFeed.get_point_by_point` — defined; never called by production. (Backfill script reimplements the HTTP rather than calling it.)
- `ApiLogger._reset_default_logger_for_testing` — explicitly testing-only.
- The `metadata`, `reason`, and `triggering_call_id` columns of `audit.poll_audit_log` are written by no code path; the schema accommodates them but every row has them as NULL.

---

## SECTION 11 — KNOWN ISSUES AND TECHNICAL DEBT

### 1. The TennisAPI1 `game_num` bug and its workaround

**The bug:** TennisAPI1's `pointByPoint[i].games[j].game` field doesn't reliably increment when a new game starts. Two same-game-number rows can appear back to back, or the `score.serving` field can lag the actual server change. The reference guide (§10) calls this out explicitly, and the source comments in `tennis_feed.py` (around line 376) document the same. This affects only the point-by-point endpoint, which is consumed only by the offline backfill path today.

**The workaround:** within each set, count `local_game_num = 1` and increment ONLY when a point's score is exactly "15-0" or "0-15" — that's the unambiguous signature of the first point of a new game. Tiebreak games are hardcoded to game 13. The first point of a set is exempted from the increment. This logic lives in:
- `src/live/tennis_feed.py:translate_to_engine_format` (live-side, currently unused)
- `scripts/backtesting/backfill_today.py:derive_points` (active during manual backfill)

The two implementations are duplicates of each other.

### 2. The pg_dump version mismatch

This was not directly visible in code I read, but the project is hosted on PostgreSQL 17 (Digital Ocean managed) and the local dev environment likely has an older `pg_dump` client. The standard pg_dump compatibility rule is that the client version must be >= the server's; otherwise you get errors like `aborting because of server version mismatch` when trying to dump. The fix is to install a matching `postgresql-client` package on the workstation. No active code depends on pg_dump, but anyone running database backups locally will hit this.

### 3. The MatchLogger / PollLogger idle-in-transaction risk

Each `MatchWorker` constructs its own `MatchLogger`, which holds its own psycopg2 connection. With N active workers there are N MatchLogger connections plus 1 in the scheduler's logger plus 1 in the singleton ApiLogger plus 1 per HTTP request to the FastAPI backend (each connection is opened-and-closed per request). The MatchLogger code does explicitly commit/rollback each cursor inside its public methods, so the mechanism for becoming idle-in-transaction is more about cleanup-on-error paths than the happy path. PollLogger and ApiLogger have similar shapes but use shared singletons. The deeper issue is that on a managed PostgreSQL with low `max_connections` (Digital Ocean Basic plans cap it around 22-100), this connection-per-worker pattern can exhaust the pool when many matches are live concurrently.

The `Phase 3.1` singleton refactor of `ApiLogger` was a partial fix; doing the same for `MatchLogger` is the obvious next step but hasn't been done (each MatchWorker still holds its own).

### 4. The `triggering_call_id` linkage that is always NULL

`audit.poll_audit_log.triggering_call_id BIGINT` is declared in the schema and was clearly intended to FK into `audit.api_call_log.id` — i.e. for each `POINTS_RECEIVED` event you'd link to the specific API call that observed the points. This would let you trace from "the score changed at this moment" to "here's the exact API response that informed us." The column is never populated. Every row has `triggering_call_id IS NULL` (confirmed: `SELECT COUNT(*) FROM audit.poll_audit_log WHERE triggering_call_id IS NOT NULL` returns 0 across 31,322 rows).

The same row also declares `reason TEXT` and `metadata JSONB`, neither populated.

The reason is structural: `PollLogger.log()` doesn't accept a call_id parameter, and the call sites (`MatchWorker._poll`, `MatchScheduler._check_schedule`) don't have access to the api_call_log row's `id` (which is generated server-side by the BIGSERIAL on insert). To fix this, ApiLogger.log_call already returns the inserted id; PollLogger.log() would need an extra parameter; the call-site would have to thread it through. The work isn't large, but it hasn't been done.

The poll_cycle_id provides a partial substitute — you can join `audit.api_call_log` to `audit.poll_audit_log` ON `poll_cycle_id`, but a single cycle can produce multiple events (e.g. one TICK_START and three MATCH_DISCOVERED in the same cycle), so you can't always pinpoint which call triggered which event.

### 5. Phase 1 legacy schemas still containing stale data

`live_raw` and `live_processed` schemas hold pre-migration data totaling about 6,800 rows. Per the schema audit at `data/audits/schema_audit_2026-05-07.md`, all are slated for cleanup (RENAME+MOVE for some; DROP for `live_processed.dashboard_log` and `live_processed.points`, which are orphans). The migration script `scripts/setup/migrate_to_v2_schemas.py` exists but the cleanup step (deleting the old tables once data is migrated) hasn't been run. Until it is, two near-duplicate "where do live points live" schemas coexist.

### 6. The dashboard server-indicator is wrong half the time

`src/live/backend.py:_enrich_detail_points` assigns server identity using `total_games_played` parity with a hardcoded `first_server="home"`. Tennis matches don't have a fixed first-server convention — it's decided by coin toss. So matches where the away player served game 1 will display the wrong server indicator on every point. The TennisAPI1 live `event/{id}` endpoint does not return a server field at all — only `homeScore.point` and `awayScore.point` — so the backend has no source of truth for first-server identity in production today. The `point-by-point` endpoint does carry `score.serving`, but that endpoint is offline-only (backfill).

### 7. Phase 2 verification orchestrator is missing

Documented in §4. The validator is built and tested, the audit tables are created, but no daily/post-match job calls `validate_match()`. Both `audit.verification_reports` and `audit.live_gap_reports` have zero rows. This is more "deferred work" than "bug" — but it's the highest-value next deliverable, since real-time gap detection is the natural next layer on top of an already-running tracker.

### 8. The MatchLogger singleton-connection-per-worker pattern allows N+ duplicate DDL runs at startup

`MatchLogger.__init__` runs the full `_SETUP_STMTS` list (every CREATE TABLE / CREATE INDEX) on every construction. With one logger in the scheduler plus N workers, those `IF NOT EXISTS` statements fire N+1 times at startup (and per worker spawn during a discovery cycle). Idempotent, but wasteful.

### 9. `events_by_date` is excluded from response archiving by design — but 12 rows from 2026-05-07 are present

A code-audit anomaly. The `_ARCHIVE_ENDPOINTS` set in `api_logger.py` is `{"live_matches", "match_details"}` — `events_by_date` is NOT in it. Yet `audit.api_response_archive` contains 12 events_by_date rows from a one-hour window on 2026-05-07. The most plausible explanation is that the rows were inserted by a previous code version that did archive `events_by_date`, before the exclusion was added. Recommendation: nothing — the rows are harmless, and the current code does the right thing going forward.

### 10. Multiple TODO/DEFERRED items in the reference guide (`tennis_engine_reference_guide_v4.md`)

- **Step 6A — Streaming Signal Engine**: incremental, stateful version of `signal_engine.py`. Not built.
- **Step 6B — Live Prediction Service**: combines streaming engine with trained model. Not built.
- **Step 6C — Persistent Prediction Logging**: log every prediction with full feature context. Not built.
- **Step 6D — State Machine Integration**: wire prediction service into APScheduler. Not built.
- **Step 6E — Dashboard Integration**: display predictions in the UI. The placeholders ("Model coming soon") exist; no real values flow.
- **Step 7 — Model Iteration**: deferred until live data accumulates.
- **Step 8 — Odds Integration and Edge Analysis**: deferred.

These are not bugs but they explain why the dashboard's "Model" / "Book" / "Edge" / "Signals" cards all render placeholders.

### 11. Reference guide / repo drift

The reference guide describes a `src/p0_engine.py` with fuzzy player matching. That file does not exist; the equivalent functionality lives in `src/live/player_lookup.py`. The reference guide also lists `core/match_id_map`, `core/atp_points_enhanced`, `core/ml_game_level`, `core/charting_*` tables — none of which exist in the current PostgreSQL instance (the offline ML pipeline appears to use a separate DuckDB file `tennis.duckdb`, which is gitignored and not in the audit scope).

### 12. Some endpoint methods on `TennisFeed` are unused but kept

`TennisFeed.get_live_matches` (parsed-summary form), `get_point_by_point`, and `translate_to_engine_format` exist but have no live-path callers. They are exercised by tests only. Dead-but-tested code is technically fine; just worth noting.

### 13. Worker max-runtime is 6 hours, hardcoded

`MatchWorker._max_runtime_seconds = 6 * 3600`. Real ATP/WTA matches can in rare cases run longer than 6 hours (the longest professional tennis match on record was over 11 hours). When that happens, the worker terminates itself even if the match is still in progress. No replay mechanism. Soft issue but worth documenting.

### 14. The `is_break_point` column does not exist; is_ace and is_double_fault default to False on the dashboard side

`_enrich_detail_points` in the backend hardcodes `is_ace: False` and `is_double_fault: False` for every per-point row sent to the dashboard. The data does not flow from API → DB → dashboard for those flags in the live path. They are populated only in `live.backfill_points` (where the backfill script reads `pointDescription` from the point-by-point endpoint). So the backfill table has ace/double-fault info; the live table does not.

---

## APPENDIX — Quick Reference for Key Identifiers

| Identifier | Definition |
| --- | --- |
| Process | One Python process under systemd (`tennis-engine.service`). |
| Entry point | `scripts/run_collector.py`. |
| Scheduler tick | 60 s in PRE_MATCH/LIVE; 3600 s in IDLE. |
| Discovery cycle | 60 s while LIVE (separate from scheduler tick). |
| Per-match poll | 15 s. |
| Worker max runtime | 6 hours. |
| Pre-match arming window | 900 s before scheduled start (state flips IDLE → PRE_MATCH). |
| Pre-spawn window | 300 s before scheduled start (worker started before status flips to inprogress). |
| API base | `https://tennisapi1.p.rapidapi.com`. |
| Endpoints used live | `/api/tennis/events/live`, `/api/tennis/events/{d}/{m}/{y}`, `/api/tennis/event/{id}`. |
| Endpoint used offline only | `/api/tennis/event/{id}/point-by-point`. |
| Live truth table | `live.match_states`. |
| Raw poll table | `live.match_polls`. |
| Per-poll API audit | `audit.api_call_log`. |
| Raw response archive (2 endpoints only) | `audit.api_response_archive`. |
| Polling event audit | `audit.poll_audit_log`. |
| Dashboard URL (server) | `http://142.93.82.38:8000/dashboard`. |
| Database | DigitalOcean managed PostgreSQL, schemas: `live`, `audit`, `book` (empty), `core`, `rankings`, `live_raw` (legacy), `live_processed` (legacy). |
