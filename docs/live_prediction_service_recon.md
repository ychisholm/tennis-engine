# Live Prediction Service — Reconnaissance

Recon for a forthcoming `src/live_prediction_service.py`. The service
will drive a `StreamingMatchState`, assemble the 63-feature vector the
trained model expects, compute the Markov baseline feature, and call
`model.predict_proba`. Everything below is what the live code must
match exactly — drift anywhere here breaks alignment with training.

---

## 1. Trained model loading

```python
>>> import joblib
>>> model = joblib.load("data/processed/model_v1.joblib")
>>> type(model).__name__              # CalibratedClassifierCV
>>> type(model.estimator).__name__    # XGBClassifier
>>> model.method                       # 'isotonic'
>>> len(model.calibrated_classifiers_) # 5  (5-fold internal CV)
>>> model.classes_                     # array([0, 1])
>>> model.n_features_in_               # 63
>>> hasattr(model, "feature_names_in_") # False
```

- **Class.** `sklearn.calibration.CalibratedClassifierCV` wrapping an
  `xgboost.XGBClassifier`, calibrated with `method='isotonic'` over 5
  CV folds.
- **`predict_proba` input.** Expects a 2-D float array of shape
  `(n_rows, 63)`. The training-time `load_ml_data` builds `X` as
  `df[feature_names].to_numpy(dtype=np.float64)` ([src/data_loader.py:104](src/data_loader.py:104)),
  so the live service should also pass `float64`. No scaler / pipeline
  wraps the calibrated estimator — features go in raw.
- **`classes_` ordering.** `[0, 1]`. So `predict_proba(X)[:, 1]` is the
  probability that `set_winner_is_A == 1` (i.e. P(A wins the current set)).
- **Feature-name preservation.** The calibrated model does **not**
  carry `feature_names_in_` (XGB strips it through calibration). The
  canonical 63-feature order is therefore the
  `feature_names` array in `data/processed/model_v1_metadata.json`,
  which is identical to what `load_ml_data` constructs in
  [src/data_loader.py:100](src/data_loader.py:100):
  `NUMERIC_CONTEXT + SURFACE_DUMMIES + signal_cols + ["markov_set_win_prob_A"]`.

### Concerns
- The model object holds no feature-name guard. Pass a misordered
  column and `predict_proba` will happily return nonsense. The live
  service should construct the row from a dict keyed by the metadata
  names, then materialise as an array in metadata order.

---

## 2. Feature order (canonical 63)

From `data/processed/model_v1_metadata.json["feature_names"]`, verbatim:

| idx | name | group |
|---|---|---|
| 0 | `games_A` | numeric context |
| 1 | `games_B` | numeric context |
| 2 | `set_number` | numeric context |
| 3 | `sets_won_A` | numeric context |
| 4 | `sets_won_B` | numeric context |
| 5 | `game_number_in_set` | numeric context |
| 6 | `surface_hard` | surface one-hot |
| 7 | `surface_clay` | surface one-hot |
| 8 | `surface_grass` | surface one-hot |
| 9 | `surface_unknown` | surface one-hot |
| 10–61 | 52 signal sub-components | from `SIGNAL_COLUMNS` |
| 62 | `markov_set_win_prob_A` | Markov baseline |

Within the 52-block, the names and order match `SignalEngine.SIGNAL_COLUMNS`
exactly (see `docs/streaming_signal_engine_recon.md` §7). Cross-checked:

- `data_loader._signal_columns` returns the signal columns in DuckDB
  ordinal order, which equals `SIGNAL_COLUMNS` because they were
  inserted in that order by `scripts/dev/build_signals.py`.
- The metadata file's signal section matches `SIGNAL_COLUMNS` 1:1.

**No naming aliases or drift detected.** Numeric context column names
match `core.ml_game_level` schema, surface dummies match
`SURFACE_DUMMIES`, signal names match `SIGNAL_COLUMNS`, Markov name
matches the column ALTER'd in by `build_markov_feature.py`.

---

## 3. Markov engine API

