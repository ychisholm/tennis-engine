# Step 6D recon — wiring `LivePredictionService` into `MatchWorker._poll`

Read-only investigation. No source changes proposed beyond the minimal
writer-contract bump in §2 (still not applied).

Authoritative sources verified against current code on `main` at HEAD
`40719b1` (2026-05-11). Every line-number reference is from the current
working tree, not the prior recon.

---

## 1. Worker hook point

**File:** `src/live/collector.py`. The method is `MatchWorker._poll`,
defined at [collector.py:104-241](src/live/collector.py:104).

The `upsert_match_detail_points` call lives at
[collector.py:210-212](src/live/collector.py:210) inside a `try`/`except`
that starts at [collector.py:209](src/live/collector.py:209) and ends at
[collector.py:225](src/live/collector.py:225). The new prediction
adapter call must go **after** that `try`/`except` block returns, i.e.
between [collector.py:226](src/live/collector.py:226) (blank line after
the existing `try`/`except`) and the `winner_code` short-circuit at
[collector.py:227](src/live/collector.py:227).

Full body of `_poll` (lines 104-241):

```python
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
        _log.warning(...)
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
        # ── status / runtime checks ──
        status = parsed_detail.get("status")
        if status in self._terminal_non_finished:
            ...
            return
        if (time.time() - self._spawned_at) > self._max_runtime_seconds:
            ...
            return

        # ── score-state delta logging (POINTS_RECEIVED / NO_NEW_POINTS) ──
        score_state = (..., parsed_detail.get("away_current_point"))
        poll_logger = getattr(self, "_poll_logger", None)
        if status == "inprogress":
            ...
            self._last_score_state = score_state

        # ── augment with country codes ──
        parsed_detail["country_a"] = getattr(self, "_country_a", None)
        parsed_detail["country_b"] = getattr(self, "_country_b", None)

        # ── learn first_server (lazy point-by-point probe) ──
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
                pass

        # ── writer call: this is where state row is inserted (or skipped) ──
        try:
            self._logger.upsert_match_detail_points(
                parsed_detail, polled_at, first_server=self._first_server,
            )
        except Exception as exc:
            _log.warning(...)
            poll_logger = ...
            if poll_logger is not None:
                poll_logger.log(
                    event_type="POLL_ERROR",
                    match_id=self._match_id,
                    detail=str(exc)[:200],
                    poll_cycle_id=cycle_id,
                )

        # ◀──────── HOOK POINT: prediction adapter call goes here ────────▶

        if parsed_detail.get("winner_code"):
            ...
            return

    _log.debug(...)
```

**Local variables in scope at the hook point** (immediately after the
`upsert_match_detail_points` `try`/`except`, before the `winner_code`
short-circuit):

| Name | Type | Source |
|---|---|---|
| `cycle_id` | `uuid.UUID` | [collector.py:105](src/live/collector.py:105) |
| `polled_at` | `datetime` (UTC) | [collector.py:106](src/live/collector.py:106) |
| `raw_detail` | `dict` (raw JSON) | [collector.py:108](src/live/collector.py:108) — only when no early exception |
| `parsed_detail` | `dict \| None` | [collector.py:111](src/live/collector.py:111) — guaranteed non-None inside the `if parsed_detail:` block |
| `status` | `str` | [collector.py:136](src/live/collector.py:136) |
| `score_state` | `tuple[Optional[int], …]` | [collector.py:158](src/live/collector.py:158) — only set when `status` was truthy |
| `poll_logger` | `PollLogger \| None` | reset multiple times via `getattr(self, "_poll_logger", None)` |
| `self._first_server` | `str \| None` (`'home' \| 'away'`) | learned lazily |
| `self._match_id` | `int` | constructor |
| `self._player_a` / `self._player_b` | `str` | constructor |
| `self._tournament_name` | `str` | constructor |
| `self._category` | `str` (`'atp' \| 'wta'` lowercase) | constructor |
| `self._country_a` / `self._country_b` | `str \| None` (alpha2) | constructor |
| `self._tournament_id` | `int \| None` | constructor (currently unused after init) |

**Confirmed available at the hook point:** `self._first_server`,
`polled_at`, `parsed_detail`, `self._match_id`, `cycle_id` (the
poll_cycle_id).

---

## 2. Writer return contract

`MatchLogger.upsert_match_detail_points` is declared at
[logger.py:385-628](src/live/logger.py:385) with signature

```python
def upsert_match_detail_points(
    self,
    parsed_detail: dict,
    polled_at: datetime,
    first_server: str | None = None,
) -> None:
```

**Current return:** implicitly `None` on every path:

- Early return on missing `match_id_int` ([logger.py:406](src/live/logger.py:406))
- Early return on missing point fields ([logger.py:411](src/live/logger.py:411))
- Early return on out-of-range `current_set` ([logger.py:417](src/live/logger.py:417))
- Early return on bogus-0-0 skip ([logger.py:462](src/live/logger.py:462))
- Early return on idempotent-repoll skip ([logger.py:490](src/live/logger.py:490))
- Falls off the end after the INSERT path ([logger.py:620](src/live/logger.py:620))

The INSERT statement fires exactly once at
[logger.py:564-589](src/live/logger.py:564) — `cur.execute(_UPSERT_MATCH_STATE, [...])`.
The `ON CONFLICT (match_id, polled_at) DO NOTHING` clause means a
duplicate `(match_id, polled_at)` PK collision silently inserts zero
rows; `cur.rowcount` distinguishes the two cases.

A retroactive `UPDATE` at [logger.py:604-618](src/live/logger.py:604)
may also fire to attribute a prior NULL `point_winner`, but that is a
fix-up, not the row we want returned.

**Proposed minimal change** (not applied here): change the return type
to `dict | None` and return either (a) the post-retro-update view of
the row we just inserted, or (b) `None` on every skip path. The least
intrusive patch:

```python
# After cur.execute(_UPSERT_MATCH_STATE, [...]) at logger.py:564
inserted = cur.rowcount  # 1 on fresh insert, 0 on ON CONFLICT skip
...
if retro and prev is not None:
    cur.execute("UPDATE ... point_winner ...")
    ...
self._conn.commit()
if inserted == 0:
    return None
return {
    "match_id": match_id_int,
    "polled_at": polled_at,
    "status": parsed_detail.get("status"),
    "home_sets_won": home_sets,
    "away_sets_won": away_sets,
    "home_set1_games": curr_state["home_set1_games"],
    "away_set1_games": curr_state["away_set1_games"],
    "home_set2_games": curr_state["home_set2_games"],
    "away_set2_games": curr_state["away_set2_games"],
    "home_set3_games": curr_state["home_set3_games"],
    "away_set3_games": curr_state["away_set3_games"],
    "home_current_games": home_current_games,
    "away_current_games": away_current_games,
    "home_current_point": home_pt_s,
    "away_current_point": away_pt_s,
    "point_winner": point_winner,
    "first_server": first_server,
    "prev": prev,  # for the adapter
}
```

The `prev` field carries the previously-most-recent row (already
fetched at [logger.py:510-549](src/live/logger.py:510) for
`point_winner` derivation) so the adapter does not have to re-query.

