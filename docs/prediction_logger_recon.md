# Prediction Logger — Reconnaissance (Step 6C)

Recon for a forthcoming `live.predictions` table + `PredictionLogger`
that persistently records every `Prediction` emitted by
`src/live_prediction_service.py`. Read-only audit; no production code
written in this session.

---

## SECTION A — `LivePredictionService` current surface

### A1. `process_point` signature & return

[src/live_prediction_service.py:237-259](src/live_prediction_service.py:237):

```python
def process_point(self, pt) -> Prediction | None:
```

- Returns `None` when `signal_dict is None` — i.e. for every point of
  game 1 (the streaming signal engine emits no row before the first
  game closes). See line 257-258.
- Returns a `Prediction` exactly when the just-fed point closes a
  game (`snapshot` and `signal_dict` both non-`None`).
- Raises `RuntimeError` if (a) `finalize()` has already been called
  (line 240), (b) `start_match` was not called first (line 242), or
  (c) score-state and signal-engine disagree about whether a game just
  closed (line 247-255).

`finalize() -> Prediction | None` ([line 261-285](src/live_prediction_service.py:261))
emits one final prediction for the in-progress last game (or `None`
if no game ever closed); idempotent.

### A2. `Prediction` dataclass fields

[src/live_prediction_service.py:38-46](src/live_prediction_service.py:38):

```python
@dataclass(frozen=True)
class Prediction:
    match_id_int: int
    set_number: int
    game_number_in_set: int
    probability_a: float
    confidence: float
    features: dict[str, float]
```

`features` is the full 63-feature dict keyed by canonical names from
`model_v1_metadata.json` (verified by the `assert` at line 335-337).

### A3. State on `self` after `start_match()`

[src/live_prediction_service.py:205-235](src/live_prediction_service.py:205):

```python
self.match_id_int = match_id_int            # int
self.match_state = StreamingMatchState(...)
self.score_state = _ScoreState()
self.finalized = False
self._last_emitted_server_was_a = None

self.p0_a = ...                              # float
self.p0_b = ...                              # float
self.surface_dummies = {                     # dict[str, int], 4 keys
    "surface_hard": 0/1, "surface_clay": 0/1,
    "surface_grass": 0/1, "surface_unknown": 0/1,
}
self.first_server_is_a = bool(first_server_is_a)
```

**Gaps relative to the task brief:**
- **No `player_a_id` / `player_b_id`** — `start_match` takes
  `player_a: str` and `player_b: str` (names, used only to resolve
  p0). Numeric player ids are **not** carried on `self`.
- **No raw `surface` string retained** — only the bucketed one-hot
  dict (`self.surface_dummies`). The raw input surface is consumed
  and discarded inside `start_match`.
- **No separate `player_a` / `player_b` name fields** retained either;
  they exist only as locals during `start_match`.

The logger / caller must either (a) supply ids and raw surface to the
logger directly at match-start time, or (b) the service must be
extended to carry them. Flag this as a small required widening.

### A4. Does `Prediction` carry `match_id`?

**Yes.** `Prediction.match_id_int` is set in `_make_prediction` at
[line 348](src/live_prediction_service.py:348) from `self.match_id_int`.
The caller does **not** need to associate the match id post-hoc.

What `Prediction` does **not** carry: `player_a_id`, `player_b_id`,
the raw surface string, the timestamp of emission, the raw `p0_a` /
`p0_b` used. The logger must source those from either `self` on
`LivePredictionService` (p0_a / p0_b / surface_dummies / first_server_is_a)
or from an out-of-band call-site supplied at `start_match`.

### A5. Where surface comes from + bucketing logic

Surface is passed in by the caller as a raw string to `start_match`.
Bucketing happens at [lines 225-231](src/live_prediction_service.py:225):

```python
surface_norm = (surface or "unknown").lower()
if surface_norm not in _SURFACE_CATEGORIES:
    surface_norm = "unknown"
self.surface_dummies = {
    f"surface_{cat}": 1 if surface_norm == cat else 0
    for cat in _SURFACE_CATEGORIES
}
```

with `_SURFACE_CATEGORIES = ("hard", "clay", "grass", "unknown")` at
[line 35](src/live_prediction_service.py:35).

Behaviour:
- `None` / empty → `"unknown"`.
- Case-insensitive: `"Hard"`, `"HARD"`, `"hard"` all → `"hard"`.
- Anything not in `{hard, clay, grass, unknown}` (e.g. `"carpet"`,
  `"indoor"`) → `"unknown"`. **No exception raised**, unlike
  training's `load_ml_data` which raises on unknown surfaces.

---

## SECTION B — The 63 features