⚠️ **Two `markov_engine.py` files exist and they have different signatures.** The one
that produced the values the model was trained on is **not** in the
main branch — it lives only in `.claude/worktrees/friendly-hawking-ffe42f/src/markov_engine.py`.
This is the most important finding in this recon; treat it as load-bearing.

### 3a. `src/engine/markov_engine.py` (in main, used by `src/baseline.py`)

```python
@lru_cache(maxsize=None)
def game_win_prob(p: float, points_server: int = 0,
                  points_receiver: int = 0) -> float

@lru_cache(maxsize=None)
def tiebreak_win_prob(p: float, q: float,
                      points_server: int = 0,
                      points_receiver: int = 0) -> float

@lru_cache(maxsize=None)
def set_win_prob(p: float, q: float,
                 games_server: int = 0,
                 games_receiver: int = 0) -> float
```

- `p`, `q` are already game-level probabilities, **from the set
  server's perspective**: `p = game_win_prob(p_hat_server)`,
  `q = 1 - game_win_prob(p_hat_receiver)`.
- Score state is `(games_server, games_receiver)` — also from the set
  server's perspective.
- Returns P(set server wins set).
- No standalone `clear_set_cache` is defined in this file; the
  `lru_cache`s on `set_win_prob`, `tiebreak_win_prob`, `game_win_prob`
  must be cleared individually (or via `set_win_prob.cache_clear()`).

### 3b. `.claude/worktrees/friendly-hawking-ffe42f/src/markov_engine.py` (used by `build_markov_feature.py` — the one that wrote `markov_set_win_prob_A`)

```python
def game_win_prob(p: float) -> float                       # closed form
@lru_cache(maxsize=None)
def tiebreak_win_prob(p_a: float, p_b: float,
                      first_server_is_a: bool) -> float
def clear_set_cache() -> None                              # clears _SET_CACHE
def set_win_prob(p_a: float, p_b: float,
                 games_a: int, games_b: int,
                 next_server_is_a: bool) -> float          # manual memo dict
```

- `p_a`, `p_b` are raw **point-win-on-own-serve** probabilities (career
  p0 values) — **not** game-level reframed.
- Score state is `(games_a, games_b)` from **A's perspective**, with a
  `next_server_is_a` flag because A may or may not serve the next game.
- Returns P(A wins the set), directly.
- `clear_set_cache` empties the hand-rolled `_SET_CACHE` dict.
- `p` is clamped to `[0.35, 0.85]` inside `game_win_prob`.

### Concerns
- The metadata-bound `markov_set_win_prob_A` column was populated by
  the worktree's 5-arg, A-perspective `set_win_prob`. So the live
  service must replicate that signature, not the in-main 4-arg
  server-perspective one in `src/engine/`.
- The worktree's `src/markov_engine.py` is **not yet promoted** to
  main. Building the live service requires either promoting it (out
  of scope here, but flag-worthy) or vendoring the exact closed-form
  math inside the live module.

---

## 4. Markov call pattern at training time

### 4a. `src/baseline.py` — `predict_soft_markov` (uses the 4-arg engine)

Reads the first row of each `(match_id_int, set_number)` group and
evaluates set-win prob from 0-0 via a wrapper that handles
A↔server reframing:

```python
def _set_win_prob_a(p_a: float, p_b: float, first_server_is_a: bool) -> float:
    g_a = game_win_prob(p_a)
    g_b = game_win_prob(p_b)
    if first_server_is_a:
        p = g_a
        q = 1.0 - g_b
        return set_win_prob(p, q, 0, 0)
    p = g_b
    q = 1.0 - g_a
    return 1.0 - set_win_prob(p, q, 0, 0)        # symmetry: 1 - P(B wins)
```

Per-set call ([src/baseline.py:97-107](src/baseline.py:97)):

```python
p_a = p0_lookup[first_row["player_A"]]
p_b = p0_lookup[first_row["player_B"]]
first_server_is_a = bool(first_row["server_was_A"])
prob = _set_win_prob_a(p_a, p_b, first_server_is_a)
out[group.index.to_numpy()] = prob   # broadcast to every row in the set
```

`predict_soft_markov` evaluates **only at 0-0** — it does not advance the
score within a set. The same value is broadcast to every row in the
set.

