# Schema Audit — 2026-05-07

Generated at: 2026-05-07T18:29:49
Schemas audited: live_raw, live_processed, public

## UNCATALOGED tables

_None — every found table is in the migration plan._

## Schema `live_raw`

### `live_raw.api_call_log`

- **Status**: OK (in plan)
- **Plan**: MOVE -> audit.api_call_log
- **Rows**: 5,129
- **Most recent timestamp**: `timestamp`: 2026-05-08 00:38:42.604771+00:00
- **Columns**: 12
- **Code refs to `live_raw.api_call_log`**: 7 file(s)
    - `tests/test_api_logger.py`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.2.pyc`
    - `src/live/__pycache__/api_logger.cpython-314.pyc`
    - `src/live/__pycache__/logger.cpython-314.pyc`
- **Code refs to bare `api_call_log`**: 7 file(s)
    - `tests/test_api_logger.py`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.2.pyc`
    - `src/live/__pycache__/api_logger.cpython-314.pyc`
    - `src/live/__pycache__/logger.cpython-314.pyc`

### `live_raw.api_response_archive`

- **Status**: OK (in plan)
- **Plan**: MOVE -> audit.api_response_archive
- **Rows**: 4,891
- **Most recent timestamp**: `timestamp`: 2026-05-08 00:38:42.604771+00:00
- **Columns**: 6
- **Code refs to `live_raw.api_response_archive`**: 7 file(s)
    - `tests/test_api_logger.py`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.2.pyc`
    - `src/live/__pycache__/api_logger.cpython-314.pyc`
    - `src/live/__pycache__/logger.cpython-314.pyc`
- **Code refs to bare `api_response_archive`**: 7 file(s)
    - `tests/test_api_logger.py`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.2.pyc`
    - `src/live/__pycache__/api_logger.cpython-314.pyc`
    - `src/live/__pycache__/logger.cpython-314.pyc`

### `live_raw.match_details`

- **Status**: OK (in plan)
- **Plan**: RENAME+MOVE -> live.match_polls
- **Rows**: 76,897
- **Most recent timestamp**: `polled_at`: 2026-05-07 21:38:36.220460+00:00
- **Columns**: 18
- **Code refs to `live_raw.match_details`**: 13 file(s)
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/src/live/collector.py`
- **Code refs to bare `match_details`**: 24 file(s)
    - `.pytest_cache/v/cache/nodeids`
    - `tests/test_api_logger.py`
    - `tests/__pycache__/test_tennis_feed.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_tennis_feed.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_api_logger.cpython-314-pytest-9.0.3.pyc`

### `live_raw.oddsapi_polls`

- **Status**: OK (in plan)
- **Plan**: RENAME+MOVE -> live.backfill_odds_polls
- **Rows**: 561
- **Most recent timestamp**: `ts`: 2026-04-30 15:46:31.641589+00:00
- **Columns**: 7
- **Code refs to `live_raw.oddsapi_polls`**: 23 file(s)
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/scripts/backtesting/enrich_dashboard_log.py`
- **Code refs to bare `oddsapi_polls`**: 23 file(s)
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/scripts/backtesting/enrich_dashboard_log.py`

### `live_raw.tennisapi_points`

- **Status**: OK (in plan)
- **Plan**: RENAME+MOVE -> live.backfill_points
- **Rows**: 25,040
- **Most recent timestamp**: `ts`: 2026-05-07 21:36:51.419757+00:00
- **Columns**: 16
- **Code refs to `live_raw.tennisapi_points`**: 28 file(s)
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_processed_points.py`
- **Code refs to bare `tennisapi_points`**: 29 file(s)
    - `.pytest_cache/v/cache/nodeids`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`

## Schema `live_processed`

### `live_processed.dashboard_log`

- **Status**: ORPHAN (will drop)
- **Plan**: DROP
- **Rows**: 23,797
- **Most recent timestamp**: `ts`: 2026-05-07 21:36:51.635772+00:00
- **Columns**: 35
- **Code refs to `live_processed.dashboard_log`**: 23 file(s)
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/scripts/backtesting/enrich_dashboard_log.py`
- **Code refs to bare `dashboard_log`**: 26 file(s)
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_logger.py`
    - `.claude/settings.local.json`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_logger.py`

### `live_processed.match_detail_points`

- **Status**: OK (in plan)
- **Plan**: RENAME+MOVE -> live.match_states
- **Rows**: 20,320
- **Most recent timestamp**: `polled_at`: 2026-05-07 21:38:21.048380+00:00
- **Columns**: 23
- **Code refs to `live_processed.match_detail_points`**: 17 file(s)
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/src/live/backend.py`
- **Code refs to bare `match_detail_points`**: 19 file(s)
    - `.pytest_cache/v/cache/nodeids`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_processed_points.py`

### `live_processed.points`

- **Status**: ORPHAN (will drop)
- **Plan**: DROP
- **Rows**: 6,344
- **Most recent timestamp**: `last_updated`: 2026-05-07 21:36:55.024513+00:00
- **Columns**: 21
- **Code refs to `live_processed.points`**: 9 file(s)
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.2.pyc`
    - `tests/__pycache__/test_processed_points.cpython-314-pytest-9.0.3.pyc`
    - `tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/tests/test_processed_points.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/src/live/logger.py`
- **Code refs to bare `points`**: 229 file(s)
    - `.pytest_cache/v/cache/nodeids`
    - `.pytest_cache/v/cache/lastfailed`
    - `tests/test_ml_game_level.py`
    - `tests/test_data_pipeline.py`
    - `tests/test_api_logger.py`

## Schema `public`

### `public.poll_audit_log`

- **Status**: OK (in plan)
- **Plan**: MOVE -> audit.poll_audit_log
- **Rows**: 17,474
- **Most recent timestamp**: `timestamp`: 2026-05-08 00:38:41.317384+00:00
- **Columns**: 10
- **Code refs to `public.poll_audit_log`**: 4 file(s)
    - `.claude/worktrees/gifted-bassi-ebd57b/src/poll_logger.py`
    - `.claude/worktrees/friendly-hawking-ffe42f/src/poll_logger.py`
    - `src/__pycache__/poll_logger.cpython-314.pyc`
    - `src/poll_logger.py`
- **Code refs to bare `poll_audit_log`**: 10 file(s)
    - `tests/__pycache__/test_poll_logger.cpython-314-pytest-9.0.3.pyc`
    - `tests/__pycache__/test_poll_logger.cpython-314-pytest-9.0.2.pyc`
    - `tests/test_poll_logger.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/scripts/dev/audit_match.py`
    - `.claude/worktrees/gifted-bassi-ebd57b/src/poll_logger.py`