### B1. `feature_names` (verbatim from `model_v1_metadata.json`, order preserved)

```
 0  games_A                       32  sds_avg_pts_per_game_cm_a
 1  games_B                       33  sds_avg_pts_per_game_cm_b
 2  set_number                    34  res_return_win_pct_ws_a
 3  sets_won_A                    35  res_return_win_pct_ws_b
 4  sets_won_B                    36  res_return_win_pct_cm_a
 5  game_number_in_set            37  res_return_win_pct_cm_b
 6  surface_hard                  38  res_bp_conv_rate_ws_a
 7  surface_clay                  39  res_bp_conv_rate_ws_b
 8  surface_grass                 40  res_bp_conv_rate_cm_a
 9  surface_unknown               41  res_bp_conv_rate_cm_b
10  bpi_bp_rate_ws_a              42  cpi_serve_pressure_pct_ws_a
11  bpi_bp_rate_ws_b              43  cpi_serve_pressure_pct_ws_b
12  bpi_bp_rate_cm_a              44  cpi_serve_pressure_pct_cm_a
13  bpi_bp_rate_cm_b              45  cpi_serve_pressure_pct_cm_b
14  bpi_deep_pressure_rate_ws_a   46  cpi_return_pressure_pct_ws_a
15  bpi_deep_pressure_rate_ws_b   47  cpi_return_pressure_pct_ws_b
16  bpi_deep_pressure_rate_cm_a   48  cpi_return_pressure_pct_cm_a
17  bpi_deep_pressure_rate_cm_b   49  cpi_return_pressure_pct_cm_b
18  bpi_near_pressure_rate_ws_a   50  mrs_pwr_10_ws_a
19  bpi_near_pressure_rate_ws_b   51  mrs_pwr_10_ws_b
20  bpi_near_pressure_rate_cm_a   52  mrs_pwr_10_cm_a
21  bpi_near_pressure_rate_cm_b   53  mrs_pwr_10_cm_b
22  sds_serve_win_pct_ws_a        54  mrs_pwr_30_ws_a
23  sds_serve_win_pct_ws_b        55  mrs_pwr_30_ws_b
24  sds_serve_win_pct_cm_a        56  mrs_pwr_30_cm_a
25  sds_serve_win_pct_cm_b        57  mrs_pwr_30_cm_b
26  sds_hold_rate_ws_a            58  mrs_game_streak_ws_a
27  sds_hold_rate_ws_b            59  mrs_game_streak_ws_b
28  sds_hold_rate_cm_a            60  mrs_game_streak_cm_a
29  sds_hold_rate_cm_b            61  mrs_game_streak_cm_b
30  sds_avg_pts_per_game_ws_a     62  markov_set_win_prob_A
31  sds_avg_pts_per_game_ws_b
```

### B2. Per-feature in-memory type (as emitted by the service)

Every feature lands in `features: dict[str, float]` after explicit
`float(...)` coercion at [lines 324-334](src/live_prediction_service.py:324):

```python
features: dict[str, float] = {
    "games_A": float(snapshot["games_A"]),
    ...
    **{k: float(v) for k, v in self.surface_dummies.items()},
    **{col: float(signal_dict[col]) for col in SIGNAL_COLUMNS},
    "markov_set_win_prob_A": float(markov),
}
```

| group | indices | source type | emitted type |
|---|---|---|---|
| `games_A`, `games_B`, `set_number`, `sets_won_A`, `sets_won_B`, `game_number_in_set` | 0–5 | int (from `_ScoreState`) | float |
| `surface_hard/clay/grass/unknown` | 6–9 | int (0 or 1) from `surface_dummies` | float (0.0 / 1.0) |
| 52 signal columns | 10–61 | float / int from `StreamingMatchState.process_point` | float |
| `markov_set_win_prob_A` | 62 | float from `set_win_prob(...)` | float |

For the DB column, the natural mapping is:
- Score context (6) → `INT NOT NULL`.
- Surface one-hot (4) → `BOOLEAN NOT NULL` **or** `SMALLINT NOT NULL`.
  One-hot encoding guarantees exactly one is `1`; pick whichever the
  rest of the live schema favours. Worth a separate column-per or a
  single `surface TEXT` plus on-the-fly one-hot at query time —
  schema design decision flagged for Step 6C build, not for here.
- Signals (52) → `DOUBLE PRECISION NOT NULL`.
- `markov_set_win_prob_A` → `DOUBLE PRECISION NOT NULL`.
- `probability_a`, `confidence` → `DOUBLE PRECISION NOT NULL`.

### B3. Features that could "legitimately be NULL"