### 4b. `build_markov_feature.py` — the actual writer of `markov_set_win_prob_A`

Per-row call ([scripts/dev/build_markov_feature.py:115-117 in the worktree](.claude/worktrees/friendly-hawking-ffe42f/scripts/dev/build_markov_feature.py:115)):

```python
next_server_is_a = not bool(server_was_a)
prob = set_win_prob(p0_a, p0_b, int(games_a), int(games_b), next_server_is_a)
```

Critical conventions:

1. **Per-row, not per-set.** Every row in `core.ml_game_level` gets its
   own Markov probability computed at that row's `(games_a, games_b)`.
2. **Score-state convention.** `games_a` / `games_b` come straight off
   the ml_game_level row — which is the **post-game** count (see §7).
   So Markov is evaluated at the score **after** the row's game ended.
3. **`next_server_is_a = not server_was_a`.** `server_was_A` flags
   who served the game **just completed** by this row. So whoever
   served this game does **not** serve the next one — that's the
   `not …` flip.
4. **`p0_a`, `p0_b`** are looked up from the per-match `p0_map`,
   falling back to `league_avg_p0` for unknown players
   ([line 110-111](.claude/worktrees/friendly-hawking-ffe42f/scripts/dev/build_markov_feature.py:110)).
5. **Cache clear between matches.** `clear_set_cache()` is called
   whenever `match_id` changes ([line 109](.claude/worktrees/friendly-hawking-ffe42f/scripts/dev/build_markov_feature.py:109)).
   In live, we should clear on every new match too.

### Concerns
- `baseline.predict_soft_markov` and `build_markov_feature` produce
  **different values** for the same row when `(games_a, games_b) != (0, 0)`.
  The former is a per-set constant; the latter advances with the score.
  The model's feature column is the per-row, score-advancing one.
- `next_server_is_a = not server_was_a` is correct because
  `server_was_A` describes the just-finished game and serve alternates
  game-to-game **within a set**. At set boundaries serve also flips
  game-by-game ordinally, so this still holds across the set-clinching
  → set-1-game-1 boundary (the set-1-game-1 row's `server_was_A` will
  carry that fact already).

---

## 5. P0 lookup at training time

### 5a. `src.baseline.load_p0_lookup`

```python
def load_p0_lookup(db_path: str = "data/processed/tennis.duckdb") -> dict[str, float]:
    """Return ``{player_name: p0}`` from core.player_p0."""
    rows = con.execute("SELECT player_name, p0 FROM core.player_p0").fetchall()
    return {name: float(p0) for name, p0 in rows}
```

Returns a flat `dict[str, float]` — no surface awareness, no
recency-weighting, just one career p0 per name. **628 entries** in the
current build.

### 5b. `p0_engine.compute_p0_table` (worktree only)

`.claude/worktrees/friendly-hawking-ffe42f/src/p0_engine.py` — 83 lines.

Per-player p0 is `serve_points_won / serve_points_played` accumulated
from both winner and loser sides of `core.atp_matches`. Matching
strategy:

1. **Exact match** by name → `match_method = 'direct'`.
2. Otherwise, `rapidfuzz.process.extractOne(name, atp_name_list,
   scorer=fuzz.ratio, score_cutoff=90)` → `match_method = 'fuzzy'`.
3. Otherwise, `p0 = league_avg_p0`, `match_method = 'league_average'`.

Distribution in the current build (`markov_build_report.txt`):

| method | count |
|---|---|
| direct | 532 |
| fuzzy | 3 |
| league_average | 93 |

### 5c. League-average p0

Computed as `total_serve_points_won / total_serve_points_played`
across **all** ATP players found in `core.atp_matches`:

```
League-average p0: 0.6266
```

This is also the constant that gets stored as the `p0` value for the
93 unknown players (whose `serve_points_played`/`won` are NULL and
whose `match_method = 'league_average'`).

### Concerns
- `p0_engine.py` is **not in main** — it lives in the worktree. The
  live service either needs that module promoted or has to read p0
  values directly from `core.player_p0` (preferred — the table is
  already built and indexed by `player_name PRIMARY KEY`).
