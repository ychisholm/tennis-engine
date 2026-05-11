# Streaming Signal Engine — Reconnaissance

Recon of the existing batch engine in `src/signal_engine.py` ahead of
building a stateful streaming twin in `src/streaming_signal_engine.py`.
No code is being changed here — this is a map of the territory.

---

## 1. Public surface of the batch engine

**Module:** `src/signal_engine.py`

**Main class:** `SignalEngine` (a stateless class — all per-match state
lives inside the `process_match` call).

**Main signature:**

```python
class SignalEngine:
    def process_match(
        self, match_id_int: int, points_iter: Iterable
    ) -> list[dict]:
```

**Module-level exports also used by callers:**

- `SIGNAL_COLUMNS: list[str]` — the 52 sub-component names in canonical
  order (asserted `== 52` at import time).
- `BP_STATES`, `DEEP_BP_STATES`, `PRESSURE_STATES`, `DEUCE_STATE` — the
  score-state sets used to classify `score_before`.

**Input shape:** an iterable of point-like objects. Each object must
expose attributes `set_number`, `game_number_in_set`, `score_before`,
`Svr`, `PtWinner`, `is_tiebreak`. The engine sorts nothing — the caller
must hand points in `Pt`-ascending order. In practice the caller passes
a pandas `itertuples` named-tuple iterator (see §1 below) so attribute
access works.

**Return shape:** a `list[dict]`, one dict per completed game. Each dict
contains:
- `match_id_int` (int) — echoed back from the caller
- `set_number` (int)
- `game_number_in_set` (int)
- the 52 signal columns from `SIGNAL_COLUMNS`, all `float` except the
  four `mrs_game_streak_*` columns which are `int`

**Invocation from `scripts/dev/build_signals.py`** (`compute_signals`,
line 74):

```python
engine = SignalEngine()
...
for i, (mid, group) in enumerate(
    points.groupby("match_id_int", sort=False), start=1
):
    rows = engine.process_match(int(mid), group.itertuples(index=False))
    all_rows.extend(rows)
```

The points DataFrame is loaded in `load_points` and pre-sorted
`ORDER BY mim.match_id_int, ape.Pt`, then grouped by `match_id_int`
without re-sorting (`sort=False`). One `SignalEngine` instance services
every match in the build.

---

## 2. Per-point input format

The engine reads exactly these six attributes off each point:

| Attribute | Type | Source |
|---|---|---|
| `set_number` | int | **Derived in `load_points`** as `ape.Set1 + ape.Set2 + 1` |
| `game_number_in_set` | int | **Derived in `load_points`** as `ape.Gm1 + ape.Gm2 + 1` |
| `score_before` | str | Direct from `core.atp_points_enhanced.score_before` |
| `Svr` | int (1 or 2) | `CAST(ape.Svr AS INTEGER)` |
| `PtWinner` | int (1 or 2) | `CAST(ape.PtWinner AS INTEGER)` |
| `is_tiebreak` | bool | Direct from `core.atp_points_enhanced.is_tiebreak` |

The DataFrame loader also selects `match_id_int` and `Pt`, but only for
grouping and ordering — the engine itself never reads them.

**`score_before` is the only source of truth for score state.** The
column `core.atp_points_enhanced.is_break_point` does exist in the
schema but `signal_engine.py` never references it. All BP / deep-BP /
near-pressure / deuce classification happens via membership tests
against the module-level `BP_STATES`, `DEEP_BP_STATES`, `PRESSURE_STATES`
and `DEUCE_STATE` set on the `score_before` string. This is consistent
with the documented warning that `is_break_point` is unreliable.