The streaming signal engine **does not emit NULL for any feature**.
Per the live_prediction_service recon §10b: the per-player
accumulators start at zero and smoothed BPI denominators default to
`0 / (0+2) = 0`. So:

- `bpi_*` (bp_rate, deep_pressure, near_pressure) for a player who
  has faced no break points yet → emitted as `0.0`, not NULL.
- `res_bp_conv_rate_*` similarly defaults to `0.0` before any break
  point has been seen.
- `mrs_game_streak_*` is `0` if no games yet for that side.
- `mrs_pwr_10_*` / `mrs_pwr_30_*` weight-shifted windows: emitted as
  `0.0` until enough games accumulate.
- `sds_*`, `cpi_*` likewise default to `0.0`.

Tiebreak rows: the engine emits one row per tiebreak game with
`game_number_in_set = 13`. **No signal column is excluded or frozen
during a tiebreak**; the same accumulators tick through. (Confirmed
by `assert set(features) == set(self.feature_names)` at
[line 335](src/live_prediction_service.py:335) — every feature is
present every emission.)

**Implication for the table:** every feature column can safely be
`NOT NULL`. Defaulting to NULL on the DB side would mask a real
emission bug — keep them `NOT NULL` and let inserts fail loudly.

The Markov feature is similarly always-non-NULL (a finite real in
[0, 1], modulo the `[0.35, 0.85]` clamp per `live_prediction_service_recon.md`
§10e).

---

## SECTION C — Existing logger patterns to mirror

### C1. PostgreSQL library

**`psycopg2`** (not psycopg3). [src/live/api_logger.py:21](src/live/api_logger.py:21):

```python
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json
```

Same import in `poll_logger.py:19` and `live/logger.py:9`.

### C2. DB URL env var

**`DATABASE_URL`**. Loaded via `load_dotenv()` then `os.getenv("DATABASE_URL")`.

[src/live/api_logger.py:43-44](src/live/api_logger.py:43):

```python
load_dotenv()
url = db_url or os.getenv("DATABASE_URL")
if not url:
    raise RuntimeError("DATABASE_URL environment variable is not set.")
```

### C3. Singleton pattern (verbatim)

[src/live/api_logger.py:138-181](src/live/api_logger.py:138):

```python
_default_logger: "ApiLogger | None" = None
_default_logger_init_attempted: bool = False
_default_logger_lock = threading.Lock()


def get_default_logger() -> "ApiLogger | None":
    """Return the process-wide default ApiLogger, or None.

    Lazy-initialized on first call. Returns None permanently for the life
    of the process if DATABASE_URL is unset or if construction fails.
    Construction is attempted exactly once; subsequent calls return the
    memoized result. To re-attempt after a transient DB outage, restart
    the process.

    Thread-safe via double-checked locking.
    """
    global _default_logger, _default_logger_init_attempted

    if _default_logger_init_attempted:
        return _default_logger

    with _default_logger_lock:
        if _default_logger_init_attempted:
            return _default_logger

        if not os.getenv("DATABASE_URL"):
            _default_logger_init_attempted = True
            return None

        try:
            _default_logger = ApiLogger()
        except Exception as exc:
            _log.warning(
                "Default ApiLogger construction failed (audit logging disabled "
                "for this process until restart): %s",
                exc,
            )
        # Set the flag ONLY after construction has settled. Setting it before
        # would let the outside fast-path return _default_logger=None while a
        # slow ApiLogger() is still mid-construction in another thread.
        _default_logger_init_attempted = True

    return _default_logger
```

A `_reset_default_logger_for_testing()` helper exists at line 183.

### C4. `log_call` try/except (verbatim)

[src/live/api_logger.py:50-114](src/live/api_logger.py:50):

```python
def log_call(
    self,
    endpoint: str,
    ...
) -> Optional[int]:
    match_id_str = str(match_id) if match_id is not None else None
    try:
        with self._lock:
            with self._conn.cursor() as cur:
                ...
                cur.execute("""INSERT INTO audit.api_call_log ...""", (...))
                call_id = cur.fetchone()[0]
            self._conn.commit()
            return int(call_id)
    except Exception as exc:
        try:
            self._conn.rollback()
        except Exception:
            pass
        _log.warning("ApiLogger.log_call failed: %s", exc)
        return None
```

Pattern: bare `except Exception` around the whole write, nested
`try/except: pass` around rollback (so rollback failure doesn't mask
the original), `_log.warning(...)` (PollLogger uses `_log.debug` — see
C-note below), return `None` on failure.

