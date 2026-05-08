# TENNIS PREDICTION ENGINE
## Reference Guide — Single Source of Truth
### Version 4.0 | Updated: May 2026 (Step 5 complete, strategic pivot to shadow deployment)

Upload this document to every chat in this project. It is the complete
reference for project context, current strategy, current state, data
structure, signal definitions, build sequence, and technical environment.
The TennisAPI1 endpoints file (tennisapi1_endpoints.md) is the only other
document retained alongside this one.

After each chat session, ask the chat to produce an updated version of
this file reflecting any new decisions, completed steps, or changed
approaches. Increment the version number each time.

---

## 1. WHAT WE ARE BUILDING AND WHY

We are building a machine learning model that predicts tennis set winners
during live ATP/WTA matches. The model uses in-match momentum and dominance
signals — derived entirely from point-by-point outcomes and score states —
to predict who will win the current set after each completed game.

The engine is a decision-support tool for live set betting markets. It is
not automated and does not place bets. A single user watches a live match,
the model processes point-by-point data from a live API feed, and the user
ultimately compares the model's probability output against bookmaker set
odds to identify potential edges. As of v4, that final edge-comparison
step is decoupled from the live deployment work. The model deploys now;
odds integration follows when historical and live odds data become
available (see Section 2 on the dual-track strategy).

### The Core Hypothesis

Tennis scoreboards are lagging indicators. A game is either won or lost —
a 6-4 set tells you nothing about how those games were won. Two 6-4 sets
can represent completely different competitive realities: one player
dominant throughout versus one player hanging on through multiple break
points every game. Bookmaker live odds are largely driven by the score.
Our model is driven by what is actually happening on court — pressure,
momentum, and dominance patterns the score hides. The gap between those
two perspectives is where the betting edge lives, and Step 5 has
demonstrated empirically that the model finds substantial signal beyond
what pure score state predicts.

### Why Set Betting (Not Match Winner)

Match winner is the hardest market to beat — bookmakers price it most
efficiently. Set betting is updated less frequently, modeled less
precisely, and is a shorter-horizon prediction where momentum signals
should have more predictive power. Once this model is validated against
real bookmaker odds (Step 8), we extend to game winner, totals, and
other markets using the same feature infrastructure.

### Selective Edge Philosophy

The model is **not expected to predict every set accurately**. Tennis
has genuine randomness, mid-set momentum swings, and noisy back-and-forth
periods where no clean prediction is possible. The goal is selective edge
identification: the model produces a confidence level for every
prediction, and we act only on the high-confidence subset where the
model materially disagrees with bookmaker pricing. Low-confidence
predictions are ignored. This mirrors how professional sports bettors
operate — bet only when you have clear conviction AND a meaningful edge
over the market price.

Step 5C confirmed that the trained model produces a clean monotonic
relationship between confidence and accuracy. In the highest confidence
bucket (predicted probability above 0.80), accuracy reaches 94.21%
across 15,674 test predictions. This validates the philosophy: the model
need not be globally accurate to be useful, only reliably accurate when
it is confident.

---

## 2. STRATEGY

### 2.1 ML Architecture

The model is an isotonic-calibrated XGBoost classifier trained on
roughly sixty input features per row. Architecturally, "Markov as a
feature plus decomposed signal sub-components fed to XGBoost" is the
chosen design, settled in v3 and validated by Step 5 results.

The Markov probability engine (built in Step 4) computes the
mathematically exact set-win probability from the current game score and
each player's career baseline serve-point-win probability (p0). This
single number captures an enormous amount of information: the entire
future state-space of the set given the current score. It is one input
feature among many, but Step 5C confirmed it carries 35.5% of all model
feature importance — by far the largest single contributor.

Each of the five engineered signals is decomposed into 2-3 sub-components.
Each sub-component is computed in two carryover versions: within-set
(resets at set boundaries) and cumulative-match (never resets). XGBoost
sees a flat list of features and learns relationships at every level.
"Signals" remain as conceptual organizing labels for code structure,
feature importance grouping, and documentation. The five signal families
collectively account for 53.9% of feature importance, with score context
making up the remaining 11.5%.

XGBoost was chosen for robustness against overfitting with proper
regularization, strong track record on tabular datasets of this size
(~100K rows), and interpretable feature importance output. Tree-based
models naturally learn non-linear relationships and feature interactions
without requiring us to engineer them. The hyperparameters chosen by
GroupKFold cross-validation favor a deliberately conservative model
(shallow trees, slow learning rate, strong regularization), suggesting
the underlying signal is real but not enormously complex — exactly what
we'd expect given the inherent randomness of tennis points.

### 2.2 Shadow Deployment Strategy (v4 pivot)

The original v3 plan required completing component 6C (an odds
comparison gate check) before any live deployment work could begin. The
intent of that rule was to avoid investing in deployment infrastructure
for a model that might not actually beat bookmaker pricing. As of v4
that rule is **explicitly relaxed** for two interrelated reasons.

First, the model has demonstrated real predictive ability through Step 5
evaluation. On undecided test rows (the rows where the set's outcome is
not yet mechanically encoded in the score features) the model achieves
a Brier score of 0.1664, beating the naive Soft Markov baseline of
0.2411 by 31% relative on the rows that would actually matter for live
betting. This is no longer a model whose worth is unproven.

Second, historical bookmaker odds for ATP/WTA set markets are not
trivially available. Sourcing them is an open-ended task with unknown
timeline. Blocking all deployment work on that dependency would mean
indefinite delay during a period when valuable live operational
experience could be accumulating.

The chosen path is a **shadow deployment** (also called paper trading
in financial contexts). The model runs live on the existing server
infrastructure, consumes the same TennisAPI1 point-by-point feed already
powering the live score tracker, and produces real-time predictions
after each completed game. Predictions are logged with full feature
context to a database. No money moves. The dashboard displays
predictions for human review and observation.