**Alternative**: leave `upsert_match_detail_points` unchanged and have
the prediction wiring fetch `(prev, just_inserted)` itself via a
fresh `SELECT … ORDER BY polled_at DESC LIMIT 2`. This is one extra
round-trip per poll. The build prompt should pick based on whether
schema and writer changes are in scope.

Every other return path (the five skip branches) should return `None`
to match the existing implicit behavior, and the build should treat
`None` as "no new state — skip the prediction step this poll."

---

## 3. `live.match_states` column reference for the adapter

DDL is at [logger.py:71-108](src/live/logger.py:71). Empirically
confirmed schema:

| Column | PG type | Nullable | Semantics |
|---|---|---|---|
| `match_id` | `INTEGER` | NO | Sofa event id; PK part |
| `player_a` | `VARCHAR` | YES | home player display name |
| `player_b` | `VARCHAR` | YES | away player display name |
| `polled_at` | `TIMESTAMPTZ` | NO | UTC poll time; PK part |
| `status` | `VARCHAR` | YES | `'inprogress' \| 'finished' \| 'notstarted' \| 'canceled' \| 'postponed' \| 'walkover'` |
| `home_sets_won` | `INTEGER` | NO | sets A has won prior to current set |
| `away_sets_won` | `INTEGER` | NO | sets B has won prior to current set |
| `home_set1_games`–`home_set3_games` | `INTEGER` | YES | per-set games totals; NULL for sets not yet started |
| `away_set1_games`–`away_set3_games` | `INTEGER` | YES | mirror |
| `home_current_games` | `INTEGER` | NO | games A has in the in-progress set |
| `away_current_games` | `INTEGER` | NO | games B has in the in-progress set |
| `home_current_point` | `VARCHAR` | NO | regular: `'0' \| '15' \| '30' \| '40' \| 'A' \| 'AD'`; tiebreak: integer-as-string (e.g. `'7'`, `'10'`) |
| `away_current_point` | `VARCHAR` | NO | mirror |
| `point_winner` | `VARCHAR` | YES | `'home' \| 'away' \| NULL` — see below |
| `winner_code` | `INTEGER` | YES | 1 = home, 2 = away; non-NULL only on terminal `'finished'` row |
| `tournament_name` | `VARCHAR` | YES | display name (e.g. `"WTA 1000 Rome, Italy"`) |
| `category` | `VARCHAR` | YES | `'atp' \| 'wta'` — lowercase, lower-cased from the API category slug |
| `country_a` / `country_b` | `VARCHAR` | YES | ISO 3166-1 alpha-2; nullable when missing in the live event |
| `first_server` | `VARCHAR(10)` | YES | `'home' \| 'away' \| NULL` — backfilled by `MatchLogger.backfill_first_server` once `/point-by-point` resolves it |

PK: `(match_id, polled_at)`. Index `match_states_score_idx` on
`(match_id, home_sets_won, away_sets_won, home_current_games,
away_current_games, home_current_point, away_current_point)`.

### `point_winner` semantics

Derivation lives at [logger.py:558-562](src/live/logger.py:558):

```python
point_winner = (
    _derive_point_winner(prev, curr_state)
    or _retro_winner_for_prev_game(prev, curr_state)
    or (_infer_first_row_winner(curr_state) if prev is None else None)
)
```

The three helpers are at
[logger.py:211-246](src/live/logger.py:211),
[logger.py:268-300](src/live/logger.py:268), and
[logger.py:249-265](src/live/logger.py:249).

**When is `point_winner` NULL vs `'home'` vs `'away'`?**

- **First row of a match (`prev is None`).** `_infer_first_row_winner`
  is consulted. NULL if both sides are at `'0'` (no point yet) or if
  both are non-zero (multiple points compressed into the first
  observation). Otherwise the non-zero side wins.