`PollLogger.log` ([src/poll_logger.py:68-99](src/poll_logger.py:68))
mirrors the same shape but logs the swallowed exception at `DEBUG`
rather than `WARNING`. The `PredictionLogger` should pick **WARNING**
(per ApiLogger) for visibility — a missing prediction row is a real
correctness gap.

### C5. Connection model

**Single persistent connection per logger instance.** [src/live/api_logger.py:47-48](src/live/api_logger.py:47):

```python
self._conn = psycopg2.connect(url)
self._lock = threading.Lock()
```

No pool. All writes serialize through `self._lock`. `psycopg2`
connections are not thread-safe by default; the lock is what makes
the singleton safe for concurrent feed threads. Combined with the
process-wide singleton from C3, the entire process holds exactly one
PG connection per logger class.

### C6. When is `setup()` (DDL init) called?

- **`PollLogger`** has an explicit `setup()` method ([src/poll_logger.py:34-66](src/poll_logger.py:34))
  that is **not** called from `__init__`. Callers invoke it manually
  on process boot.
- **`ApiLogger`** has **no `setup()` method**. The DDL for
  `audit.api_call_log` and `audit.api_response_archive` lives in
  `MatchLogger`'s `_SETUP_STMTS` ([src/live/logger.py:13-141](src/live/logger.py:13))
  and is executed in `MatchLogger.__init__` ([line 317-323](src/live/logger.py:317)).
  So the live process boots `MatchLogger` once, which DDLs every
  schema (`live.*`, `audit.*`, `book.*`) idempotently, and then
  `ApiLogger` / `PollLogger` write into pre-existing tables.

**Recommendation for `PredictionLogger`:** put the `live.predictions`
DDL in a dedicated migration script (mirroring
`scripts/setup/migrate_book_and_audit_v2.py` — see §D) rather than
inside `MatchLogger._SETUP_STMTS`. That keeps the new table's
provenance auditable in `scripts/setup/` and decouples table creation
from the running process. `PredictionLogger.__init__` then just opens
the connection and trusts the table to exist (with a
`_log.warning("table missing, run migration X")` if a write fails for
that reason — matches the silent-on-failure contract).

### C7. Parameter binding

Confirmed **`%s`** placeholders, not `?`. Every `cur.execute` in
`api_logger.py`, `poll_logger.py`, and `live/logger.py` uses `%s`.
`Json(...)` wrapping for JSONB columns ([api_logger.py:79,95,99](src/live/api_logger.py:79)).

---

## SECTION D — Schema migration pattern

### D1. Structural outline of `migrate_book_and_audit_v2.py`

[scripts/setup/migrate_book_and_audit_v2.py](scripts/setup/migrate_book_and_audit_v2.py):

1. **Env load** (lines 33-34): `load_dotenv(_ROOT / ".env")` then
   `db_url = os.getenv("DATABASE_URL")` ([line 295](scripts/setup/migrate_book_and_audit_v2.py:295)). Fail-fast with stderr message + exit 1.
2. **Connection** ([line 305-306](scripts/setup/migrate_book_and_audit_v2.py:305)):
   `conn = psycopg2.connect(db_url); conn.autocommit = False`.
3. **Single transaction wrap** ([lines 307-347](scripts/setup/migrate_book_and_audit_v2.py:307)):
   one `with conn.cursor() as cur:` block; all DDL inside; `conn.commit()`
   on success, `conn.rollback()` on any exception.
4. **DDL is constants** at module top (`_BOOK_MATCHES_DDL`,
   `_BOOK_PLAYERS_DDL`, …) — each uses `CREATE TABLE IF NOT EXISTS`.
5. **Idempotency via information_schema probes** ([lines 134-185](scripts/setup/migrate_book_and_audit_v2.py:134)):
   `_table_exists`, `_column_exists`, `_column_is_nullable`,
   `_index_exists`, `_gap_table_name_in_use` — checked **before**
   each `ALTER` / rename, so re-runs print `[SKIP]` instead of failing.
6. **Progress reporting** via plain `print(...)`:
   - `[OK]   ...` for executed ops
   - `[SKIP] ...` for no-op (already applied)
   - `[ERR]  ...` for failures
   - Counters dict `{"created", "skipped", "errors"}` tallied across
     all ops, summary printed at end (`Done: N created, M skipped, K errors`).
7. **Exit codes**: 0 success, 1 missing env, 2 migration failed.

**Template to mirror for `live.predictions`:** copy this script's
`main()` shell, replace the DDL list with `live.predictions` CREATE +
its indexes, keep the `_table_exists` / `_index_exists` guards.

### D2. Existing `live.*` table conventions

From [src/live/logger.py:13-141](src/live/logger.py:13) and
[scripts/setup/migrate_book_and_audit_v2.py:41-96](scripts/setup/migrate_book_and_audit_v2.py:41):