- The `core.player_p0` table holds 628 rows, all of them players that
  appeared in `core.ml_game_level`. Live matches may feature names
  **not in this table at all** (e.g. a young pro who only debuted
  after the build window) — the table itself doesn't carry a
  league_average fallback for arbitrary unseen names. Live code must
  re-implement the exact → fuzzy → league_avg fallback against the
  existing table, or default to `0.6266` for unknowns.

---

## 6. Feature vector assembly at training time

Single chokepoint: `src/data_loader.py::load_ml_data`.

### 6a. Column order

`feature_names = NUMERIC_CONTEXT + SURFACE_DUMMIES + signal_cols + ["markov_set_win_prob_A"]`
where:

```python
NUMERIC_CONTEXT = ["games_A", "games_B", "set_number", "sets_won_A",
                   "sets_won_B", "game_number_in_set"]
SURFACE_CATEGORIES = ["hard", "clay", "grass", "unknown"]
SURFACE_DUMMIES = [f"surface_{s}" for s in SURFACE_CATEGORIES]
signal_cols = <52 signal columns in DuckDB ordinal order>
```

### 6b. Preprocessing

```python
X = df[feature_names].to_numpy(dtype=np.float64)
```

No scaling, no clipping, no imputation. NaN handling is a hard fail —
`load_ml_data` raises `AssertionError` if any NaN slips through
([data_loader.py:117-124](src/data_loader.py:117)). Booleans are cast to
int64 (one-hot path) or preserved as DuckDB-converted values; the
six numeric-context columns are integers in DuckDB and arrive as
floats via `dtype=np.float64`.

### 6c. Surface one-hot encoding

```python
surface_lower = df["surface"].astype(str).str.lower()
unknown_surfaces = set(surface_lower.unique()) - set(SURFACE_CATEGORIES)
if unknown_surfaces:
    raise RuntimeError(...)
for cat in SURFACE_CATEGORIES:
    df[f"surface_{cat}"] = (surface_lower == cat).astype(np.int64)
```

- All four categories produce a column; exactly one is `1` per row.
- Lower-cased before comparison.
- Anything that isn't `{hard, clay, grass, unknown}` raises — but
  `unknown` is already a real bucket (see §8), so live code can safely
  collapse missing-surface → `'unknown'` and let the one-hot land in
  `surface_unknown`.

### 6d. Train/test split

```python
"split": EXTRACT(YEAR FROM match_date) BETWEEN 2015 AND 2023 → 'train'
                                       BETWEEN 2024 AND 2025 → 'test'
```

(`build_ml_game_level.py:330-333`). Live inference does not use the
`split` column — the live service builds an X-row directly from the
streaming state.

---

## 7. Score-state column conventions — CRITICAL EMPIRICAL SECTION

`build_ml_game_level.py` derives `games_A/B` and `sets_won_A/B`
differently:

```sql
-- games_A: cumulative wins INCLUDING this row's game
SUM(CASE WHEN game_winner_int = 1 THEN 1 ELSE 0 END)
  OVER (PARTITION BY match_id, set_number ORDER BY game_number_in_set
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS games_A

-- sets_won_A: cumulative wins EXCLUDING this row's set
COALESCE(SUM(CASE WHEN set_winner_is_A = 1 AND is_complete THEN 1 ELSE 0 END)
  OVER (PARTITION BY match_id ORDER BY set_number
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS sets_won_A
```

So the convention is **asymmetric**:

- `games_A` / `games_B` = score in the current set **after** this row's
  game completed (i.e. post-game).
- `sets_won_A` / `sets_won_B` = sets won **before** this set started.
  Even on the set-clinching game's row, this value still excludes the
  set being clinched.

### 7a. Empirical: clean two-set match (6-3, 6-2), `match_id_int = 1000003931`