**Concerns**
- The loader's `set_number = Set1 + Set2 + 1` and `game_number_in_set
  = Gm1 + Gm2 + 1` derivations live in `build_signals.py`, not in the
  engine. The streaming engine will need the same derivation done
  upstream (or rederived inside the streaming state itself, since the
  live point feed will not have `set_number`/`game_number_in_set`
  pre-computed).

---

## 3. Internal state maintained during the walk

All state is local to one `process_match` call. Three nested
data structures:

### 3a. `acc` — per-player sub-component accumulators

`acc = {"ws": {1: _PlayerAcc(), 2: _PlayerAcc()},
         "cm": {1: _PlayerAcc(), 2: _PlayerAcc()}}`

`_PlayerAcc` fields (`src/signal_engine.py:55-74`) and which signal
sub-components they feed:

| Field | Updated when | Feeds |
|---|---|---|
| `serve_pts_played` | every non-tiebreak point where player is server | `sds_serve_win_pct` (denom) |
| `serve_pts_won` | non-tiebreak point won while serving | `sds_serve_win_pct` (num) |
| `service_games_played` | game close, non-tiebreak | `sds_hold_rate`, `sds_avg_pts_per_game` (denoms) |
| `service_games_held` | game close, non-tiebreak, server won game | `sds_hold_rate` (num) |
| `service_game_total_points` | game close, non-tiebreak: `+= game.points_played` | `sds_avg_pts_per_game` (num) |
| `return_pts_played` | every non-tiebreak point where player is returner | `res_return_win_pct` (denom) |
| `return_pts_won` | non-tiebreak point won while returning | `res_return_win_pct` (num) |
| `return_games_played` | game close, non-tiebreak | `bpi_*` smoothed-rate denoms (`+2` smoothing) |
| `breaks_won_with_bp` | game close, non-tiebreak, returner won and game had a BP state | `res_bp_conv_rate` (num) |
| `bp_states_faced` | game close, non-tiebreak: `+= game.bp_count` (returner) | `bpi_bp_rate`, `res_bp_conv_rate` (denom) |
| `deep_pressure_games` | game close, non-tiebreak, `deep_reached` | `bpi_deep_pressure_rate` (num, returner) |
| `near_pressure_games` | game close, non-tiebreak, `deuce_reached and not bp_state_reached` | `bpi_near_pressure_rate` (num, returner) |
| `serve_pressure_played` | **every** point (incl. tiebreak) in `PRESSURE_STATES`, server | `cpi_serve_pressure_pct` (denom) |
| `serve_pressure_won` | pressure point, server won | `cpi_serve_pressure_pct` (num) |
| `return_pressure_played` | pressure point, returner | `cpi_return_pressure_pct` (denom) |
| `return_pressure_won` | pressure point, returner won | `cpi_return_pressure_pct` (num) |
| `game_streak` | game close (incl. tiebreak): winner += 1, loser := 0 | `mrs_game_streak` |

### 3b. `mrs_deque` — point-winner rolling windows

`mrs_deque = {"ws": deque(maxlen=30), "cm": deque(maxlen=30)}`

Every point's `winner_int` (`1` or `2`) is appended to **both** deques
on every point, including tiebreak points. At emit time the engine
reads `last10 = list(dq)[-10:]` and `last30 = list(dq)` (i.e. the whole
deque, capped at 30 by `maxlen`) and computes player A and B's
fraction-of-wins.

### 3c. `in_game` — within-game tracker

`_GameTracker` (`src/signal_engine.py:77-87`):

| Field | Reset | Purpose |
|---|---|---|
| `is_tiebreak` | every new game (init from current point) — sticky-OR'd as the game progresses | gates `_close_game` accounting |
| `server` | every new game (init from current point's `Svr`) | identifies server at game close |
| `bp_count` | every new game | accumulated `BP_STATES` hits this game → returner `bp_states_faced` |
| `deep_reached` | every new game | did we hit `0-40`/`15-40` this game |
| `bp_state_reached` | every new game | did we hit any BP state this game |
| `deuce_reached` | every new game | did we hit `40-40` this game |
| `points_played` | every new game | non-tiebreak point count this game |
| `last_winner` | every new game | who won the **last** point of the game ⇒ game winner |

### 3d. Reset rules summary

- **`acc["ws"]` + `mrs_deque["ws"]`** are reset (rebuilt fresh) whenever
  `prev_set is not None and cur_set != prev_set`. So they reset *before*
  the first game of every set after set 1.
- **`acc["cm"]` + `mrs_deque["cm"]`** are never reset — they carry
  across the entire match.
- **`in_game`** is rebuilt on every game boundary (new `(set, game)`
  pair).

### Concerns
- `game_streak` is updated unconditionally on game close, including
  tiebreak-game close. That means the tiebreak result *does* affect the
  `mrs_game_streak_*_*` columns on the tiebreak row, which is consistent
  with "MRS continues" in §6.
- `service_game_total_points` uses `game.points_played`, which is the
  **non-tiebreak** point counter (since `in_game.points_played += 1` is
  inside the `if not point_is_tiebreak` block). For a tiebreak game
  `points_played` would be `0`, but `_close_game` early-returns for
  tiebreak before touching `service_game_total_points`, so this is safe.

---

## 4. Game boundary detection

The engine does **not** use a stored game-counter column or score-state
heuristic to know a game has ended. It compares `(set_number,
game_number_in_set)` on the incoming point to the prior point's
`(prev_set, prev_game)`:

```python
cur_set = int(pt.set_number)
cur_game = int(pt.game_number_in_set)

