# Markov / P0 file promotion

Promoted three of the four Step 4 files from
`.claude/worktrees/friendly-hawking-ffe42f/` into their canonical main
locations. The fourth (`tests/test_markov_engine.py`) was **skipped**
due to a destination conflict — see "Anomalies" below.

## Files copied

| Source (worktree) | Destination (main) | Bytes |
|---|---|---|
| `.claude/worktrees/friendly-hawking-ffe42f/src/markov_engine.py` | [src/markov_engine.py](src/markov_engine.py) | 3156 |
| `.claude/worktrees/friendly-hawking-ffe42f/src/p0_engine.py` | [src/p0_engine.py](src/p0_engine.py) | 2603 |
| `.claude/worktrees/friendly-hawking-ffe42f/scripts/dev/build_markov_feature.py` | [scripts/dev/build_markov_feature.py](scripts/dev/build_markov_feature.py) | 6632 |

## Import verification

```
$ .venv/bin/python -c "from src.markov_engine import set_win_prob, game_win_prob, tiebreak_win_prob, clear_set_cache; print('markov ok')"
markov ok
$ .venv/bin/python -c "from src import p0_engine; print('p0 ok')"
p0 ok
```

Both imports resolve. No hidden dependency on other worktree-only
modules.

## `tests/test_markov_engine.py` result

**19 passed in 0.06s.** This is the **existing** legacy test file in
main, which targets `src/engine/markov_engine.py` (the 4-arg API used
by `src/baseline.py`). It was not replaced — see "Anomalies."

The reference guide's "10/10" expectation applies to the worktree's
own `tests/test_markov_engine.py`, which targets the new
`src/markov_engine.py` (5-arg A-perspective API). That file was not
promoted; the new module currently has no test coverage in main.

## Full test suite

`.venv/bin/python -m pytest tests/`

**4 failed, 439 passed, 12 skipped, 2346 warnings in 57.49s.**

Failures:

| Test | Status | Touches promoted modules? |
|---|---|---|
| `tests/test_signal_engine.py::test_column_count` | pre-existing, called out in prompt | no |
| `tests/test_scheduler.py::test_mutation_shrinkage_resets_engine` | pre-existing | no |
| `tests/test_scheduler.py::test_mutation_changed_history_resets_engine` | pre-existing | no |
| `tests/test_scheduler.py::test_stable_history_does_not_reset_engine` | pre-existing | no |

- `test_column_count` — known stale fixture (`EXPECTED_TOTAL_COLS = 80`
  vs actual 81 from `markov_set_win_prob_A`). Already flagged in the
  prompt as expected.
- The three scheduler failures fail with `AttributeError: 'MatchWorker'
  object has no attribute '_terminal_non_finished'` in
  `src/live/collector.py:137`. Neither file imports anything from the
  promoted modules; `grep` for `markov_engine | p0_engine |
  build_markov_feature` against `tests/test_scheduler.py`,
  `src/live/collector.py`, `src/live/*.py` returns nothing. These
  failures are unrelated pre-existing breakage in the live-collector
  code path.

No new failures attributable to this promotion.

## Anomalies

**Conflict on `tests/test_markov_engine.py`.** The destination already
existed in main (4108 bytes, dated Apr 23). Contents differ from the
worktree file:

| File | Imports |
|---|---|
| main `tests/test_markov_engine.py` (kept) | `from src.engine.markov_engine import game_win_prob, tiebreak_win_prob, match_win_prob, compute_live_probabilities, GAME_TABLE, TIEBREAK_TABLE` |
| worktree `tests/test_markov_engine.py` (skipped) | `from src.markov_engine import ...` |

These are two different test files for two different modules — the
legacy 4-arg engine at `src/engine/markov_engine.py` (still used by
`src/baseline.py`) and the newly promoted 5-arg engine at
`src/markov_engine.py`. Per the prompt's "STOP and report which one.
Do not proceed with that file." rule, the worktree's test file was
**not** copied.

Consequence: the newly promoted `src/markov_engine.py` ships into main
without its own test coverage. The worktree test file is still
available at
`.claude/worktrees/friendly-hawking-ffe42f/tests/test_markov_engine.py`
and could be promoted under a different name (e.g.
`tests/test_markov_engine_v2.py` or `tests/test_set_win_prob_a.py`) in
a follow-up if desired.

No commits, pushes, or `.gitignore` changes made. The
`scripts/dev/build_markov_feature.py` script was promoted but **not
executed** — `core.ml_game_level.markov_set_win_prob_A` remains
untouched.

## Test file follow-up

The previously-skipped worktree test file was promoted under a
non-colliding name so both test surfaces (legacy and canonical) live
in main side-by-side.

| Source (worktree) | Destination (main) | Bytes |
|---|---|---|
| `.claude/worktrees/friendly-hawking-ffe42f/tests/test_markov_engine.py` | [tests/test_markov_engine_canonical.py](tests/test_markov_engine_canonical.py) | 3841 |

Naming rationale: the new `src/markov_engine.py` is the canonical
5-arg engine (matches `build_markov_feature.py` and the training-data
column). The legacy `src/engine/markov_engine.py` is older and
narrower in scope. The `_canonical` suffix keeps the new engine's
tests discoverable without colliding with the legacy file.

### Canonical test result

`.venv/bin/python -m pytest tests/test_markov_engine_canonical.py -v`

**10 passed in 0.11s.** Matches the reference guide expectation.

```
test_g_sanity                         PASSED
test_g_clipping                       PASSED
test_t_symmetry                       PASSED
test_set_terminal                     PASSED
test_set_symmetry                     PASSED
test_set_complementary                PASSED
test_p0_top_server_high               PASSED
test_p0_league_average_in_range       PASSED
test_p0_fuzzy_match_count_positive    PASSED
test_markov_column_complete           PASSED
```

### Legacy test regression check

`.venv/bin/python -m pytest tests/test_markov_engine.py -v`

**19 passed in 0.05s.** No regression; the legacy 4-arg engine still
fully tested.