| set | g# | gA | gB | sA | sB | swA | tb | svA | winA | narrative |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0 | 0 | 0 | 1 | F | T | T | A holds → 1-0 |
| 1 | 2 | 1 | 1 | 0 | 0 | 1 | F | F | F | B holds → 1-1 |
| 1 | 3 | 2 | 1 | 0 | 0 | 1 | F | T | T | A holds → 2-1 |
| 1 | 4 | 3 | 1 | 0 | 0 | 1 | F | F | T | A breaks B → 3-1 |
| 1 | 5 | 4 | 1 | 0 | 0 | 1 | F | T | T | A holds → 4-1 |
| 1 | 6 | 4 | 2 | 0 | 0 | 1 | F | F | F | B holds → 4-2 |
| 1 | 7 | 5 | 2 | 0 | 0 | 1 | F | T | T | A holds → 5-2 |
| 1 | 8 | 5 | 3 | 0 | 0 | 1 | F | F | F | B holds → 5-3 |
| 1 | 9 | **6** | **3** | **0** | **0** | 1 | F | T | T | **A holds, takes set 6-3; `sets_won_A` still 0 here** |
| 2 | 1 | 1 | 0 | **1** | 0 | 1 | F | F | T | **first row of set 2 — `sets_won_A` now 1** |
| 2 | 2 | 2 | 0 | 1 | 0 | 1 | F | T | T | A holds → 2-0 |
| 2 | 3 | 2 | 1 | 1 | 0 | 1 | F | F | F | B holds → 2-1 |
| 2 | 4 | 3 | 1 | 1 | 0 | 1 | F | T | T | A holds → 3-1 |
| 2 | 5 | 4 | 1 | 1 | 0 | 1 | F | F | T | A breaks → 4-1 |
| 2 | 6 | 5 | 1 | 1 | 0 | 1 | F | T | T | A holds → 5-1 |
| 2 | 7 | 5 | 2 | 1 | 0 | 1 | F | F | F | B holds → 5-2 |
| 2 | 8 | 6 | 2 | 1 | 0 | 1 | F | T | T | A holds, takes set 6-2 (match) |

**Confirms:** `games_A/B` is post-game, `sets_won_A/B` is pre-set.
The set-clinching row (set 1, game 9) reads `gA=6, gB=3, swA=0` — sets
won has not yet incremented even though A just clinched. Only on the
first row of the next set does `sets_won_A` jump to 1.

### 7b. Empirical: tiebreak in a three-set match, `match_id_int = 1000002485`

| set | g# | gA | gB | sA | sB | swA | tb | svA | winA |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0 | 0 | 0 | 0 | F | T | T |
| … | … | … | … | … | … | … | … | … | … |
| 1 | 10 | 4 | 6 | 0 | 0 | 0 | F | F | F | (B clinches set 1, 6-4) |
| 2 | 1 | 0 | 1 | 0 | 1 | 1 | F | T | F | sets_won_B now 1 |
| 2 | 11 | 6 | 5 | 0 | 1 | 1 | F | T | T |
| 2 | 12 | 6 | 6 | 0 | 1 | 1 | F | F | F | **tied 6-6 → tiebreak next** |
| 2 | **13** | **7** | **6** | 0 | 1 | 1 | **T** | T | T | **tiebreak row: gA=7, gB=6, is_tiebreak=True** |
| 3 | 1 | 0 | 1 | 1 | 1 | 0 | F | F | F | set 3 begins; both `sets_won` = 1 |
| … | … | … | … | … | … | … | … | … | … |

**Tiebreak row encoding:**
- `set_number` = the set the tiebreak settles (here 2).
- `game_number_in_set` = 13 (the "13th game" is the tiebreak by convention).
- `games_A`, `games_B` = 7-6 (or 6-7) — the tiebreak is counted as a
  game-win that produces the final 7-6 set score.
- `is_tiebreak` = True.

### 7c. Empirical: three-set match, set-boundary reset

From the same match (`1000002485`), comparing the last row of each set
to the first row of the next:

| transition | last-row state | first-row state |
|---|---|---|
| set 1 → set 2 | `s=1 g=10 gA=4 gB=6 swA=0 swB=0` | `s=2 g=1 gA=0 gB=1 swA=0 swB=1` |
| set 2 → set 3 | `s=2 g=13 gA=7 gB=6 swA=0 swB=1` (TB) | `s=3 g=1 gA=0 gB=1 swA=1 swB=1` |