if (cur_set, cur_game) != (prev_set, prev_game):
    if in_game is not None:
        self._close_game(in_game, acc)
        rows.append(self._emit_row(
            match_id_int, prev_set, prev_game, acc, mrs_deque
        ))
    ...
    in_game = _GameTracker(...)
    prev_set, prev_game = cur_set, cur_game
```

So the previous game is closed *only when the first point of the next
game arrives* — or, for the final game of the match, by the explicit
post-loop flush at `src/signal_engine.py:174-178`. This is a
look-one-point-ahead pattern (see §8).

### Concerns
- The detection assumes monotonic `(set, game)` pairs. If the upstream
  loader ever emits a duplicate-game point with an earlier
  `game_number_in_set`, the engine would treat it as a new game. The
  loader's `ORDER BY mim.match_id_int, ape.Pt` plus the `Set1+Set2+1`
  derivation make that effectively impossible, but it is a latent
  assumption.

---

## 5. Set boundary detection

Same pair-comparison; the set boundary is the subset of game boundaries
where the set number changed:

```python
if prev_set is not None and cur_set != prev_set:
    acc["ws"] = _new_acc_set()
    mrs_deque["ws"] = deque(maxlen=30)
```

This block runs **after** `_close_game`/`_emit_row` for the just-ended
game and **before** the new `_GameTracker` is built. So:
- The final game of the *outgoing* set is emitted using the still-built
  `acc["ws"]` (correct: it reflects that game's full state).
- Only the `ws` half resets. `acc["cm"]` and `mrs_deque["cm"]` carry
  forward untouched.
- `in_game` (within-game tracker) is replaced unconditionally on every
  game boundary, set boundary or not.

### Concerns
- The reset happens *between* emit and the new `_GameTracker` construction.
  A streaming twin must keep the same ordering — if reset runs before
  emit, the last game of set N would be reported with empty `ws` stats.

---

## 6. Tiebreak detection and handling

**Detection:** taken straight from the point's `is_tiebreak` field
(boolean). The engine seeds `in_game.is_tiebreak` from the first point
of the game and then sticky-OR's it on every subsequent point:

```python
in_game.is_tiebreak = in_game.is_tiebreak or point_is_tiebreak
```

So if any point in the game is flagged `is_tiebreak`, the game is
treated as a tiebreak at close.

**Behaviour during tiebreak points** (matches the docstring claim):

| Sub-component | What happens on a tiebreak point | Frozen? |
|---|---|---|
| BPI (`bp_states_faced`, `deep_pressure_games`, `near_pressure_games`, `return_games_played` smoothing) | All updates are inside `if not point_is_tiebreak` and inside the `if g.is_tiebreak: return` early-exit in `_close_game`. Numerators and the smoothed-denominator both freeze. | ✅ frozen |
| SDS (`serve_pts_*`, `service_games_*`, `service_game_total_points`) | Same gates as BPI — point-level counters skipped, game-close counters skipped. | ✅ frozen |
| RES (`return_pts_*`, `breaks_won_with_bp`, `bp_states_faced`) | Same gates. | ✅ frozen |
| CPI (`serve_pressure_*`, `return_pressure_*`) | `PRESSURE_STATES` check is **outside** the `if not point_is_tiebreak` block (line 143) — updates continue every tiebreak point at a pressure state. | ❌ keeps updating |
| MRS (`mrs_deque` append, `game_streak`) | Deque append is unconditional (line 140-141). `game_streak` updates in `_close_game` *before* the tiebreak early-return (lines 190-196). | ❌ keeps updating |

Tests 6 (`test_tiebreak_freeze_bpi`) and 7 (`test_tiebreak_advance_cpi_mrs`)
in `tests/test_signal_engine.py:147-207` enforce both halves of this
contract against the persisted `core.ml_game_level` table.

### Concerns
- `PRESSURE_STATES` membership depends on `score_before` being expressed
  in the *game* scoring vocabulary (`0-15`/`AD-40`/...). During a
  tiebreak, the upstream `score_before` is numeric (`5-3`, `6-6`,
  `7-6`, etc.) and almost never matches those strings, so CPI
  effectively only updates on the rare coincidence of a tiebreak point's
  `score_before` happening to land in `PRESSURE_STATES`. Worth verifying
  on a real tiebreak row whether this is intentional or a quiet bug —
  the test only asserts "at least one" CPI/MRS value moves, and MRS
  alone (deque + streak) is enough to satisfy it.

---

## 7. Output schema

The 52 column names in canonical order (as produced by
`_signal_column_names()`):

```
bpi_bp_rate_ws_a              bpi_bp_rate_ws_b              bpi_bp_rate_cm_a              bpi_bp_rate_cm_b
bpi_deep_pressure_rate_ws_a   bpi_deep_pressure_rate_ws_b   bpi_deep_pressure_rate_cm_a   bpi_deep_pressure_rate_cm_b
bpi_near_pressure_rate_ws_a   bpi_near_pressure_rate_ws_b   bpi_near_pressure_rate_cm_a   bpi_near_pressure_rate_cm_b
sds_serve_win_pct_ws_a        sds_serve_win_pct_ws_b        sds_serve_win_pct_cm_a        sds_serve_win_pct_cm_b
sds_hold_rate_ws_a            sds_hold_rate_ws_b            sds_hold_rate_cm_a            sds_hold_rate_cm_b
sds_avg_pts_per_game_ws_a     sds_avg_pts_per_game_ws_b     sds_avg_pts_per_game_cm_a     sds_avg_pts_per_game_cm_b
res_return_win_pct_ws_a       res_return_win_pct_ws_b       res_return_win_pct_cm_a       res_return_win_pct_cm_b
res_bp_conv_rate_ws_a         res_bp_conv_rate_ws_b         res_bp_conv_rate_cm_a         res_bp_conv_rate_cm_b
cpi_serve_pressure_pct_ws_a   cpi_serve_pressure_pct_ws_b   cpi_serve_pressure_pct_cm_a   cpi_serve_pressure_pct_cm_b
cpi_return_pressure_pct_ws_a  cpi_return_pressure_pct_ws_b  cpi_return_pressure_pct_cm_a  cpi_return_pressure_pct_cm_b
mrs_pwr_10_ws_a               mrs_pwr_10_ws_b               mrs_pwr_10_cm_a               mrs_pwr_10_cm_b
mrs_pwr_30_ws_a               mrs_pwr_30_ws_b               mrs_pwr_30_cm_a               mrs_pwr_30_cm_b
mrs_game_streak_ws_a          mrs_game_streak_ws_b          mrs_game_streak_cm_a          mrs_game_streak_cm_b
```

(Iteration order in `_signal_column_names`: for each `(signal,
subcomponent)` pair, then for each `ver in ("ws", "cm")`, then for each
`player in ("a", "b")`. That produces the `..._ws_a, _ws_b, _cm_a,
_cm_b` quartets above.)

**Naming convention** — confirmed: every column matches
`{signal}_{subcomponent}_{ws|cm}_{a|b}`.

**Score-context columns emitted alongside the 52 signals** — each row
dict also carries:
- `match_id_int` (int)
- `set_number` (int)
- `game_number_in_set` (int)

These are *the only* identifier columns emitted by the engine. All
other game-row context (`match_id_string`, `player_A`, `player_B`,
`surface`, `games_A`, `is_tiebreak`, `server_was_A`, `points_in_game`,
`server_held`, etc. — the 28 "original" columns) lives in
`core.ml_game_level` already and is joined back in by
`merge_into_ml_game_level` on `(match_id_int, set_number,
game_number_in_set)`. The signal engine emits no score, no winner, no
tiebreak flag.

**Markov feature** — `markov_set_win_prob_A` is **not** computed inside
`SignalEngine`. It's added by a separate Step 4 script
(`scripts/dev/build_markov_feature.py`) which `ALTER TABLE
core.ml_game_level ADD COLUMN markov_set_win_prob_A DOUBLE` and then
updates each row using `src.engine.markov_engine.set_win_prob` driven
by a per-player p0 lookup. The DuckDB describe confirms it is the 81st
column on `core.ml_game_level` and is read by
`src/data_loader.py` and `src/baseline.py` downstream.

### Concerns
- The signal engine assumes its 52 columns and the 28-original-column
  schema in `core.ml_game_level` agree on `(match_id_int, set_number,
  game_number_in_set)`. If the upstream `build_ml_game_level` ever emits
  a row that the signal engine *doesn't* produce (or vice versa),
  `merge_into_ml_game_level` silently NULL-fills via the `LEFT JOIN`.
  The build report's "Unmatched ml_game_level rows" counter is the only
  alert.

---

## 8. Lookahead and end-of-match dependencies

The batch engine has **one structural lookahead** and **one
end-of-stream dependency**, both centred on the game-boundary detector
in §4.

1. **Game close is deferred until the next point arrives.** The
   `_close_game` + `_emit_row` calls live inside the
   `if (cur_set, cur_game) != (prev_set, prev_game)` block, which only
   fires when the *next* game's first point has been read. In effect
   the engine is `O(1)` look-ahead over the point stream. For streaming:
   - We cannot emit the row for game *G* on the *last* point of *G*. We
     have to wait for the first point of *G+1* (or for an explicit
     end-of-match signal) before we know game *G* is done.
   - In the live pipeline, this means a one-point lag between "the game
     ended in the real match" and "we emit the row to downstream
     consumers." That lag will need to be acknowledged in the streaming
     API contract.

2. **End-of-match flush is explicit and required.** After the iterator
   is exhausted, the engine does:
   ```python
   if in_game is not None:
       self._close_game(in_game, acc)
       rows.append(self._emit_row(...))
   ```
   There is no `Pt`-count-based or score-based end-of-match detector. For
   streaming, an external caller must invoke a `finalize()` / `end()` /
   `close()` method to get the final game's row, because no following
   point will ever trigger the boundary check that flushes it.

3. **No full-match-length dependency.** The engine does not know how
   many points are in the match, how many sets to expect, or who won.
   Everything is local — match-id is just echoed back, and the
   `mrs_deque` `maxlen=30` is the only "horizon" parameter.

4. **One subtler lookahead is hiding inside `_close_game`:** to decide
   "did the returner win the game with a BP state?", the engine reads
   `g.last_winner` (the winner of the most recent *non-end-of-game*
   point) and `g.bp_state_reached` (whether any BP state was hit). The
   "winner of the game" is taken to equal `last_winner` of the final
   point — which only works because games end on a point win, not on a
   gap. Streaming doesn't change this; just worth noting that the
   convention is "the last point's winner *is* the game winner."

### Concerns
- A live point feed can stall mid-game (e.g. medical timeout, weather
  delay). The streaming engine needs to tolerate "no point for N
  minutes, then a point that's still in the same `(set, game)`." Easy
  to implement (just keep mutating `in_game`), but the design should
  not require flush-on-timeout.
- Conversely, a live feed might *skip* a point if the upstream scraper
  loses one. The batch engine has no point-counter validation, so a
  missing point silently corrupts `points_played`/`service_pts_*`
  counters. Worth deciding whether the streaming version should
  validate `Pt` continuity or remain permissive.

---

## 9. Test fixture structure

`tests/test_signal_engine.py` has 13 tests, split cleanly into two
groups:

**Group A — DB-backed (tests 1-7, 12, 13).** They run against the
persisted `core.ml_game_level` table via a module-scoped `con` fixture
(`tests/test_signal_engine.py:42-56`). The fixture `pytest.skip`s if
`data/processed/tennis.duckdb` is missing or if the signal columns have
not been built yet (sentinel: `bpi_bp_rate_ws_a` not in
`information_schema.columns`). Constants:
- `EXPECTED_ROW_COUNT = 120_718`
- `EXPECTED_TOTAL_COLS = 80` (28 original + 52 signal)
- `EXPECTED_SIGNAL_COLS = 52`

**Group B — synthetic (tests 8-11).** They build inline lists of `Pt`
dataclass instances and call `SignalEngine().process_match(...)`
directly. The `Pt` dataclass (`tests/test_signal_engine.py:30-39`):

```python
@dataclass
class Pt:
    set_number: int
    game_number_in_set: int
    Pt: int
    score_before: str
    Svr: int
    PtWinner: int
    is_tiebreak: bool = False