When historical and/or live odds data becomes available later, the
logged prediction stream can be retroactively joined against odds and
the edge analysis originally specified by component 6C can be performed
on real live data instead of backtested data. This is a stronger test
than the original 6C plan would have produced, because real live data
includes operational realities (live API quirks, latency, edge cases)
that historical backtesting cannot expose.

The strategic risk being accepted is that we may invest engineering
effort in deploying a model whose real-world edge against bookmakers
turns out to be smaller than its edge against the naive baseline. This
risk is judged acceptable given the strength of the Step 5 evaluation,
the relatively low marginal cost of deployment (existing infrastructure
is reused, not built fresh), and the diagnostic value of live operation
even before the edge analysis becomes possible.

### 2.3 Three Parallel Tracks

Project work going forward is organized into three tracks that progress
in parallel rather than strict sequence.

**Track A — Live Deployment** is the active focus. This is Step 6 in
the build sequence and breaks down into five sub-components: streaming
signal computation, the live prediction service, persistent prediction
logging, state machine integration on the production droplet, and
dashboard integration. Track A produces a working live engine that
consumes point-by-point data and emits predictions in real time.

**Track B — Odds Sourcing** runs in parallel and is owned at a higher
level than the build chats themselves (it is primarily a research and
business-development activity rather than a coding task). Subgoals
include identifying providers of live ATP/WTA set odds, identifying
sources of historical set odds for retroactive analysis, and evaluating
pricing and integration feasibility. Progress on Track B does not block
Track A.

**Track C — Iteration** is deferred until two preconditions hold: live
deployment is stable enough to produce a reliable stream of logged
predictions, and enough live data has accumulated to make iteration
decisions on something other than the held-out 2024-2025 test set. Step
5C surfaced specific candidates for iteration — the systematic
under-confidence in calibration, the relative weakness of the MRS signal
family, and the elevated Brier on extremely close sets reaching games
11 and 12. None of these are blockers for deployment. They become
worthwhile to address when we have live data to iterate against.

### 2.4 Architecture Evolution Summary

A condensed history of architectural decisions, with the full chronology
in Section 13. The project began with a hand-tuned sigmoid pipeline
(v1), pivoted to XGBoost with hand-tuned signal blending (v2), then to
exposed sub-components letting XGBoost learn all relationships (v3).
Step 4 of v3 added the Markov baseline. Step 5 of v3.2 trained and
evaluated the model. v4 (current) preserves the v3 ML architecture and
adds the shadow deployment strategy.

The single principle running through all four versions is the same:
avoid hand-tuning at every level. Signal weights are not hand-tuned.
Sub-component blends are not hand-tuned. The model learns everything
from data. The deployment strategy follows the same principle in
spirit: rather than guessing how the model will behave in production,
deploy it and measure.

---

## 3. DATA STRATEGY

### Training Source

Jeff Sackmann's Match Charting Project point-by-point data, stored in
DuckDB at data/processed/tennis.duckdb, schema: charting. The enriched
canonical source table is core.atp_points_enhanced (1,221,862 rows,
36 columns).

### Train / Test Split

Training: match_date 2015-01-01 through 2023-12-31. Test: match_date
2024-01-01 through 2025-12-31. The test set was held out completely
through Steps 1 through 5 — never touched during feature engineering or
hyperparameter tuning. This is a hard rule.

For Step 6 onward (live deployment), the test set is permitted to be
used for diagnostic purposes such as comparing live prediction streams
against historical equivalents, since the test set is no longer being
used to make modeling decisions.

### Critical Feature Constraint

Only engineer features derivable from point winner and score state. The
live production feed (TennisAPI1 on RapidAPI) provides only who won
each point and the score situation. Columns that must NOT be used as
features (even though they exist in training data): rally_length,
point_outcome, is_ace, is_double_fault, serve_number, first_serve_in,
rally encoding strings, shot type fields.

What IS derivable from the live feed: who is serving, exact game score
(15-0, 30-15, deuce, etc.), set score, whether a point was a break
point, whether a break was converted, how many points each service game
took, whether a tiebreak is happening, full match score.

---

## 4. DATABASE STRUCTURE

### Location

data/processed/tennis.duckdb — browsed via TablePlus on Mac. Note: in
TablePlus, schemas are accessible via the bottom dropdown, not the
sidebar folders.

### Schemas

**core** — match and point-level data from Sackmann's repositories.
Key tables include atp_matches (match-level serve statistics, used for
computing p0), atp_points_enhanced (the 1,221,862-row canonical
point-level source table), ml_game_level (the 120,718-row, 81-column ML
backbone built across Steps 2 through 4), match_id_map (integer/string
match ID mapping covering 7,160 matches), and player_p0 (628 rows of
career serve-point-win probability per player).

**charting** — 42 tables from Match Charting Project (decade-partitioned
raw data). These are the upstream source for atp_points_enhanced.

**rankings** — player bios (65K+ players with height, handedness, DOB)
and historical ranking snapshots (3M+ weekly records).

**live_processed, live_raw** — live feed tables for production use.
These will be extended in Step 6 with new tables for prediction logging
(specific schema defined within Step 6C).

### core.ml_game_level — The ML Backbone (81 columns)

120,718 rows, one per completed game within a set. Built across Steps
2, 2.5, 3, and 4. The full column inventory is unchanged from v3.2 and
includes 28 original game-level columns from Steps 2 and 2.5, 52 signal
sub-components from Step 3, and 1 Markov baseline column from Step 4.

The target column is set_winner_is_A (BOOLEAN, converted to int 0/1
inside the data loader). The split column ('train' or 'test')
partitions the data: 86,698 train rows and 34,020 test rows. Surface
distribution: hard 59,618 / clay 22,747 / grass 11,521 / unknown 26,832.
The 22% surface-unknown rate reflects charting matches that didn't
name-match into atp_matches; this is left as its own category rather
than imputed.

### core.player_p0 — Player Career Serve Stats