- **Same set, same `(home_current_games, away_current_games)`** ("same
  game" transition). `_derive_point_winner` compares rank deltas. If
  one side advances OR the other regresses from AD (a deuce reset),
  attribute to that side. Returns NULL only when the comparison is
  ambiguous (in practice never within a regular game; commonly NULL
  inside tiebreaks because `_rank_point_score` parses tiebreak digits
  via `int()` and `ch > ph or ca < pa` is then ambiguous when both
  digits incremented — empirically the 10 most recent NULL rows are
  all tiebreak transitions inside the same 6-6 game).
- **Multi-point compression in the same game** (the gap > 1 case,
  e.g. `15-0 → 30-15`): `_derive_point_winner` still returns the
  player whose rank went up; ordering is lost. Empirical example
  ([recon §13](#13-empirical-findings)): `0-30 → 15-40` produces
  `point_winner='home'` because home advanced — even though one away
  point also happened between the two observations. **The column gives
  the *latest* observable winner under the writer's rank check; it is
  not necessarily a faithful per-point label.**
- **Game-end transition** (in-set games incremented, new row at `0-0`
  with non-zero `games`): `_derive_point_winner` returns NULL because
  sets/games changed (it short-circuits at
  [logger.py:234](src/live/logger.py:234)).
  `_retro_winner_for_prev_game` fills in NULL: whichever side's
  `home_current_games` (or `away_current_games`, or the
  matching `home_setN_games` at set boundary) incremented. So the
  *new* row's `point_winner` is the *game winner*, i.e. the side that
  won the unobserved final point of the prior game.
- **Set-end transition** (`home_sets_won` or `away_sets_won`
  incremented; new row has `games_a=games_b=0`):
  `_retro_winner_for_prev_game` reads the completed set's
  `home_setN_games`/`away_setN_games` from `curr` (the new row carries
  the totals for set N=prev_sets+1) and attributes the game winner
  the same way ([logger.py:289-300](src/live/logger.py:289)).
- **Retroactive update** ([logger.py:594-618](src/live/logger.py:594)):
  when a new row starts a new game, the writer goes back and UPDATEs
  the **prior** row's `point_winner` if it was NULL, using the games-
  count delta to assign it. The current row's `point_winner` is
  unaffected by this.
- **Tiebreak entry** (prev = 6-6 with point in regular vocab, curr =
  6-6 with point in integer vocab). The same-game check
  `same_games` is `True` because games haven't changed.
  `_rank_point_score` returns the integer for tiebreak digits via
  `int()`, but the prior side's score is `'0'` (rank 0) and the new
  side's is `'1'` (which falls through the table lookup to `int('1')
  = 1`). So `_derive_point_winner` does return `'home'` or `'away'`
  cleanly for the first tiebreak point. Subsequent tiebreak transitions
  inside the same 6-6 game also pass through `_derive_point_winner`
  successfully unless both sides incremented (a compression).
- **"Bogus 0-0" rows.** The API briefly emits `(0,0)` rows between
  points. The writer filters them at [logger.py:448-462](src/live/logger.py:448):
  if `home_current_point == '0' AND away_current_point == '0'` AND a
  non-0-0 row already exists for this same `(match_id,
  home_sets_won, away_sets_won, home_current_games, away_current_games)`
  state, the writer returns early without inserting. So these never
  reach `live.match_states`.

Empirically (§13, sample of 10 NULL rows in last 7 days): every NULL
row falls into one of two buckets:
1. `status='notstarted'` rows that the live tracker writes pre-match
   with `home_sets_won=away_sets_won=0`, all-zero games, point=0-0 —
   the live_adapter's `live_match_states_to_state_rows` already filters
   these.
2. Tiebreak transitions where both digits incremented (multi-point
   compression inside a tiebreak game).

NULL share over the last 7 days: 677 / 25406 rows ≈ **2.66%**.

---

## 4. State row → Point algorithm

Given two adjacent `live.match_states` rows `prev` and `curr` (ordered
by `polled_at` ASC), the adapter must emit zero or more `Point`
objects compatible with the engine's duck-typed Point shape
([signal_engine.py:103](src/signal_engine.py:103) docstring — fields
`set_number`, `game_number_in_set`, `score_before`, `Svr` (1=home,
2=away), `PtWinner` (1=home, 2=away), `is_tiebreak`).

Plain-prose algorithm:

1. **Identify which set the transition belongs to.** Use `prev`'s
   `(home_sets_won + away_sets_won + 1)` as the set number, except in
   the set-end shape where the writer mirrors the canonical book/promoter
   convention: the *closing* row of a set may carry the just-completed
   set in `home_sets_won/away_sets_won` and games at `(0, 0)`. The
   adapter must classify the transition before assigning a `set_number`.

2. **Classify the transition shape**, in this order:
   - **Skipped**: both rows have identical state (the bogus-0-0 filter
     already prevents most cases; defensive double-check anyway).
   - **Same set, same games** ⇒ *within-game transition*.
   - **Same set, games incremented by exactly one game** ⇒
     *game-end transition*.
   - **Sets incremented by 1, new row at `games=(0,0)` and
     `point=(0,0)`** ⇒ *set-end transition* (fresh set started).
   - **Sets incremented by 1, new row at non-zero games and
     `point=(0,0)` with `status='finished'`** ⇒ *match-end shape*
     (the writer emits the closing set's true games on the terminal
     poll — see [logger.py:427-437](src/live/logger.py:427) and the
     `match_end_shape` branch in
     [validator.py:475-484](src/verification/validator.py:475)).
   - Anything else (games regression, multi-game jump, set regression)
     ⇒ malformed transition; emit no Points and log the anomaly.

3. **Compute server identity.** Server for the in-progress game is
   determined from `first_server` (canonical learned value) plus the
   parity of completed games:
   - Total completed games in the match = `home_sets_won * games_per_set_completed`
     (not reliable directly — use `polished_games(prev)` instead). The
     simpler accurate rule: total completed games = `prev.home_sets_won +
     prev.away_sets_won` set lookups summed, PLUS
     `prev.home_current_games + prev.away_current_games`. The current
     game's server is `first_server` iff that total is even, else the
     other side. Concretely: `server_was_home = (first_server == 'home') XOR (total_completed_games % 2 == 1)`.
   - **Tiebreak entry exception (v4.3 behavior).** Per the recon at
     [logger.py:184](src/live/logger.py:184) and the comment in
     `_ScoreState.observe_point` at
     [live_prediction_service.py:111-119](src/live_prediction_service.py:111):
     when the new game is a tiebreak (game 13 in a normal set,
     detected via `_is_tiebreak_score` from
     [tennis_feed.py:442-448](src/live/tennis_feed.py:442)), the
     server of the *first* tiebreak point is the side that did **not**
     serve the last regular game (i.e. server flips one final time
     before the tiebreak begins). `_ScoreState` already flips
     `current_game_server` automatically when it sees a tiebreak boundary,
     so the adapter just needs to compute the regular-alternation
     server for the prior game and let `_ScoreState` do the flip. **A
     stuck Svr inside the tiebreak** (book.points shows `Svr=2` on every
     tiebreak point as a known quirk per the comment at
     [live_prediction_service.py:111](src/live_prediction_service.py:111))
     is acceptable — `_ScoreState.observe_point` ignores `pt.Svr` for
     in-tiebreak server tracking.

4. **Compute `score_before` for each emitted Point**, formatted as
   `"{home_score}-{away_score}"` using regular-game vocabulary
   (`"0"|"15"|"30"|"40"|"AD"`) for non-tiebreak games and integer
   strings for tiebreaks. `score_before` is the score *immediately
   before* the point is played (so the first point of every game has
   `score_before == "0-0"`).
   - **Single-point transition** (rank delta sums to 1): emit exactly
     one Point with `score_before = "{prev.hp}-{prev.ap}"` after
     normalising `'A'` → `'AD'` (mirroring
     [validator.py:_normalize_point](src/verification/validator.py:221)).
   - **Multi-point compression** (rank delta sums to ≥ 2 with both
     sides ≥ 0): the adapter must synthesise `N = delta_total`
     intermediate Points and inject them between `prev` and `curr`.
     For each synthesised point pick `score_before` by walking the
     regular-game graph in `_REGULAR_GAME_EDGES`
     ([validator.py:98-117](src/verification/validator.py:98)) from
     `(prev.hp, prev.ap)` to `(curr.hp, curr.ap)` via BFS. Use the
     shortest-path edges in order. Attribute the *last* synthesised
     point's `PtWinner` to whichever side the writer's `point_winner`
     column names (this is what the writer recorded). Distribute the
     intermediate points' winners by following the BFS edges — each
     edge moves either home's or away's score up by one rank, so the
     direction of each edge dictates the per-point winner.
   - **Game-end transition.** Synthesise the unobserved tail of `prev`'s
     in-progress game by walking the same `_REGULAR_GAME_EDGES` graph
     from `(prev.hp, prev.ap)` to `GAME_A` (if `curr.home_current_games
     > prev.home_current_games`) or `GAME_B` (mirror). The number of
     extra Points is `path_length - 1` (per
     [validator.py:_check_game_end_score:336-351](src/verification/validator.py:336)).
     Each synthesised Point's winner follows the edge direction. The
     game-winning point's `point_winner` is the side whose games count
     incremented in `curr` — `_retro_winner_for_prev_game` proves this
     is unambiguous.
   - **Set-end transition.** Treat as a game-end (synthesise the
     winning game's tail) immediately followed by a set boundary.
     `_ScoreState.observe_point` handles the set bump automatically
     because `cur_set != prev_set` triggers
     [live_prediction_service.py:99-106](src/live_prediction_service.py:99).
   - **Match-end shape.** Same as set-end: synthesise the closing
     game's tail, then `LivePredictionService.finalize()` flushes the
     final in-progress game when the worker stops.

5. **Map `point_winner` from the row to the Point's winner.** The
   row's `point_winner` represents the *latest* point observed:
   - Single-point transition: `pt.PtWinner = 1` if `row.point_winner == 'home'`,
     else `2`.
   - Multi-point compression: as above, the row's value names only the
     latest observable side; intermediate winners come from edge-walk
     direction. The build prompt must document this.
   - Game-end / set-end: the writer's `point_winner` column on the new
     row holds the game-winning side via the retro-fill. Use it for
     the synthesised game-winning Point.

6. **`is_tiebreak` flag.** Inspect both `prev` and `curr`. A Point is
   in a tiebreak when its `(home_current_games, away_current_games) ==
   (6, 6)` AND at least one of `home_current_point` /
   `away_current_point` is an integer string outside the regular
   vocab. Use
   [tennis_feed.py:442-448](src/live/tennis_feed.py:442) (`TennisFeed._is_tiebreak_score`)
   as the canonical detector.

7. **`game_number_in_set`.** Use `prev.home_current_games +
   prev.away_current_games + 1` for in-game and game-end synthesised
   points; the canonical convention is that tiebreaks are
   `game_number_in_set == 13` (per
   [tennis_feed.py:496-497](src/live/tennis_feed.py:496) and the
   `_GameTracker` semantics in
   [signal_engine.py:128-131](src/signal_engine.py:128)).

The order of operations inside a single transition: emit synthesised
in-game points first (in BFS path order), then the game-winning point
(if game-end), then `_ScoreState` will flip set / reset games on the
*next* call. The recon for `_ScoreState`
([live_prediction_service.py:75-129](src/live_prediction_service.py:75))
makes this explicit: `observe_point` returns the closing snapshot only
when the new point's `(set_number, game_number_in_set)` differs from
the previous one, so a clean game boundary is detected by the engine
naturally as long as the adapter increments `game_number_in_set`.

---

## 5. Match metadata source

`LivePredictionService.start_match` ([live_prediction_service.py:233-245](src/live_prediction_service.py:233))
requires `player_a_id: int`, `player_b_id: int`, and `raw_surface: str`
(plus `first_server_is_a` and the player display names).

**`player_a_id` / `player_b_id`:** **NOT** in
`parse_match_detail`'s output. `TennisFeed.parse_match_detail`
([tennis_feed.py:281-312](src/live/tennis_feed.py:281)) produces only
`match_id`, `player_a`, `player_b`, `status`, `home_sets`/`away_sets`,
`home_periodN`/`away_periodN`, `home_current_point`/`away_current_point`,
`winner_code`, `tournament_name`, `category`. No IDs.

The raw API event payload **does** carry them. Empirically (recon §13)
the raw blob has `event.homeTeam.id` (e.g. `20732` for Karolína
Plíšková) and `event.awayTeam.id`. These IDs flow into MatchWorker's
constructor via `event["homeTeam"]["name"]` / `["country"]` but
**not** the IDs — see
[collector.py:37-53](src/live/collector.py:37). The IDs would have to
be plumbed through one of three routes:

1. **Pull from `raw_detail` inside `_poll`.** After
   `self._feed.get_match_detail` returns at
   [collector.py:108](src/live/collector.py:108), `raw_detail["event"]["homeTeam"]["id"]`
   and `["awayTeam"]["id"]` are available. The build prompt could
   either store them on the worker (`self._player_a_id`,
   `self._player_b_id`) on the first poll where they parse cleanly, or
   parse-and-pass on every poll.
2. **Capture from the discovery event in `MatchCollector._cycle`.** The
   `event` dict the collector hands to `MatchWorker.__init__` is the
   same raw-event shape; `event["homeTeam"]["id"]` is right there. The
   constructor would need a few extra lines to capture them. This
   matches the existing pattern for `_country_a`/`_country_b`
   ([collector.py:40-41](src/live/collector.py:40)).
3. **DB lookup at hook time.** The discovery payload may not include
   IDs reliably — preference is to read off the raw_detail JSON each
   poll until both IDs are observed, then cache on the worker.

The **most consistent existing pattern** is (2): extend
`MatchWorker.__init__` to capture the IDs alongside countries. The build
prompt should adopt that.

**`raw_surface`:** the API exposes it as
`raw_detail["event"]["groundType"]`. Confirmed empirically for match
16160007: `groundType = "Red clay"`. `parse_match_detail` does not
read this field. The adapter must either:

1. Have `TennisFeed.parse_match_detail` start writing `surface_raw`
   (or `ground_type`) into its output dict, OR
2. Pluck `raw_detail["event"]["groundType"]` directly inside the
   worker hook.

Either way, normalise via
[book/promoter.py:_normalize_surface:121-133](src/book/promoter.py:121)
before handing to `start_match` — this is the same function used to
populate `book.matches.surface`, so the live and historical surface
buckets stay consistent. Note that `_ScoreState`'s start_match
defensive normalisation
([live_prediction_service.py:260-266](src/live_prediction_service.py:260))
also lower-cases and pins unknown values to `'unknown'`, but the
book-canonical form is preferred.

(There is no DB-backed source for surface for an in-progress match
short of reading the raw API archive after-the-fact — surface is not
stored on `live.match_states` or `live.match_polls`.)

**`tournament_name`** is already populated on the worker
(`self._tournament_name`) and is already written to every
`match_states` row, but it is not what `start_match` accepts. Surface
is the only categorical input the model uses
([live_prediction_service.py:260-266](src/live_prediction_service.py:260)).

**Access at the hook point:** the build prompt should plumb the IDs
and surface to the worker constructor and store them as
`self._player_a_id`, `self._player_b_id`, `self._raw_surface`. At the
hook point they are then directly available alongside
`self._first_server` and the other constructor-time fields.

---

## 6. `LivePredictionService` API surface (post-6C)

```python
class LivePredictionService:
    def __init__(
        self,
        model_path: str = "data/processed/model_v1.joblib",
        metadata_path: str = "data/processed/model_v1_metadata.json",
        p0_lookup: dict[str, float] | None = None,
        db_path: str = "data/processed/tennis.duckdb",
        league_avg_p0: float = 0.6266,
        prediction_logger: Optional[PredictionLogger] = None,
        model_version: str = "v1",
    ) -> None:
        ...
```

Source: [live_prediction_service.py:163-172](src/live_prediction_service.py:163).

Constructor side-effects: loads the joblib model, validates
`n_features_in_=63` and `classes_=(0,1)`, loads metadata, loads p0
lookup from DuckDB (`core.player_p0`) if not supplied. **The
PredictionLogger is set at construction time** at
[live_prediction_service.py:224-230](src/live_prediction_service.py:224):
if the caller passes one, that one is used; otherwise the constructor
falls back to `get_default_logger()` (the process-wide singleton from
[prediction_logger.py:229](src/prediction_logger.py:229)). The
fallback is wrapped in `try/except` so missing `DATABASE_URL` or DB
errors keep `_prediction_logger = None` instead of raising.

```python
def start_match(
    self,
    match_id_int: int,
    player_a: str,
    player_b: str,
    raw_surface: str,
    first_server_is_a: bool,
    p0_a: float | None = None,
    p0_b: float | None = None,
    *,
    player_a_id: int,
    player_b_id: int,
) -> None:
```

Source: [live_prediction_service.py:233-245](src/live_prediction_service.py:233).
`player_a_id` and `player_b_id` are **keyword-only**, both required
(no defaults). `p0_a`/`p0_b` are optional — if omitted, the service
resolves them via `_resolve_p0` from `self.p0_lookup` (which was
loaded at construction time from DuckDB or the optional `p0_lookup`
override).

**`model_version`**: defaults to `"v1"` on the constructor. The logger
also accepts a `model_version` kwarg on `log()` but `LivePredictionService._make_prediction`
already passes `self._model_version` at
[live_prediction_service.py:394-397](src/live_prediction_service.py:394).
So the worker doesn't need to pass `model_version` on every prediction;
constructor default is sufficient unless we want versioning per
worker.

**`prediction_logger`**: set on the constructor only. `start_match`
does not re-bind it. To use a non-default logger, pass it at
`LivePredictionService(...)` construction.

**Lifecycle in the worker context:**
- **One service per active match.** Per the design decisions in this
  task, lifecycle owned by `MatchWorker`. Construct lazily on the
  first poll where `self._first_server` is known (the prediction layer
  cannot start without it — `start_match` requires `first_server_is_a`).
- **`start_match` is called exactly once per worker**, immediately
  after the service is constructed. The flow:
  1. First few polls — `self._first_server is None`. Skip the
     prediction step entirely.
  2. First poll where `self._first_server` is non-NULL — construct
     `self._service = LivePredictionService(...)`, call
     `self._service.start_match(...)`, then replay all existing
     `live.match_states` rows for the match silently (see §7), then
     resume normal forward flow.
  3. Subsequent polls — feed each new Point produced by the adapter
     into `self._service.process_point(...)`. The service returns
     either `None` (no game closed yet) or a `Prediction` (a game
     closed and was scored).
  4. When the worker stops (the `winner_code` short-circuit in
     [collector.py:227-236](src/live/collector.py:227)), call
     `self._service.finalize()` to flush the last in-progress game.

The "construct on first known `first_server`" trigger is the only
pattern the existing code supports. Earlier polls have nothing
actionable (`start_match` would raise on a NULL `first_server_is_a`
because `bool(None)` is `False`, silently mis-classifying the match).

---

## 7. Replay pattern

Goal: when the service is constructed on the first poll with known
`first_server`, replay every existing `live.match_states` row for the
match through the adapter+service so the streaming engine's
accumulators reach the same state they would have had if we'd been
live from the first poll. Predictions emitted during replay are
**discarded** — only the in-memory state should be advanced. This
mirrors the restart-resilience requirement.

**Existing "silent mode" on `LivePredictionService` / `PredictionLogger`?**
There is **none**. Verified:

- `PredictionLogger.log` ([prediction_logger.py:124-204](src/prediction_logger.py:124))
  has no `quiet` kwarg; it always tries to insert.
- `LivePredictionService.process_point` ([live_prediction_service.py:272-294](src/live_prediction_service.py:272))
  calls `_make_prediction` which unconditionally invokes
  `self._prediction_logger.log(...)` at
  [live_prediction_service.py:394-397](src/live_prediction_service.py:394).
- The only escape hatch is `self._prediction_logger is None`: if the
  logger reference is `None`, `_make_prediction` skips the `log()`
  call entirely.

**Options evaluated:**

- **(a) Pass `prediction_logger=None` for the replay phase, swap to the
  real logger after.** Requires re-construction of
  `LivePredictionService` (logger is constructor-only) or a setter.
  Doubling construction is wasteful (joblib load + DuckDB read each
  cost real seconds). Adding a setter is a new API surface.
- **(b) Boolean kwarg on `process_point` to suppress logging.**
  Touches the service's most-used method, propagates to
  `_make_prediction`, and is easy to misuse (the caller has to remember
  to flip it back).
- **(c) Capture the `Prediction` return value during replay and just
  don't log it — but the service already logs inside
  `_make_prediction` before returning, so this requires changing
  the service.**

**None of the three are zero-change.** The cleanest delta is a
*temporary* swap of `self._service._prediction_logger`:

```python
real_logger = self._service._prediction_logger
self._service._prediction_logger = None
try:
    for old_row, prev_row in replay_pairs:
        for pt in adapt(old_row, prev_row, self._first_server):
            self._service.process_point(pt)
finally:
    self._service._prediction_logger = real_logger
```

This is a one-attribute mutation, requires no API change, and the
service's only use of the attribute is the conditional call inside
`_make_prediction` — `if self._prediction_logger is not None`. Setting
it to `None` for the replay window is exactly the path the service
already supports.

**Recommendation:** Option (a) variant — *swap the attribute* (don't
reconstruct the service). Minimal change, no public API change, and
the attribute is module-internal so direct mutation is acceptable.

The recommended pattern the build prompt should follow:

```python
def _ensure_service_started(self):
    if self._service is not None or self._first_server is None:
        return
    # Build service with the real logger
    self._service = LivePredictionService(prediction_logger=get_default_logger())
    self._service.start_match(
        match_id_int=self._match_id,
        player_a=self._player_a,
        player_b=self._player_b,
        raw_surface=self._raw_surface,
        first_server_is_a=(self._first_server == "home"),
        player_a_id=self._player_a_id,
        player_b_id=self._player_b_id,
    )
    # Replay every existing state-row for this match with logger muted
    rows = self._fetch_existing_state_rows()  # see §11
    real = self._service._prediction_logger
    self._service._prediction_logger = None
    try:
        adapter = StateRowAdapter(first_server=self._first_server)
        for prev, curr in pairwise(rows):
            for pt in adapter.transition(prev, curr):
                self._service.process_point(pt)
    finally:
        self._service._prediction_logger = real
```

A minor caveat: **invariant violations during replay must also be
caught** (per §10), and `RuntimeError` from `process_point` should be
swallowed during replay (the adapter may produce an unrecoverable
server-alternation gap mid-replay; the build should log and
abandon prediction for the match rather than crashing the worker).

---

## 8. Configuration flag

Searched `src/` for env-flag conventions
(`grep -rn "os.environ.get\|os.getenv\|ENABLE_"`). Findings:

- `os.getenv("DATABASE_URL")` is the dominant pattern (logger.py:312,
  prediction_logger.py:118 etc.).
- `os.environ["RAPIDAPI_KEY"]` indexed directly
  ([tennis_feed.py:50](src/live/tennis_feed.py:50)).
- `os.environ.get("ODDS_API_KEY")` for optional credential
  ([live/odds_fetcher.py:77](src/live/odds_fetcher.py:77)).
- `os.environ.get("DATABASE_URL")` in the FastAPI backend
  ([live/backend.py:38](src/live/backend.py:38)).
- **No existing `ENABLE_<feature>` boolean toggle pattern.** Grep
  confirms zero hits in `src/`.

**Proposed minimal addition:** a single env var
`TENNIS_LIVE_PREDICTIONS_ENABLED` (or shorter — `LIVE_PREDICTIONS_ENABLED`),
read **once** at `MatchWorker.__init__` and stored on the instance:

```python
self._predictions_enabled = (
    os.environ.get("LIVE_PREDICTIONS_ENABLED", "0").lower()
    in {"1", "true", "yes"}
)
```

Read at module-import-of-collector time is also acceptable, but
per-worker capture means the env var can be flipped between worker
spawns without restarting the whole service (matches the existing
`spawn_pre_match_worker` pattern).

**Default off (`"0"`)** for the first deploy. The build prompt should
gate the entire prediction-step block on `self._predictions_enabled`
so the disabled case adds **zero** branches inside `_poll` after the
initial guard.

`.env.example` is empty-ish — only `DATABASE_URL` and `RAPIDAPI_KEY`
are documented there ([.env.example](.env.example) at top of repo).
The new flag does **not** need to be added to `.env.example` until
turned on in production.

---

## 9. Error handling spec

How `_poll` currently handles failures:

1. **Top-level loop** ([collector.py:84-94](src/live/collector.py:84)):
   ```python
   while self._running:
       try:
           self._poll()
       except Exception as exc:
           _log.warning("worker error ...")
   ```
   Anything that escapes `_poll` is caught, logged, and the worker
   keeps polling. This is the outermost safety net.
2. **API + parse + log_match_detail** ([collector.py:107-133](src/live/collector.py:107)):
   wrapped in `try/except`. On failure, sets `parsed_detail = None`
   and writes `POLL_ERROR` to `audit.poll_audit_log`. Subsequent
   `if parsed_detail:` block is skipped.
3. **`upsert_match_detail_points`** ([collector.py:209-225](src/live/collector.py:209)):
   wrapped in its own `try/except`. On failure, logs `POLL_ERROR`
   audit event but **continues** to the `winner_code` check. This is
   the shape we want to mirror.
4. **`get_first_server`** ([collector.py:197-207](src/live/collector.py:197)):
   wrapped in `try/except: pass`. Failures are silent because
   `TennisFeed.get_first_server` already audit-logs them via `_log_attempt`.
5. **`status in self._terminal_non_finished`** etc. are pure dict
   reads; no error paths.

**Target shape** for the new prediction-adapter call:

```python
if self._predictions_enabled:
    try:
        self._ensure_service_started()  # may construct + replay
        if self._service is not None:
            adapter = self._adapter  # lazy-init alongside service
            for pt in adapter.transition(prev_row, just_inserted_row):
                try:
                    self._service.process_point(pt)
                except RuntimeError as exc:
                    # Serve-alternation or score/signal-engine disagreement.
                    # Silent recovery per design: log forensic event, drop
                    # the prediction, continue the match.
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
                    # Abandon prediction for this match (next polls will
                    # try to re-feed and likely error again — see §14
                    # open question).
                    self._service = None
                    break
    except Exception as exc:
        # Anything else (DB error during replay, adapter bug, etc.):
        # log and continue. The prediction layer must never crash the
        # live tracker.
        _log.warning(
            "prediction step error for match %s: %s",
            self._match_id, exc,
        )
        poll_logger = getattr(self, "_poll_logger", None)
        if poll_logger is not None:
            poll_logger.log(
                event_type="POLL_ERROR",
                match_id=self._match_id,
                detail=f"prediction:{exc}"[:200],
                poll_cycle_id=cycle_id,
            )
```

Key properties:

- The inner `try`/`except RuntimeError` catches the explicit
  invariant violation from
  [live_prediction_service.py:286-290](src/live_prediction_service.py:286)
  (score / signal engine disagreement) and
  [live_prediction_service.py:338-348](src/live_prediction_service.py:338)
  (serve-alternation).
- `PredictionLogger.log` failures are already swallowed inside
  `PredictionLogger.log` itself
  ([prediction_logger.py:198-204](src/prediction_logger.py:198)) — no
  additional handling needed.
- The outer `try`/`except Exception` is the catch-all: DB lookups
  during replay, adapter bugs, missing IDs, missing surface. Anything
  unexpected becomes a `POLL_ERROR` audit row and the worker continues.
- The worker's *existing* outermost `try`/`except` at
  [collector.py:86-92](src/live/collector.py:86) is a backstop, but
  the prediction code should never reach it.

---

## 10. Audit event type addition

Current `event_type` values in use (from `audit.poll_audit_log`):

| `event_type` | Count (last collected) | Source |
|---|---|---|
| `TICK_START` | 2797 | [scheduler.py:91](src/live/scheduler.py:91) |
| `STATE_TRANSITION` | 37 | [scheduler.py:197,210,224](src/live/scheduler.py:197) |
| `POLL_ERROR` | (varies; e.g. 0 today) | [collector.py:128,221,313](src/live/collector.py:128) |
| `POINTS_RECEIVED` | 16936 | [collector.py:176](src/live/collector.py:176) |
| `NO_NEW_POINTS` | 44896 | [collector.py:183](src/live/collector.py:183) |
| `MATCH_DISCOVERED` | 219 | [collector.py:332,412](src/live/collector.py:332) |
| `MATCH_ENDED` | 147 | [collector.py:360](src/live/collector.py:360) |

Column constraints (from `pg_constraint`):

| Constraint | Type | Definition |
|---|---|---|
| `poll_audit_log_pkey` | primary | `PRIMARY KEY (id)` |
| `poll_audit_log_event_type_not_null` | not null | `NOT NULL event_type` |
| `poll_audit_log_id_not_null` | not null | `NOT NULL id` |
| `poll_audit_log_timestamp_not_null` | not null | `NOT NULL "timestamp"` |

No `CHECK` constraint on `event_type`, no enum, no FK. The column is
`VARCHAR(50)`; `PREDICTION_INVARIANT_VIOLATION` (28 chars) and any
similar string fits comfortably.

**Calling site:** the natural location is `MatchWorker._poll`, inside
the inner `try/except RuntimeError` (§9). The new event_type should be
`PREDICTION_INVARIANT_VIOLATION`. `PollLogger.log` signature
([poll_logger.py:68-99](src/poll_logger.py:68)) accepts kwargs
`event_type` (required), `match_id`, `detail`, `points_count`,
`poll_cycle_id` — the canonical shape for this case:

```python
poll_logger.log(
    event_type="PREDICTION_INVARIANT_VIOLATION",
    match_id=self._match_id,
    detail=f"set={set} game={game} {error_str}"[:200],
    poll_cycle_id=cycle_id,
)
```

`detail` is `TEXT` (no length limit) but we truncate to 200 chars to
match the convention at
[collector.py:130,223](src/live/collector.py:130).

---

## 11. Canonical replay SELECT

Per §3 column list, the adapter consumes: `polled_at`, `status`,
`home_sets_won`, `away_sets_won`, `home_set1_games`–`home_set3_games`,
`away_set1_games`–`away_set3_games`, `home_current_games`,
`away_current_games`, `home_current_point`, `away_current_point`,
`point_winner`, `first_server`.

The `live_adapter.py` `__main__` block's SELECT
([live_adapter.py:81-93](src/verification/live_adapter.py:81)) is
close but **misses `point_winner` and `first_server`** — both
required by the adapter. The canonical replay SELECT:

```sql
SELECT polled_at, status,
       home_sets_won, away_sets_won,
       home_set1_games, away_set1_games,
       home_set2_games, away_set2_games,
       home_set3_games, away_set3_games,
       home_current_games, away_current_games,
       home_current_point, away_current_point,
       point_winner, first_server
FROM live.match_states
WHERE match_id = %s
ORDER BY polled_at ASC
```

Bind: a single parameter, the integer `match_id`. Use `psycopg2`
with `%s` parameter binding (per project convention — see
[live_adapter.py:91-94](src/verification/live_adapter.py:91)). Use
`RealDictCursor` so the result is a list of dicts the adapter can
consume directly.

Empirical row volume per match: average **130.8** rows, median **127**,
p95 **211**, max **263**. A replay of a finished match is therefore
~130 in-memory iterations — negligible. Total table size **9 MB
heap + 3 MB index = ~9 MB total** for 31,656 rows across 242 matches.

---

## 12. `poll_cycle_id` linkage decision

Current state of `poll_cycle_id` in `_poll`:

- Created on every poll at [collector.py:105](src/live/collector.py:105):
  `cycle_id = uuid.uuid4()`.
- Passed through the API call ([collector.py:108-110](src/live/collector.py:108)),
  written to `audit.api_call_log.poll_cycle_id`
  ([logger.py:122](src/live/logger.py:122), via
  [api_logger.py](src/live/api_logger.py)).
- Passed to every `poll_logger.log(...)` call (e.g.
  [collector.py:179](src/live/collector.py:179)) so
  `audit.poll_audit_log.poll_cycle_id` lines up.
- **Not currently passed to `MatchLogger.upsert_match_detail_points`** —
  `live.match_states` has no `poll_cycle_id` column.

Confirmed: `live.predictions` does **not** have a `poll_cycle_id`
column today (recon §13 schema dump).

`PredictionLogger.log` signature
([prediction_logger.py:124-129](src/prediction_logger.py:124)) accepts
only `prediction` and `model_version`. Plumbing `poll_cycle_id` would
require:

1. Adding a `poll_cycle_id UUID NULL` column to `live.predictions` (one
   `ALTER TABLE … ADD COLUMN IF NOT EXISTS …`).
2. Extending `_COLUMN_ORDER` and `_build_insert_sql` in
   [prediction_logger.py:49-101](src/prediction_logger.py:49) — both
   derive from `_FEATURE_NAMES` so a single tuple change ripples.
3. Adding a `poll_cycle_id: Optional[UUID] = None` kwarg to
   `PredictionLogger.log` and weaving it through `_make_prediction` in
   the service (so the worker can pass it on each `process_point`).

**Pros:** forensic linkage from `live.predictions` back to the exact
API call (`audit.api_call_log`) and the audit event
(`audit.poll_audit_log`) that triggered it. The triad of these tables
already share `poll_cycle_id` for cross-correlation; predictions are
the only live-write table that doesn't.

**Cons:** schema migration, new SQL DDL script,
`PredictionLogger.log` API change, service-layer plumbing (passing
the cycle id from worker → service → logger on every prediction).
Three components touched.

**Recommendation: defer.** Reasons:

- `live.predictions` rows already carry `(match_id_int, set_number,
  game_number_in_set, model_version, predicted_at)` — the
  `predicted_at` timestamp gives close-enough linkage when joined to
  `audit.poll_audit_log` on `(match_id, timestamp ≈ predicted_at)`.
- Step 6D's stated discipline is "minimum viable wiring." Adding a
  column to `live.predictions` requires its own DDL migration, an
  extra column in the column-order contract, an extra prediction-logger
  signature change, and a service-level kwarg through `_make_prediction`.
- Forensic linkage matters most when an invariant violation fires —
  but that flows through `audit.poll_audit_log` with `poll_cycle_id`
  attached already (§10), so the violation timing is already linkable.

If the build prompt decides linkage is critical, the DDL is one
statement and the column-order contract in
[prediction_logger.py:59-66](src/prediction_logger.py:59) is the only
single source of truth that needs to change.

---

## 13. Empirical findings

All queries against the production `DATABASE_URL` from `.env`.
Read-only session (`conn.set_session(readonly=True)`).

### A. Sample transitions from a finished match

Picked **match 16160007** (`Karolína Plíšková vs Elena Rybakina`,
finished 2026-05-11 19:56:40 UTC, 88 state rows, final score 0-2 sets
for Rybakina, set scores 0-6 and 2-6, `first_server='away'`).

**Single-point transition** (clean 15→30, no compression):
```
PREV: polled_at=18:59:09  sets=0-0 games=0-0 pt=15-30 pw=away status=inprogress
CURR: polled_at=18:59:29  sets=0-0 games=0-0 pt=30-15 pw=home status=inprogress
```
This is actually two single-point steps compressed into one transition:
home advanced 15→30 (rank +1) AND away regressed 30→15 — which is
impossible in real tennis. The writer's `_derive_point_winner` would
read it as: `ch>ph` so `'home'`. **Note:** this looks like an API
glitch where the writer should ideally have rejected it; in practice
it does not. Recon §14 flags this.

A cleaner single-point case from the same match (browsing further):

```
prev sets=0-0 games=0-0 pt=0-0 (first observed row)
curr sets=0-0 games=0-0 pt=15-0 pw=home
```

(The very first row of the match, also `pw='home'` derived from
`_infer_first_row_winner`.)

**Multi-point compression** (sourced from broader 14-day window since
the chosen match had none — polling at 10s is dense enough to mostly
prevent compressions):
```
match 16098967 17:51:37:   0-15 → 30-15   pw=home  (1 home, no away)
match 16098967 17:56:01:   0-30 → 15-40   pw=home  (1 home + 1 away; writer credits home)
match 16098970 16:02:27:   15-0 → 30-15   pw=home  (1 home + 1 away)
match 16155398 13:58:35:   0-15 → 15-30   pw=home  (1 home + 1 away)
match 16098971 13:57:12:   15-30 → 40-40  pw=home  (1 home + 1 away)
```
Every multi-point compression observed in the last 14 days is the
"both sides won one point" shape, and the writer attributed it to
`'home'` in every case because `_derive_point_winner` short-circuits
on the first rank delta it detects (home advanced). The adapter
**cannot** trust `point_winner` for compression cases.

**Game-end transition** (in-set games incremented):
```
PREV: polled_at=19:01:21  sets=0-0 games=0-0 pt=40-A pw=away status=inprogress
CURR: polled_at=19:01:52  sets=0-0 games=0-1 pt=0-0  pw=away status=inprogress
```
Away player held service from advantage. The writer's retro fill
labels `curr.point_winner = 'away'` (game-winning point), and a
separate UPDATE statement sets `prev.point_winner = 'away'` too.

**Set-end transition** (in this match the first set ended 0-6):
```
PREV: polled_at=19:19:21  sets=0-0 games=0-5 pt=15-40 pw=away status=inprogress
CURR: polled_at=19:19:52  sets=0-1 games=0-0 pt=0-0  pw=away status=inprogress
```
Set ended; new row's `home_set1_games / away_set1_games` carry the
completed-set total (0-6); games reset to 0-0 for set 2. `point_winner='away'`
is the side that incremented `sets_won` (via
`_retro_winner_for_prev_game` going through the set-boundary branch).

**Tiebreak entry** (this match didn't have one — Plíšková dropped both
sets without reaching 6-6). Pulled from broader 7-day window:
```
match 16098924 16:58:23:  prev games=6-6 pt=0-0   →  curr games=6-6 pt=1-0  pw=home
match 16098928 15:54:09:  prev games=6-6 pt=0-0   →  curr games=6-6 pt=1-0  pw=home
match 16098929 12:04:09:  prev games=6-6 pt=0-0   →  curr games=6-6 pt=1-0  pw=home
```
Tiebreak entries are characterised by: prev shows regular vocab at
6-6, curr shows integer-string digits. Within tiebreaks the writer
will continue using `_rank_point_score` (the integer fallback at
[logger.py:198-201](src/live/logger.py:198)).

### B. NULL `point_winner` rows in match 16160007

Match 16160007 had **0** NULL `point_winner` rows. Sampling broader
to cover edge cases — 10 NULL rows from the last 7 days:

1 row at `notstarted`, `games=0-0 pt=0-0`. Reason:
`_derive_point_winner` returns NULL when `prev is None`,
`_infer_first_row_winner` returns NULL on `(0, 0)`. The live_adapter
filters these out anyway.

9 rows from match 16159253, all `inprogress` and all at `games=6-6`
in tiebreak vocabulary, e.g.:
```
sets=0-0 games=6-6 pt=4-4
sets=0-0 games=6-6 pt=3-1
sets=0-0 games=6-6 pt=1-1
```
These are tiebreak transitions where both digits incremented (e.g.
2-1 → 3-1 → 3-2 → 4-2 with one row missed). `_derive_point_winner`
parses tiebreak digits via `int()` but the rank check `ch>ph or ca<pa`
gives an ambiguous answer when the missing row had one home and one
away point (rare but possible). Specifically: same set + same games
+ both sides' tiebreak digits unchanged or both advanced equally →
NULL.

Overall NULL share over the last 7 days: **677 / 25,406 ≈ 2.66%**.

### C. `match_details` JSON shape

For match 16160007:
- `homeTeam.id = 20732` ✓ (present)
- `homeTeam.name = 'Karolína Plíšková'` ✓
- `awayTeam.id = 186312` ✓
- `awayTeam.name = 'Elena Rybakina'` ✓
- `event.groundType = 'Red clay'` ✓
- `tournament.name = 'WTA 1000 Rome, Italy'` ✓
- `tournament.category.slug = 'wta'` ✓

Distinct top-level event keys in raw_json["event"]: `awayScore`,
`awayTeam`, `awayTeamSeed`, `bet365ExcludedCountryCodes`, `changes`,
`crowdsourcingDataDisplayEnabled`, `currentPeriodStartTimestamp`,
`customId`, `defaultPeriodCount`, `eventFilters`, `feedLocked`,
`finalResultOnly`, `firstToServe`, `groundType`, `hasBet365LiveStream`,
`hasGlobalHighlights`, `homeScore`, `homeTeam`, `id`, `isEditor`,
`lastPeriod`, `periods`, `roundInfo`, `season`, `showTotoPromo`, `slug`,
`startTimestamp`, `status`, `time`, `tournament`, `venue`, `winnerCode`
(32 distinct keys).

A side note on `firstToServe`: the API exposes this top-level field
on `event` directly, possibly an alternative source for first-server
identification — confirm shape against an early-poll snapshot before
relying on it. (The recon does not investigate this further; current
code uses `/point-by-point`.)

**Surface distribution** in `audit.api_response_archive` over the
data we have: only `'Red clay'` was seen — but the archive is
deliberately date-bounded and this week is the Rome clay swing. The
canonical surface normaliser
([book/promoter.py:_normalize_surface:121-133](src/book/promoter.py:121))
handles `'clay'`, `'hard'`, `'grass'`, `'carpet'`, and falls back to
the raw lowered value otherwise.

### D. Table size

```
total: 9040 kB   (heap: 5824 kB)
row_count: 31656
distinct_matches: 242
```

Distribution of rows per match: avg 130.8, median 127, p95 211, max
263. **Replay cost for a single match restart: O(130) in-memory point
transitions** plus the one SELECT. Negligible.

### E. Extra empirical detail

- `event_type` column on `audit.poll_audit_log` is `VARCHAR(50)`; no
  CHECK or FK constraint. A new value of length 28
  (`PREDICTION_INVARIANT_VIOLATION`) fits.
- `live.predictions` has 71 columns total, PK on
  `(match_id_int, model_version, set_number, game_number_in_set)`. No
  `poll_cycle_id` column.

---

## 14. Open questions

Anything ambiguous or that needs Yusef's call before the build:

1. **Multi-point compression — per-point winner attribution.** The
   writer's `point_winner` only names the latest observable winner.
   §4 prescribes BFS through `_REGULAR_GAME_EDGES` to synthesise
   intermediate winners by edge-direction, but the regular-game graph
   in the validator (`_REGULAR_GAME_EDGES` at
   [validator.py:98](src/verification/validator.py:98)) is for path
   *existence* (it's symmetric across deuce). For multi-point
   compressions there is genuinely ambiguous ordering — the empirical
   case `0-30 → 15-40` could be home-then-away or away-then-home; the
   in-game stats it produces differ. **Decision needed:** OK to commit
   to a deterministic order (e.g. alternate, starting with the row's
   `point_winner` side), or skip predictions for the affected game?
2. **API-glitch transitions** (e.g. the `pt=15-30 → 30-15` observed
   in match 16160007 at 18:59:29 — home advanced but away
   *regressed* in the same step, which is impossible in tennis). The
   writer accepts these silently. The adapter would compute a
   negative rank delta and have no legal `_REGULAR_GAME_EDGES` path.
   **Decision needed:** treat as a forced re-anchor (drop the
   transition, advance state), or treat as a soft RuntimeError that
   abandons the prediction layer for the match?
3. **`raw_surface` capture location.** §5 listed three options. Pick
   one explicitly so the build prompt knows whether to extend
   `parse_match_detail` (touches more code, benefits other future
   consumers) or read directly from `raw_detail` in the worker
   (smaller diff, less reusable).
4. **`player_a_id` / `player_b_id` capture.** Same choice: pull from
   `raw_detail` per-poll, or capture from the discovery event in
   `MatchWorker.__init__` once (mirroring the `_country_*` pattern at
   [collector.py:40-41](src/live/collector.py:40))? Recommended path
   is the latter, but confirm.
5. **Recovery after invariant violation.** §9 proposes setting
   `self._service = None` and breaking out of the per-poll Point loop.
   Should subsequent polls keep trying to re-init the service (so a
   transient violation recovers) or stay disabled for the rest of the
   match? The non-deterministic ordering of multi-point compressions
   could keep tripping the same invariant; permanent disable per
   match is the safer default.
6. **Replay scope on long-running workers.** If a worker has been
   live for, say, 5 hours and `_first_server` is still NULL (the
   point-by-point endpoint never returns a server), should the
   `_first_server_max_attempts` exhaustion at
   [collector.py:195](src/live/collector.py:195) permanently disable
   the prediction layer for this match? Implicit answer: yes, because
   `start_match` is never callable without `first_server_is_a`. Worth
   making explicit in the build prompt.
7. **Writer return-contract change scope.** §2 proposes returning the
   inserted row dict. If we decide *not* to change the writer in 6D,
   the build prompt needs the alternative path (worker fetches
   `(prev, latest)` via its own `SELECT` after `upsert_match_detail_points`).
   Either is workable — confirm preference.
8. **PredictionLogger swap pattern during replay.** §7 picked
   attribute mutation (`self._service._prediction_logger = None`)
   as the smallest delta. If this feels too leaky (it touches a
   module-private attribute), a tiny `LivePredictionService.mute()` /
   `unmute()` API is one alternative — but it changes the public
   surface for the first time since 6C. Confirm preference.
9. **`first_server` vs `firstToServe` API field.** The raw event blob
   has a top-level `firstToServe` field that may make the
   `/point-by-point` round-trip redundant. Not investigated here; out
   of scope for 6D but worth a separate ticket.
10. **`poll_cycle_id` linkage on `live.predictions`.** §12 recommends
    defer. If the build prompt prefers to plumb it now, the changes
    are localised but touch the column-order contract in
    `prediction_logger.py` and require a schema migration.