```

(The `Pt` field is carried for ordering parity with the production
schema but is not read by the engine.)

There are **no canonical match-IDs** used as named regression cases —
the synthetic tests use throwaway integers (`42`, `99`, `7`, `13`,
`101`) that exist only to be echoed back into the output rows.

Test 13 (`test_total_runtime_acceptable`) is a side-channel test on
`data/processed/signals_build_report.txt` — it greps for the "Total
runtime: Xs" line and asserts under 15 minutes. Skips cleanly if the
report file is missing.

### Concerns
- The DB-backed tests are tightly coupled to a frozen
  `EXPECTED_ROW_COUNT = 120_718`. Any window-expansion in `load_points`
  (currently `BETWEEN '2015-01-01' AND '2025-12-31'`) will break these
  unrelated tests. Not the streaming engine's problem, but worth
  knowing before any shared-fixture refactor.
- There is **no end-to-end equivalence test** between the persisted
  `core.ml_game_level` and a fresh `process_match` run on the same
  match — i.e. no regression harness that would catch a streaming
  drift today. We'll likely have to build that as part of the
  streaming work (compare `SignalEngine().process_match(mid, ...)` vs
  `StreamingMatchState(mid).process_point(...)` row-for-row).

---

## 10. Recommendations for the streaming version

**Public API shape.** For batch ↔ streaming equivalence to be a
one-line test, the streaming class should make it trivial to assemble
the same `list[dict]` the batch engine returns. A natural shape:

```python
class StreamingMatchState:
    def __init__(self, match_id_int: int) -> None: ...
    def process_point(self, pt) -> dict | None:
        """Feed one point. Returns a 55-key game-row dict iff a game
        just closed (i.e. this `pt` is the first point of a new game),
        else None."""
    def finalize(self) -> dict | None:
        """Flush the final game. Returns its row dict, or None if no
        points were ever fed."""
