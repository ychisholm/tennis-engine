---

# Tennis Prediction Engine — Project Log

## Folder Structure
tennis-engine/
├── src/
│   ├── markov_engine.py        # Component 2A — Markov probability engine
│   └── data_pipeline.py        # Component 1C — DuckDB data pipeline
├── data/
│   ├── raw/
│   │   ├── tennis_atp/         # Jeff Sackmann ATP dataset (cloned)
│   │   └── tennis_wta/         # Jeff Sackmann WTA dataset (cloned)
│   └── tennis_MatchChartingProject/  # Point-by-point data (cloned)
├── tests/
│   └── test_markov_engine.py   # Component 2A tests (19 tests, all passing)
├── notebooks/
├── config/
├── .venv/                      # Python virtual environment (not in Git)
├── .gitignore
├── requirements.txt
└── PROJECT_LOG.md              # This file

---

## Component Log

### 1A — Environment Setup ✅
- Mac, Homebrew, Python 3.14.2, VS Code, Git 2.39.5, Claude Code installed
- GitHub repo created: https://github.com/ychisholm/tennis-engine
- GitHub auth via Personal Access Token (Google OAuth account — password auth does not work)
- Python invoked as `python3`; venv activated with `source .venv/bin/activate`

### 1B — Project Folder Structure ✅
- Full scaffold created across `src/`, `data/`, `tests/`, `notebooks/`, `config/`
- `.venv` initialised, `.gitignore` configured

### 1C — Data Pipeline ✅
- **File:** `src/data_pipeline.py`
- **Database:** `tennis.duckdb` (excluded from Git — file too large for GitHub)
- **Tables:** `atp_matches`, `wta_matches`, `atp_points`, `wta_points`
- Sackmann ATP and WTA datasets cloned into `data/raw/`
- `tennis.duckdb` added to `.gitignore` after hitting GitHub 50MB file size limit
- Point-by-point data lives in separate repo: `tennis_MatchChartingProject`

### 2A — Markov Probability Engine ✅
- **File:** `src/markov_engine.py`
- **Tests:** `tests/test_markov_engine.py` — 19 tests, all passing
- **Key functions:**

| Function | Inputs | Output |
|---|---|---|
| `game_win_prob(p, points_server, points_receiver)` | p (float), score (int 0–3) | P(server wins game) |
| `tiebreak_win_prob(p, q, points_server, points_receiver)` | p own serve, q on opp serve | P(server wins tiebreak) |
| `set_win_prob(p, q, games_server, games_receiver)` | game-level p and q, current games | P(server wins set) |
| `match_win_prob(p_hat_A, p_hat_B, sets_A, sets_B, ...)` | full match state + p-hats | P(A wins match) |
| `compute_live_probabilities(p_hat_A, p_hat_B, match_state)` | p-hats + match_state dict | `{P_game_A, P_set_A, P_match_A}` |

- **Pre-computed lookup tables:** `GAME_TABLE` and `TIEBREAK_TABLE` for p ∈ [0.35, 0.85] at 0.01 resolution — loaded at import time for live speed
- **match_state dict keys:** `sets_A, sets_B, games_A, games_B, points_A, points_B, serving_player ('A' or 'B'), best_of`
- **Bugs caught during build:**
  - Tiebreak serve rotation parity check was inverted — fixed
  - 6-6 tiebreak recursion was unbounded — replaced with closed-form `pq / (1 - p - q + 2pq)`
- **Dependencies:** None beyond Python standard library + functools
- **Called by:** Component 5B (Dominance + p-hat) — not used directly until then

---

## Key Technical Notes

- Always activate venv before running anything: `source .venv/bin/activate`
- Database file (`tennis.duckdb`) is local only — regenerate by running `python3 src/data_pipeline.py`
- All p-hat values are clipped to [0.35, 0.85] as specified in the Spec
- Do not start Block 7 until component 6C (odds comparison gate) passes

---

## Build Status

| Component | Status |
|---|---|
| 1A Environment | ✅ Done |
| 1B Folder structure | ✅ Done |
| 1C Data pipeline | ✅ Done |
| 2A Markov engine | ✅ Done |
| 3A Archetype engine | ⬜ Next |
| 4A–4E Signals | ⬜ Blocked until 3A |
| 5A–5C Combining layer | ⬜ Blocked |
| 6A–6C Validation | ⬜ Blocked |
| 7A–7C Live interface | ⬜ Blocked (gate + paid API) |