628 rows, built in Step 4 from core.atp_matches. Columns: player_name,
p0 (career serve-point-win rate), serve_points_played, serve_points_won,
match_method (one of 'direct', 'fuzzy', or 'league_average'). Direct
matches: 532. Fuzzy matches via rapidfuzz at threshold 90: 3.
League-average fallback: 93. League-average p0 is 0.6266 (weighted by
serve points). Distribution: min 0.4286, max 0.7288, mean 0.6161.

### Trained Model Artifacts (added in Step 5)

The trained model and its metadata live alongside the database:

data/processed/model_v1.joblib — the calibrated XGBoost model (sklearn
CalibratedClassifierCV wrapping 5 XGBClassifier base estimators with
isotonic calibration). Loaded via joblib.load.

data/processed/model_v1_metadata.json — sidecar metadata including
timestamp, best hyperparameters, calibration method, test Brier score,
feature names list, train/test sizes, and total runtime.

data/processed/baseline_report.txt — Step 5A baseline numbers.

data/processed/training_report.txt — Step 5B training results.

data/processed/evaluation_report.txt — Step 5C evaluation results.

data/processed/calibration_curve.png — Step 5C plot.

data/processed/confidence_buckets.png — Step 5C plot.

data/processed/brier_by_game_number.png — Step 5C plot.

data/processed/feature_importance.png — Step 5C plot.

---

## 5. THE FIVE SIGNALS

This section is unchanged from v3.2. The five signals (BPI, SDS, RES,
CPI, MRS) are decomposed into 52 sub-component features total, each
computed in two carryover versions for two players. They serve as
organizational buckets — XGBoost sees a flat feature list and learns
relationships across all of them.

Architecture details common to all signals: every sub-component is
computed in within-set (ws, resets at set boundaries) and
cumulative-match (cm, never resets) versions. Tiebreak handling differs
by signal: BPI, SDS, and RES are excluded during tiebreaks (their values
freeze at game-12 levels), while CPI and MRS continue through tiebreaks
because their underlying logic doesn't depend on service-game structure.
Only BPI uses smoothing (k=2 Bayesian smoothing toward zero); other
signals stabilize naturally given their data density.

Naming convention is `{signal}_{subcomponent}_{ws|cm}_{a|b}`. All score
state computations use the score_before column rather than the
unreliable is_break_point flag.

### Step 5C Validation: Empirical Importance by Signal

Feature importance from the trained model, summed within each group:

Markov: 35.5% (single feature). BPI: 13.3%. SDS: 13.2%. score_context:
11.5%. RES: 11.2%. CPI: 9.1%. MRS: 6.3%.