| table | primary key | timestamp column(s) |
|---|---|---|
| `live.backfill_points` | (none — bulk-loaded) | `ts TIMESTAMPTZ` |
| `live.backfill_odds_polls` | (none) | `ts TIMESTAMPTZ` |
| `live.match_polls` | (none) | `polled_at TIMESTAMPTZ` |
| `live.match_states` | **composite** `(match_id, polled_at)` | `polled_at TIMESTAMPTZ` |
| `audit.api_call_log` | **`BIGSERIAL`** `id` | `timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `audit.api_response_archive` | `BIGSERIAL id` | `timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `audit.poll_audit_log` | `SERIAL id` | `timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `audit.verification_reports` | (see migration; not inspected here) | n/a |
| `book.matches` | `BIGINT match_id` | `promoted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `book.players` | `BIGINT player_id` | `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `book.points` | `BIGSERIAL id` + UNIQUE `(match_id, set_num, game_num, point_num)` | (none) |

**Convention summary for `live.predictions`:**
- The `live.*` schema's only existing table with a real PK is
  `live.match_states`, which uses a **composite natural key**
  `(match_id, polled_at)`. Mirroring that: `live.predictions` PK
  candidate is **`(match_id_int, set_number, game_number_in_set)`**
  — naturally unique because the service emits exactly one prediction
  per (match, set, game) (asserted by the serve-alternation invariant
  at [src/live_prediction_service.py:303-313](src/live_prediction_service.py:303)).
- Timestamp column name: `live.match_polls` and `live.match_states`
  both use **`polled_at`**. `audit.*` and `book.*` tables use
  `timestamp` / `promoted_at` / `created_at` instead. For
  `live.predictions` the closest semantic match is **`predicted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`** — a new verb, since
  predictions aren't polls. Worth a one-line check with the user that
  `predicted_at` is preferred over reusing `polled_at` (which would
  be misleading — predictions are derived, not polled).
- If `BIGSERIAL` surrogate is desired in addition (for FK ergonomics
  from a future `live.bets` / `live.outcomes` table), follow
  `book.points`'s pattern: `id BIGSERIAL PRIMARY KEY` + a separate
  `UNIQUE (match_id_int, set_number, game_number_in_set)` constraint.

---

## SECTION E — Connection budget sanity check

### E1. Persistent-connection sources in the live process

Grep for `psycopg2.connect` under `src/` (production code only),
excluding worktrees / .venv / tests:

```
src/poll_logger.py:31           PollLogger._conn         — singleton, persistent
src/live/logger.py:317          MatchLogger._conn         — persistent, holds DDL init
src/live/api_logger.py:47       ApiLogger._conn           — singleton, persistent
src/live/backend.py:41          _conn()                   — PER-REQUEST (FastAPI handlers)
src/live/player_lookup.py:84    inline                    — PER-CALL
src/verification/live_adapter.py:60     inline            — PER-RUN (verification script)
src/verification/backfill_adapter.py:220 inline           — PER-RUN
src/book/player_resolver.py:200 inline                    — PER-CALL
src/book/promoter.py:541        inline                    — PER-CALL
```

**Persistent (long-lived) connections held by the live worker process: 3.**

1. `MatchLogger` — one PG conn held for the lifetime of the worker.
2. `ApiLogger` (singleton via `get_default_logger`) — one PG conn.
3. `PollLogger` — one PG conn (constructed at worker start, see
   `src/poll_logger.py`).

The `_conn()` helper in `src/live/backend.py` opens **per-request**
inside FastAPI handlers using `with closing(_conn()) as conn:` ([example at line 267](src/live/backend.py:267)) so it doesn't add to the
persistent count — those connections are released on response. Same
for `player_lookup`, `book.player_resolver`, `book.promoter`, the
verification adapters, and every script under `scripts/`.

**Adding `PredictionLogger` as a third-style singleton with one
persistent connection brings the persistent-connection total to
N+1 = 4** in the live worker process.

The recon doc's reference to "§10.2 of the schema_v2 reference" did
not resolve — no `schema_v2*` doc exists under `docs/`. Closest
existing pattern doc is `docs/live_prediction_service_recon.md`,
which does not enumerate connection sources. The grep above is the
authoritative count.

Managed-Postgres connection caps (Supabase free tier = 60,
Neon shared = ~100) are not tight against 4 persistent connections
even with multiple worker processes. No connection-budget concern,
but the pattern of "one process-wide singleton, one connection,
serialized via `threading.Lock`" should continue to be the rule —
not a per-`LivePredictionService`-instance connection.