```

The equivalence test then becomes:

```python
state = StreamingMatchState(mid)
streamed = [r for pt in points if (r := state.process_point(pt))]
if (last := state.finalize()): streamed.append(last)
assert streamed == SignalEngine().process_match(mid, iter(points))
```

The `process_point` return-`None`-mid-game contract matches the
batch engine's "emit only on game-boundary detect" behaviour and keeps
the one-point lag explicit at the API surface. Per-set/per-match reset
logic stays internal — callers only see point in, optional row out.

**Natural seam for later shared helpers.** The internals already
factor cleanly:
- `_PlayerAcc`, `_GameTracker` and `_new_acc_set()` are pure data
  containers — directly shareable.
- `_close_game(g, acc)` and `_emit_row(match_id_int, set_number,
  game_number_in_set, acc, mrs_deque)` are pure functions (both
  `@staticmethod` already) and can be called verbatim from the
  streaming class.
- The one block that *does not* factor today is the body of the `for pt
  in points_iter:` loop in `process_match` (lines 115-172) — the
  boundary-detect / set-reset / per-point-accumulate logic. The cleanest
  future seam (we are not refactoring now) is to extract that loop body
  as a free function `_apply_point(pt, *, acc, mrs_deque, in_game,
  prev_set, prev_game, rows, match_id_int) -> (in_game, prev_set,
  prev_game)`; the batch engine would then become a thin loop and the
  streaming engine would call it directly from `process_point`.

No refactor is needed up front — the streaming engine can duplicate the
loop body, write its equivalence test, and only later DRY out the seam
once both implementations are passing identical fixtures.