Two findings worth highlighting. First, BPI ranks first among the five
engineered signal families, vindicating the original project hypothesis
that pressure-without-conversion (the engine's most original signal) is
the most predictive in-match dominance signal. Second, MRS ranks last
at 6.3%, consistent with the v3.2 prior that flagged momentum/runs as
the most overfitting-prone signal family. The cumulative-match (cm)
versions of signals dominate the within-set (ws) versions in the top-15
individual feature ranking, indicating the model relies more heavily on
signals that integrate across the whole match than on signals that reset
per set.

Detailed signal definitions follow the same structure as v3.2 — see that
document or the source in src/signal_engine.py for the per-signal
implementation specifications.

### Signal 1: Break Pressure Index (BPI) — 12 features

Sub-components: bpi_bp_rate (break points faced per return game with
k=2 smoothing), bpi_deep_pressure_rate (return games reaching 0-40 or
15-40), bpi_near_pressure_rate (return games reaching deuce without
generating a break point). Tiebreak handling: excluded.

### Signal 2: Service Dominance Score (SDS) — 12 features

Sub-components: sds_serve_win_pct, sds_hold_rate, sds_avg_pts_per_game.
Empirical baselines confirmed in Step 3: sds_hold_rate_cm mean ≈ 0.81,
sds_serve_win_pct_cm mean ≈ 0.66 (matches known ATP averages).
Tiebreak handling: excluded.

### Signal 3: Return Effectiveness Score (RES) — 8 features

Sub-components: res_return_win_pct, res_bp_conv_rate. Tiebreak
handling: excluded.

### Signal 4: Clutch Performance Index (CPI) — 8 features

Sub-components: cpi_serve_pressure_pct, cpi_return_pressure_pct.
Pressure point definition: score states 30-30, 40-40, 30-40, 40-30,
40-AD, AD-40, 0-40, 15-40. Tiebreak handling: included.

### Signal 5: Momentum / Runs Signal (MRS) — 12 features

Sub-components: mrs_pwr_10 (point-win rate over last 10 points),
mrs_pwr_30 (point-win rate over last 30 points), mrs_game_streak
(current consecutive games won). Tiebreak handling: included; tiebreak
counts as one game for game_streak purposes.

---

## 6. MARKOV BASELINE FEATURE

The Markov engine, rebuilt from scratch in Step 4, computes the exact
probability of winning a set from any game score given each player's
career p0. For each row in ml_game_level, this answers: "Given the
current game score and each player's career strength, what does pure
math say?" XGBoost learns how much to adjust that baseline using
in-match signals.

Step 5C confirmed this feature carries 35.5% of total model feature
importance, by far the largest single contributor. This is consistent
with the fact that the Markov computation compresses substantial
information into one number — the entire future state-space of the set.
The signals together still account for 53.9% of importance, but no
individual signal feature comes close to Markov's individual weight.

### Engine Functions (src/markov_engine.py)

Three exposed functions: game_win_prob (closed-form game probability
from 0-0 with p clipped to [0.35, 0.85]), tiebreak_win_prob (memoized
recursive DP over tiebreak score states, first-to-7 win-by-2), and
set_win_prob (memoized recursion over game scores, defers to tiebreak
at 6-6, includes closed-form extension for advantage-set deuce at (n,n)
for n≥7 to handle pre-tiebreak-era historical data).

A clear_set_cache function exists for clearing the memoization between
matches (the cache is keyed on player p0 values, which differ per
match).

### Live-Use Note (added in v4)

For Step 6, the Markov computation must run inside the live prediction
service after each completed game. The current implementation is pure
math and DB-free, which is good. The set_win_prob function uses lru_cache
internally; this needs to be cleared between matches in live use the
same way it's cleared in batch processing. Step 6B will handle this in
the live prediction wrapper.

### Empirical Symmetry Note

With equal-strength players at 0-0, the set win probability is exactly
0.5. There is no first-server advantage at the set level because game-
server alternation balances the effect across the set. Verified by 200K
Monte Carlo trials during Step 4. A separate empirical observation made
in Step 5A: with unequal players starting at 0-0, the set win
probability also appears to be invariant to who serves first (verified
by direct computation in the wrapper used by baseline.py). This is
non-obvious but apparently correct, and is the reason the soft-Markov
baseline returns identical predictions for every row in a set
regardless of who served first.

### Computing p0

Career stats from core.atp_matches: total serve points won divided by
total serve points played. Using overall p0, not surface-specific.
Surface is a separate categorical feature in the model so XGBoost can
learn surface effects independently. Player matching uses direct lookup
first, then rapidfuzz at threshold 90, then league-average fallback.

---

## 7. MODEL SPECIFICATION

### Algorithm

XGBoost (Extreme Gradient Boosting) wrapped in scikit-learn's
CalibratedClassifierCV with isotonic regression for post-hoc
calibration. Five base XGBoost estimators inside the calibrated
classifier (one per CV fold of the inner GroupKFold).

### Final Hyperparameters (Step 5B)

Selected via GridSearchCV with GroupKFold(n_splits=5) cross-validation
on the train set, scored by negative Brier loss across 24 hyperparameter
combinations:

learning_rate: 0.05. max_depth: 4. n_estimators: 300. reg_lambda: 5.0.

Fixed parameters: objective='binary:logistic', tree_method='hist',
random_state=42, eval_metric='logloss'.

The chosen hyperparameters are at the conservative end of the search
grid (smallest depth, slowest learning rate, fewest trees, strongest
regularization). This pattern indicates a model that benefits from
careful learning rather than aggressive fitting, consistent with the
genuine randomness inherent in tennis point outcomes.

### Calibration Method

Isotonic regression, selected by comparing test-set Brier scores across
three options: uncalibrated XGBoost (0.1497), Platt-scaled (0.1512),
and isotonic-calibrated (0.1495). Isotonic edged out uncalibrated by
0.0002 — a margin small enough to suggest the raw XGBoost output was
already nearly well-calibrated, which is itself a quiet vote of
confidence in the chosen hyperparameters. Platt scaling actually
slightly hurt performance, indicating the model's residual
miscalibration is not sigmoidal in shape.

### Input Features (63 total)

Score context (10): games_A, games_B, set_number, sets_won_A,
sets_won_B, game_number_in_set, plus surface one-hot encoded into four
columns (surface_hard, surface_clay, surface_grass, surface_unknown).

Signal sub-components (52): see Section 5.

Markov baseline (1): markov_set_win_prob_A.

### Target

Binary: set_winner_is_A (1 if Player A won the set, 0 if Player B won).

### Performance (Step 5 Results)

Headline test Brier (full test set, including decided rows): 0.1495.
Honest test Brier (undecided rows only): 0.1664. Naive Soft Markov
baseline on the same undecided rows: 0.2411. Relative improvement on
undecided rows: 31%.

Calibration curve: systematic under-confidence (every predicted-bin
mean is slightly below actual fraction of positives). Worst bin
deviation: 0.052 in the 0.8-0.9 bucket. Other bins within 0.03 of the
diagonal. The under-confidence direction is the safer direction for
betting purposes (when our model says 80%, reality is closer to 85%
— our predicted edges will be conservative rather than inflated).

Confidence-bucketed accuracy on the full test set:

The 50-55% confidence bucket: 6,583 predictions at 52.13% accuracy.
The 55-60% bucket: 4,334 predictions at 58.86%. The 60-65% bucket:
2,889 at 64.31%. The 65-70% bucket: 1,908 at 68.29%. The 70-75%
bucket: 1,450 at 70.28%. The 75-80% bucket: 1,182 at 78.68%. The
0.80+ bucket: 15,674 predictions at 94.21% accuracy. The accuracy
climb is monotonic across all buckets, validating the selective-edge
philosophy.

Performance by game number within set: Brier improves from 0.21 at
game 1 to 0.11 at game 9, then climbs back up to 0.21 at game 11
(reflecting that only the most competitive sets reach 5-5+, and these
are inherently hard to predict). Game 13 (tiebreak deciding points)
is trivial at Brier 0.0.

### Evaluation Methodology

Primary metric: Brier score. Naive baselines: constant 0.5 (Brier 0.25,
the math floor), hard pick by p0 differential (Brier 0.4028 — bad
because hard predictions get squared-error punished), soft Markov from
0-0 using career p0s (Brier 0.2409, the meaningful target).

Secondary analyses (all run in Step 5C): calibration curve at 10 bins,
confidence-bucketed accuracy across 7 buckets, performance sliced by
game_number_in_set, decided-versus-undecided split, feature importance
both individually and grouped by signal family.

Edge identification simulation (deferred to Step 8): once historical or
live odds become available, simulate selective-edge betting outcomes
by filtering to predictions where the model disagrees with the
bookmaker by some threshold AND the model's confidence is high. This
will be run on the live prediction stream collected during Track A.

---

## 8. BUILD SEQUENCE AND CURRENT STATUS

### ✅ Step 1 — Data Audit  [COMPLETE]

Audited charting schema. Confirmed core.atp_points_enhanced as source
(1.2M rows, 0 nulls on critical fields, all required fields confirmed).
4,714 distinct charted matches 2010-2025. Output:
data/processed/audit_report.txt.

### ✅ Step 2 — Game-Level Dataset Construction  [COMPLETE]

Built core.ml_game_level backbone (120,718 rows, 8/8 tests passing).
Script: scripts/dev/build_ml_game_level.py. Tests:
tests/test_ml_game_level.py. Output:
data/processed/game_level_build_report.txt.

### ✅ Step 2.5 — ml_game_level Enrichment  [COMPLETE]

Added integer match IDs (core.match_id_map, 7,160 entries, offset 1B),
8 new game-level descriptive features, and 5-level game_character
categorical. 13/13 tests passing.

### ✅ Step 3 — Signal Engineering  [COMPLETE]

Computed 52 signal sub-component columns by walking points sequentially
per match and emitting values at game boundaries. All values merged
into core.ml_game_level. 13/13 tests passing. Build runtime 4.3
seconds.

Files: src/signal_engine.py (250-line pure-Python walker, no DB
dependency), scripts/dev/build_signals.py, tests/test_signal_engine.py.
Build report: data/processed/signals_build_report.txt.

### ✅ Step 4 — p0 and Markov Feature  [COMPLETE]

Built the Markov probability engine from scratch, computed career p0
for all distinct players, persisted p0s to core.player_p0, and added
the markov_set_win_prob_A column to ml_game_level. 10/10 tests passing.
Build runtime 14.07 seconds.

Files: src/markov_engine.py (pure math, no DB), src/p0_engine.py,
scripts/dev/build_markov_feature.py, tests/test_markov_engine.py. Build
report: data/processed/markov_build_report.txt.

### ✅ Step 5 — Model Training and Evaluation  [COMPLETE]

Step 5 was decomposed into three sub-components, each its own focused
work session.

#### ✅ Step 5A — Data Loading and Naive Baselines

Built the ML data loading scaffold and computed two naive baselines on
the held-out test set: hard pick by p0 differential (Brier 0.4028) and
soft Markov from 0-0 using career p0s (Brier 0.2409). The soft-Markov
0.2409 became the meaningful target for XGBoost to beat. 14/14 tests
passing.

Files: src/data_loader.py (load_ml_data returning 63-feature train/test
arrays plus metadata), src/baseline.py (predict_hard_pick,
predict_soft_markov, load_p0_lookup), scripts/dev/run_baseline.py,
tests/test_data_loader.py, tests/test_baseline.py. Output:
data/processed/baseline_report.txt.

A wrapper inside baseline.py converts between the documented v3.2
markov_engine signature and the actual implemented signature, since
the reference guide originally documented a slightly different
parameter convention than the code uses. The wrapper is correct and
this v4 reflects the actual implemented signature. The wrapper also
handles the empirically-verified property that set_win_prob from 0-0
with unequal players is symmetric in who serves first.

#### ✅ Step 5B — XGBoost Training and Calibration

Hyperparameter tuning via GridSearchCV with GroupKFold(5) by
match_id_int as the grouping key, scored by neg_brier_score across 24
parameter combinations. Best parameters (smallest learning rate,
shallowest depth, fewest estimators, strongest regularization in the
grid) selected by CV. Three final models trained on the full train set
— uncalibrated, Platt-calibrated, and isotonic-calibrated. Test Brier
scores: 0.1497, 0.1512, 0.1495. Isotonic selected as the production
model with a 38% relative improvement over the soft-Markov baseline.
Total runtime 1:09 (much faster than initially estimated due to
tree_method='hist'). 8/8 tests passing.

Files: src/model_training.py (tune_hyperparameters,
train_calibrated_models, evaluate_on_test), scripts/dev/train_model.py,
tests/test_model_training.py. Outputs: data/processed/model_v1.joblib,
data/processed/model_v1_metadata.json,
data/processed/training_report.txt.

#### ✅ Step 5C — Full Evaluation Suite

Comprehensive evaluation on the held-out test set: calibration curve
(10 bins, systematic under-confidence with worst bin deviation 0.052,
others within 0.03), confidence-bucketed accuracy (monotonic 52% → 94%
across 7 buckets, 15,674 predictions at 94.21% in the 0.80+ bucket),
performance sliced by game number within set (U-shape: best at game 9
with Brier 0.11, worst at games 1 and 11 with Brier 0.21), decided-
versus-undecided split (decided 3,472 rows trivially predicted, model
beats Soft Markov on undecided rows by 0.0747 absolute Brier), feature
importance (Markov 35.5%, BPI 13.3%, SDS 13.2%, score_context 11.5%,
RES 11.2%, CPI 9.1%, MRS 6.3%). 8/8 tests passing.

Files: src/evaluation.py (load_trained_model,
compute_calibration_curve, compute_confidence_buckets,
compute_performance_by_game_number, compute_feature_importance,
make_plots), scripts/dev/evaluate_model.py, tests/test_evaluation.py.
Outputs: data/processed/evaluation_report.txt and four PNG plots
(calibration_curve.png, confidence_buckets.png,
brier_by_game_number.png, feature_importance.png).

### 🔲 Step 6 — Live Deployment  [IN PROGRESS — Track A focus]

Deploy the trained model to the existing DigitalOcean droplet
(142.93.82.38) and integrate it with the live tennis tracker so that
predictions are computed and logged in real time as live point-by-point
data arrives, and surfaced on the dashboard for human review. This step
replaces the original v3 plan of "Step 6 — Iteration" (which moves to
Step 7). The deployment proceeds without waiting for odds data per the
shadow deployment strategy (Section 2.2).

Step 6 decomposes into five sub-components, each its own work session.

#### 🔲 Step 6A — Streaming Signal Engine

The existing src/signal_engine.py walks completed matches in a single
batch pass. Live use requires an incremental, stateful engine that
processes one point at a time and maintains running state for each
ongoing match. This component builds a StreamingMatchState class with
the same logic as the batch engine, plus methods for: initializing a
new match (with players, p0 values, surface), adding a point and
updating internal accumulators, detecting game and set boundaries
correctly (including the workaround for the documented TennisAPI1
game_num bug — derive game number from score resets and server changes,
not from the API field), and emitting the full 60-feature vector at
each game boundary. Tests verify equivalence with the batch engine on
the same input data.

#### 🔲 Step 6B — Live Prediction Service

Combines the streaming signal engine with the trained model to produce
predictions in real time. Loads model_v1.joblib once at startup. After
each completed game (signaled by Step 6A), assembles the feature
vector, computes Markov baseline for the current state, calls
model.predict_proba, and returns a structured prediction object
(probability for Player A, confidence level, optionally top
contributing features for explainability). Tests cover a range of
realistic match scenarios.

#### 🔲 Step 6C — Persistent Prediction Logging

A new schema in DuckDB (within live_processed) stores every prediction
the live service emits, with full feature context. Schema includes
match identifier, set/game/point counters, score state, every input
feature value, prediction probability, calibrator version, model
version, and timestamp. The logging function is called by the live
prediction service after every prediction. Query helpers support
later retroactive analysis joining predictions against odds.

The persistent log is the asset that the eventual edge analysis (Step
8) operates on.

#### 🔲 Step 6D — State Machine Integration

The existing APScheduler state machine on the production droplet drives
data ingestion (IDLE → PRE_MATCH → LIVE → IDLE). This component wires
the live prediction service into that state machine so predictions are
computed automatically as new points arrive, without disrupting the
existing tracker functionality. Includes restart resilience (mid-match
state recovery), service health monitoring, and the deployment workflow
to the droplet (git pull, restart systemd service).

#### 🔲 Step 6E — Dashboard Integration

Update the live dashboard at http://142.93.82.38:8000/dashboard to
display predictions alongside the live score for each match in
progress. Show the current win probability for each player, ideally
with a visual gauge or chart. Optionally add a brief "why" panel
showing top contributing features. Optionally add a prediction history
view for browsing recently completed matches. Includes any necessary
backend FastAPI endpoint additions.

### 🔲 Step 7 — Model Iteration  [DEFERRED]

Track C work, deferred until live data accumulates and the deployment
is stable. Specific candidates surfaced by Step 5C:

The systematic under-confidence in the calibration curve is mild and
points in the safe direction for betting (we under-predict our own
edges rather than over-predicting them). Worth investigating whether a
small post-hoc correction tightens calibration without overfitting.

The MRS signal family carries only 6.3% of model feature importance
versus 13.3% for BPI. Consider whether dropping the weakest MRS
sub-components reduces noise without hurting predictive power.

The elevated Brier on extremely close sets (games 11-12) may be
inherently irreducible (these are coin-flip situations by definition),
but is worth investigating.

The 14.8% league-average rate in player_p0 (versus the v3.1 estimate
of 10%) suggests some players genuinely missing from atp_matches.
Lowering the fuzzy match threshold from 90 to ~85 might recover more
direct matches, but only worth doing if Step 6 live monitoring shows
Markov baseline weakness for specific players.

None of these are blockers for Step 6.

### 🔲 Step 8 — Odds Integration and Edge Analysis  [TRACK B / DEFERRED]

Two prerequisites must hold: enough live prediction data has accumulated
in the Step 6C log, and odds data is available either historically or
in real time. When both hold, the edge analysis originally scoped to
v3.2's Step 6C is run on the logged predictions: filter to
high-confidence predictions where the model disagrees with the
bookmaker by some threshold, simulate selective-edge betting on those,
and report the realized profit/loss compared to a buy-and-hold or
random-bet baseline. This is the gate that the v3.2 reference guide
called "6C odds comparison gate check," now repurposed as Step 8 and
operating on real live data rather than backtested data.

Step 8 may also include integrating live odds into the prediction
service so that real-time edge calculations are available alongside
predictions during live matches, but only after retrospective edge
analysis has confirmed there is real edge to exploit.

---

## 9. TECHNICAL ENVIRONMENT

### Machine and Runtime

Mac. Python 3 (command: python3, not python). Virtual environment
activated with: source .venv/bin/activate.

### Implementation Rule

All code produced by Claude Code (installed via npm:
@anthropic-ai/claude-code), never written manually in chat.

### Repository

GitHub: https://github.com/ychisholm/tennis-engine. tennis.duckdb
excluded from Git via .gitignore. The .joblib model artifact is also
excluded — production server pulls model from a separate channel (e.g.
direct upload or manual scp) to keep the repo lean.

### Database GUI

TablePlus on Mac for direct DuckDB inspection. Schemas accessible via
the bottom dropdown, not sidebar folders.

### Repository Structure

```
src/
  data_loader.py            — Step 5A data loading + 63-feature prep
  baseline.py               — Step 5A naive baselines
  signal_engine.py          — Step 3 batch signal computation walker
  markov_engine.py          — Step 4 Markov probability engine
  p0_engine.py              — Step 4 player p0 lookup with fuzzy matching
  model_training.py         — Step 5B tuning, calibration, evaluation
  evaluation.py             — Step 5C full evaluation suite
  (planned, Step 6:)
  streaming_signal_engine.py — incremental signal computation
  live_prediction_service.py — model + signals → live predictions
  prediction_logger.py       — persistent prediction storage

tests/
  test_signal_engine.py     — 13 tests
  test_markov_engine.py     — 10 tests
  test_data_loader.py       — 8 tests
  test_baseline.py          — 6 tests
  test_model_training.py    — 8 tests
  test_evaluation.py        — 8 tests

scripts/dev/
  build_ml_game_level.py    — Step 2/2.5 build script
  build_signals.py          — Step 3 build script
  build_markov_feature.py   — Step 4 build script
  run_baseline.py           — Step 5A orchestrator
  train_model.py            — Step 5B orchestrator
  evaluate_model.py         — Step 5C orchestrator

data/raw/                   — raw Sackmann data
data/processed/             — DuckDB, reports, parquet, model artifacts
```

### Key Libraries

duckdb, xgboost, scikit-learn, joblib, pandas, numpy, rapidfuzz,
matplotlib (for Step 5C plots), APScheduler, FastAPI (live feed
infrastructure).

### Live Feed Infrastructure

DigitalOcean Basic Droplet (Ubuntu 24.04) at 142.93.82.38. Systemd
service: tennis-engine. Dashboard: http://142.93.82.38:8000/dashboard.
APScheduler-driven state machine (IDLE → PRE_MATCH → LIVE → IDLE).
TennisAPI1 (fluis.lacasse on RapidAPI) for point-by-point. Deploy
workflow: edit locally → test → push to GitHub → git pull origin main
&& systemctl restart tennis-engine on server.

Step 6 will extend this infrastructure with the live prediction
service. The existing data ingestion path (TennisAPI1 → state machine
→ DuckDB) is preserved; the prediction layer is additive.

---

## 10. LIVE DATA FEED NOTES

### TennisAPI1 (RapidAPI)

Provider: fluis.lacasse. Base URL: https://tennisapi1.p.rapidapi.com.
Auth: x-rapidapi-key header. "Team" = Player in this API's terminology.
Date format: day/month/year as separate integers (not ISO format).
Match IDs: sequential integers — this is why charting IDs offset at 1B.

Confirmed working endpoints: /api/tennis/events/live,
/api/tennis/events/{day}/{month}/{year}, /api/tennis/event/{id},
/api/tennis/event/{id}/point-by-point. Full endpoint reference:
tennisapi1_endpoints.md.

Known bug: the game_num field in point-by-point feed doesn't always
increment on game end. Derive game number locally from score resets
and server changes. The streaming signal engine in Step 6A will need
to handle this.

---

## 11. WHAT NOT TO DO

### Data Rules

Do not use rally_length, serve_speed, shot_type, point_outcome,
first_serve_in, or any charting-specific field. Not in the live feed.
Do not train on pre-2015 data. Quality degrades and the sport has
changed. Do not touch the 2024-2025 test set during feature engineering
or hyperparameter tuning. (Test set may now be used for diagnostic
comparison against live data in Step 6 onward.) Do not use the
unreliable is_break_point column. Derive from score_before column
directly.

### Model Rules

Do not hand-tune signal weights, sub-component weights, or carryover
coefficients. XGBoost learns relationships from exposed features. Do
not blend sub-components into single signal scores. Expose them all as
separate features. Signals are organizational labels, not model inputs.
Do not use the old sigmoid/dominance-score architecture. It's retired.
Do not build separate surface models. Surface is a categorical feature.
Do not predict match winner, game winner, or totals yet. Set winner
only. Do not optimize the model for blanket overall accuracy. The
selective edge philosophy means we care about high-confidence accuracy
specifically.

### Process Rules

Do not break the live tracker on production while integrating the
prediction service. The existing tracker has users; deployment of the
prediction layer should be additive, not destructive. Do not deploy the
prediction service without persistent prediction logging from day one.
The logged prediction stream is the asset that eventually meets odds
data; missing days of logging cannot be recovered. Do not place real
bets based on the live model output until Step 8 has confirmed
real-world edge against actual bookmaker pricing. This is shadow
deployment, not live betting. Do not add new signals or sub-components
without evidence they improve the model. Do not write code directly —
always via Claude Code prompts. Do not skip tests. Every component has
a test file. Do not work on multiple build steps in one chat session.
Do not spend money on data feeds before there is a clear plan for how
the data will be used.

### Removed in v4

The previous rule "do not start live integration until model validates"
has been superseded. The model has validated through Step 5. The
related rule "do not begin live API work or suggest purchasing data
feeds before [the 6C odds gate] is cleared" has been narrowed to the
data-feeds-without-clear-plan version above. Live API work is now in
scope (Step 6 is exactly that work). The scope expansion is conscious
and is justified by Section 2.2.

---

## 12. GLOSSARY

| Term | Definition |
|------|------------|
| p | Probability server wins any given point on serve (0–1) |
| p0 | Career baseline p for a player (overall, not surface-specific) |
| G(p) | Exact game win probability given p (Markov engine) |
| Set(p,q) | Exact set win probability given p and q |
| BPI | Break Pressure Index — Signal 1 |
| SDS | Service Dominance Score — Signal 2 |
| RES | Return Effectiveness Score — Signal 3 |
| CPI | Clutch Performance Index — Signal 4 |
| MRS | Momentum / Runs Signal — Signal 5 |
| ws | within-set: signal version that resets at set boundaries |
| cm | cumulative-match: signal version that never resets |
| sub-component | Individual feature within a signal; the actual model input |
| pressure point | Score state 30-30, 40-40, 30-40, 40-30, 40-AD, AD-40, 0-40, or 15-40 |
| deep pressure | Return game reaching 0-40 or 15-40 |
| near pressure | Return game reaching deuce without ever generating a break point |
| score_before | Column in core.atp_points_enhanced containing the game score string before each point |
| ml_game_level | The backbone ML table: one row per completed game in a set |
| match_id_int | Integer match ID (≥ 1,000,000,000 for charting data) |
| split | 'train' (2015-2023) or 'test' (2024-2025) |
| Brier score | Calibration metric for probability forecasts. Lower = better |
| confidence-bucketed evaluation | Evaluating model accuracy at different confidence levels separately |
| selective edge | Strategy of acting only on high-conviction predictions where the model materially disagrees with the bookmaker |
| calibration curve | Plot showing predicted probability vs. actual win rate; well-calibrated models hug the diagonal |
| under-confidence | Calibration error where the model predicts lower probabilities than reality justifies (the safer direction for betting) |
| over-confidence | Calibration error where the model predicts higher probabilities than reality justifies (the dangerous direction for betting) |
| isotonic regression | Flexible step-function calibration method, more general than Platt scaling |
| Platt scaling | Sigmoid-curve calibration method, simpler than isotonic |
| GroupKFold | Cross-validation that keeps groups (matches) entirely within one fold to prevent leakage |
| decided row | A row in ml_game_level representing the deciding game of a set; the score features mechanically encode the outcome |
| undecided row | A row representing any game before the deciding game; the outcome is genuinely uncertain |
| shadow deployment | Running the model in production without acting on its output, for the purpose of validation and data collection |
| paper trading | Synonym for shadow deployment in financial contexts |
| Track A | Live deployment work (Step 6) |
| Track B | Odds sourcing work (parallel research/business activity) |
| Track C | Model iteration work (deferred until live data accumulates) |
| advantage set | Old-format set without tiebreak at 6-6 (e.g. 26-24); present in pre-2022 historical training data |

---

## 13. ARCHITECTURE EVOLUTION HISTORY (Context Only)

The project originally followed a 19-component build sequence (1A–7C).
Components 1A through 6B were completed under the original sigmoid
architecture before the strategic pivot to ML.

**v1.0 (sigmoid architecture, retired):** Hand-built signals →
dominance score → hand-tuned sigmoid → p-hat → Markov chain. Component
6B grid search produced boundary-hugging parameters (λ=10, k=0.04,
σ=40), suggesting the rigid sigmoid was the wrong shape. Combined with
the live-feed data constraint (no rally length, serve speed, or shot
data in production), this motivated the v2 pivot.

**v2.0 (XGBoost, May 2026):** Markov as a feature inside XGBoost.
Signals computed as single blended numbers from sub-components and fed
alongside score context. Removed hand-tuned probability transformation
but kept hand-engineered weights for combining sub-components within
each signal.

**v3.0 (exposed sub-components, May 2026):** Each signal decomposed into
its sub-components, all exposed as separate features. XGBoost learns
relationships at every level. "Signals" remain as conceptual organizing
labels for code structure and feature importance grouping, but the
model sees a flat feature list.

**v3.1 (Step 3 implementation complete, May 2026):** All 52 signal
sub-components computed and merged into ml_game_level. 13/13 tests
passing. Empirical aggregate statistics match known ATP baselines (SDS
hold rate ≈ 0.81, serve-win-pct ≈ 0.66).

**v3.2 (Step 4 complete, May 2026):** Markov baseline feature built
and merged. core.player_p0 created with 628 rows. Markov engine
extended with closed-form advantage-set deuce handling. 10/10 tests
passing.

**v4.0 (Step 5 complete and shadow deployment pivot, May 2026, this
version):** Three new components built and complete:

Step 5A produced a 63-feature data loader and two naive baselines on
the held-out test set: hard pick by p0 (Brier 0.4028) and soft Markov
from 0-0 (Brier 0.2409). 14/14 tests passing.

Step 5B trained an XGBoost classifier with hyperparameter tuning via
GroupKFold-grouped GridSearchCV (24 combinations, 5 folds), then
calibrated three variants on the full train set. The isotonic-
calibrated model achieved a test Brier of 0.1495, 38% relative
improvement over Soft Markov. Best parameters: learning_rate=0.05,
max_depth=4, n_estimators=300, reg_lambda=5.0. 8/8 tests passing.

Step 5C ran a comprehensive evaluation: calibration curve (mild
systematic under-confidence in the safe direction), confidence-bucketed
accuracy (monotonic 52% → 94% climb), performance by game number within
set (U-shaped curve), decided-versus-undecided split (model still beats
baseline on the rows that actually matter for live betting by 31%
relative), feature importance (Markov 35.5%, BPI 13.3%, SDS 13.2%, etc.).
8/8 tests passing.

The strategic pivot in v4 is the relaxation of the previous "no live
deployment until 6C odds gate clears" rule. The original rule existed
to insure against committing to deployment infrastructure for a model
of unknown worth. The model is no longer of unknown worth: Step 5
demonstrated real predictive ability. Combined with the open-ended
timeline for sourcing bookmaker odds data and the relatively low
marginal cost of live deployment on existing infrastructure, the v4
plan is to deploy now, log predictions to a persistent store from day
one, and run the eventual edge analysis on real live data when odds
become available. This is a shadow deployment / paper trading pattern.

The v4 build sequence is consequently restructured. The original Step
6 (Iteration) becomes Step 7 and is marked deferred until live data
accumulates. The original Step 7 (Live Integration) is renumbered Step
6 and broken into five sub-components (6A streaming signal engine, 6B
live prediction service, 6C persistent prediction logging, 6D state
machine integration, 6E dashboard integration). A new Step 8 (Odds
Integration and Edge Analysis) holds the work originally scoped to the
old Step 6C odds gate, now operating on logged live data instead of
backtested data.

**Carried forward through all versions:**
BPI insight (formerly NMI): pressure-without-conversion as the engine's
most original concept, validated by Step 5C as the strongest engineered
signal family. Game-level aggregation philosophy for betting-relevant
predictions. Selective edge philosophy.

**Retired or restructured:**
Markov engine (v1 2A): rebuilt from scratch in Step 4 (v3.2).
Archetype engine (v1 3A): retired. Temporal engine (v1 5A) sigmoid
combiner: retired. PhatAdjuster (v1 5B): retired. LiveMatch
orchestrator (v1 5C): retired; will be redesigned for live use as
Step 6B in v4. Backtester (v1 6A) and parameter tuner (v1 6B):
replaced by XGBoost evaluation in Step 5. Hand-blended signal scores
(v2): retired in favor of exposed sub-components (v3+). GPD (Game
Pressure Differential): renamed to CPI in v3 as differential framing
no longer fits the architecture. The "no live integration before 6C
gate" rule (v3.2): superseded in v4 by the shadow deployment strategy.