**Confirms reset rules:**
- `games_A` / `games_B` **reset to reflect only the current set** — the
  set-1-game-1 row's values (1, 0) describe just the first game's
  outcome.
- `sets_won_A` / `sets_won_B` **increment between sets** — the
  game-1-of-set-2 row's `sets_won_B = 1` accounts for the just-finished
  set 1 that B won.

### Plain-English convention for the live tracker

After the streaming engine emits a row for game *G* in set *S*:

- `games_A`, `games_B`: the score in set *S* **including** game *G*.
- `set_number = S`, `game_number_in_set = G`.
- `sets_won_A`, `sets_won_B`: sets fully completed **before** set *S*
  began. If game *G* itself clinches set *S*, `sets_won_A` / `sets_won_B`
  do **not** yet reflect that win — only on the first row of set *S+1*
  do they pick up the increment.
- For the tiebreak game: `game_number_in_set = 13`, the games_A/B pair
  is the 7-6 (or 6-7) tally, and `is_tiebreak = True`.

### Concerns
- The asymmetric convention is easy to get wrong. The live service's
  in-memory score tracker should expose two separate counters:
  `games_in_current_set` (advances each game, resets on set end) and
  `sets_already_complete` (only increments after the set-clinching
  game has been emitted, i.e. when the next set's first game starts).
- For Markov, **§4's `next_server_is_a = not server_was_a` only holds
  because every row's `server_was_A` describes the just-finished
  game and serve alternates strictly each game.** Live code that
  tracks "who is about to serve" must keep that fact consistent with
  the post-game row it emits.

---

## 8. Surface field

### 8a. Distinct values in `core.ml_game_level.surface`

```
('clay',), ('hard',), ('unknown',), ('grass',)
```

Four buckets, lower-case. `'unknown'` is the explicit fallback applied
inside `build_ml_game_level.py:286-290` when surface is missing,
mismatched, or `'carpet'`:

```sql
COALESCE(
    CASE WHEN sl.surface IN ('hard','clay','grass','carpet')
         THEN sl.surface ELSE NULL END,
    'unknown'
) AS surface
```

So **carpet matches are nominally captured but rewritten to NULL → 'unknown'**,
and any match whose nearest-date `core.atp_matches` join failed is also
`'unknown'`.

### 8b. One-hot column names

`SURFACE_DUMMIES = ["surface_hard", "surface_clay", "surface_grass", "surface_unknown"]`
in that order ([data_loader.py:35-36](src/data_loader.py:35)). Each row
has exactly one `1` and three `0`s.

### 8c. Live behaviour for unknown surface

Live code should resolve incoming surface strings to lower-case and
test membership against `{hard, clay, grass}`. Anything else (missing,
carpet, indoor, anything new) → `surface_unknown = 1` while the other
three are `0`. This matches training exactly.

---

## 9. P0 table schema

`core.player_p0`:

| column | type | null | key |
|---|---|---|---|
| `player_name` | VARCHAR | NO | PRI |
| `p0` | DOUBLE | NO | |
| `serve_points_played` | BIGINT | YES | |
| `serve_points_won` | BIGINT | YES | |
| `match_method` | VARCHAR | YES | |

Row count: **628**. Method breakdown: 532 direct, 3 fuzzy, 93 league_average.

Sample rows:

```
('David Ferrer',        0.6259, 82556, 51670, 'direct')
('John Isner',          0.7169, 69030, 49487, 'direct')
('Alejandro Gonzalez',  0.5754,  4140,  2382, 'direct')
('Thiemo De Bakker',    0.6323, 11325,  7161, 'direct')
('Jordan Thompson',     0.6352, 24016, 15255, 'direct')
```

Aggregate stats: `AVG(p0)=0.6161`, `MIN=0.4286`, `MAX=0.7288`, `n=628`.
The league-average constant the build script uses for unknowns is
`0.6266` (slightly different from the average of stored p0s — it's
computed over total serve points across the whole `atp_matches` table,
not as the mean of the per-player p0s).

---

## 10. Open questions and concerns

### 10a. Cold start (no row before the first game ends)

`StreamingMatchState.process_point` returns `None` until the first
point of game 2 arrives. So strictly mid-game-1 there is no emitted
signal row to feed the model.

Options for the live service:

1. **Wait until game 1 closes before emitting any prediction.** This
   is the safest and what training implicitly assumes — every row in
   `core.ml_game_level` has at least one closed game behind it.
2. **Emit a "pre-match" prediction using only the Markov leg.** The
   service can compute `markov_set_win_prob_A` at `(0, 0)` from `p0_a`,
   `p0_b`, and `next_server_is_a` before the first point — but the 52
   signal features and the score-context block would all be zero or
   undefined. The model has never been trained on rows like this; any
   prediction is extrapolation.

Recommendation: gate predictions on "at least one signal row has been
emitted." Surface a `MatchState.has_first_game_closed()` (or equivalent)
on the streaming wrapper so the live service knows when it's safe to
call the model.

### 10b. Mid-set recovery / partial state

If the live service restarts mid-match, the per-point signal
accumulators in `StreamingMatchState` are gone. Two reconstruction
paths exist, neither clean:

- **Full replay** from the persisted point log if one exists. This is
  the only path that produces signals byte-identical to training.
- **Bootstrap from current score only** — set `games_A/B`, `sets_won_A/B`,
  `set_number`, `game_number_in_set`, `surface_*`, Markov from p0 +
  current score, and **leave the 52 signal columns at their default-init
  values (mostly zero, except smoothed BPI denominators which default
  to `0 / (0+2) = 0`)**. This is what a fresh `_PlayerAcc()` would
  produce. The model has never seen such rows in training, so
  predictions on them are extrapolation.

Recommend the live service surface both options explicitly and log
which one is active.

### 10c. Unknown players

`core.player_p0` covers 628 specific names. Live matches will
inevitably feature players outside that set (and ATP rosters refresh
faster than the build window). Replicate `p0_engine`'s three-tier
lookup live:

1. Exact match on `player_name`.
2. `rapidfuzz` fuzzy match against the 628 names, threshold 90 on
   `fuzz.ratio`.
3. League-average fallback `0.6266`.

Cache the resolved p0 per match so the lookup runs once per player
per match, not per point.

### 10d. Markov source-of-truth divergence

The 4-arg `set_win_prob` in `src/engine/markov_engine.py` (used by
`baseline.predict_soft_markov`) and the 5-arg `set_win_prob` in the
worktree's `src/markov_engine.py` (used by `build_markov_feature.py`,
which wrote the column the model trained on) **are different
functions**. They are not interchangeable. The live service must
import / vendor the worktree version to produce values comparable to
training. This is the single biggest correctness risk in the build.

### 10e. `markov_set_win_prob_A` clamping behaviour

The worktree `game_win_prob` clamps `p` into `[0.35, 0.85]` before
evaluating. So players with extreme p0s outside that band (the table
has min=0.43, max=0.73, so none currently extreme) are squeezed.
Live code must apply the same clamp or values will diverge.

### 10f. `lru_cache` lifecycle

The worktree's `set_win_prob` is **not** decorated with `lru_cache` —
it uses a hand-rolled `_SET_CACHE` dict and a separate
`clear_set_cache()`. The worktree's `tiebreak_win_prob` and `_tb`
**are** `lru_cache`-decorated, and `clear_set_cache()` does **not**
clear those caches. Across many matches the tiebreak lru-caches will
grow unbounded but converge on a finite set of `(p_a, p_b,
first_server_is_a)` keys, so memory pressure should be modest. Worth
a one-line guard in live code that snapshots cache sizes and warns if
growth is unexpected.

### 10g. Implicit assumption: serve alternates every game

§4's "`next_server_is_a = not server_was_a`" is correct **only** for
points-tennis where serve strictly alternates game-by-game. The live
adapter must enforce that the upstream feed never delivers a row where
that invariant is broken (e.g. retired-mid-game, doubles, a feed bug
that misattributes a serve). A simple sanity check on every emitted
row — "did `server_was_A` flip from the previous emitted row?" — would
catch this.
