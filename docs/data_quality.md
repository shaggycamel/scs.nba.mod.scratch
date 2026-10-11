# Data defects in the `nba` Parquet snapshot, and how this repo works around them

Written as a handover. Everything below was found while building
`nba_injury/` (injury-duration modelling) against `data/` as snapshotted by
`data/gen.py` from the `nba` schema on CockroachDB.

**Nothing here has been fixed at source.** Every item is worked around in
code, in the module named under *Handled in*. If you fix one upstream, the
workaround becomes a no-op rather than a conflict — none of them rewrite
values, they only drop, re-key or re-date rows. The two exceptions marked
**NOT HANDLED** are known residual defects.

To see the current state of every item:

```sh
uv run python scripts/10_data_quality_check.py
```

That script is the executable version of this document. Re-run it after any
upstream change; the counts below are its output as of 2026-10-11.

---

## Severity summary

| # | Defect | Rows | Severity | Handled |
|---|---|---|---|---|
| A5 | 35 injury rows dated on the schedule's wrong clock | 35 | low | **NOT HANDLED** |
| A6 | 17 injury rows 21 days from their own `game_id` | 17 | low | **NOT HANDLED** |
| B2 | 2021-22 schedule dates are one day early | whole season | **critical** | `calendar.py` |
| C1 | Box score omits absent players entirely | — | **critical** | `spells.py` |
| A2 | `nba_id` null on 18.5% of injury rows | 11,633 | high | `ids.py` |
| A1 | Exact duplicate injury rows from 2026-02-20 | 1,406 | high | `spells.py` |
| A7 | `reason` is free text, 2,914 distinct strings | all | high | `taxonomy.py` |
| A4 | `game_id` null on 19.2% of injury rows | 12,059 | medium | `spells.py` |
| A3 | Concatenated `status` values | 21 | low | `spells.py` |
| B1 | Non-NBA teams in the schedule | 736 | medium | `calendar.py` |
| C2 | `min` null for DNP rows | 105,394 | low | `spells.py` |
| D1 | `player_info` covers two seasons; string birthdate | — | medium | `features.py` |
| D2 | `player_season_stats.player_id` is a float-string | 5,039 | low | avoided |

---

## A. `nba.injuries`

### A1. Exact duplicate rows from 2026-02-20 onwards — 1,406 rows

Every row filed on or after 2026-02-20 appears two to six times, byte for
byte. 478 (team, date, player) keys are affected.

```python
inj = pl.read_parquet("data/nba/injuries.parquet")
inj.height - inj.unique().height          # 1406
```

They are true duplicates — identical `status`, `reason` and `matchup` — so
`.unique()` is lossless and leaves zero residual duplicate keys. Looks like a
re-ingestion that appended instead of upserting; worth checking the loader's
write mode around that date.

**Impact if ignored:** a player's absence is counted up to six times, which
inflates every per-game aggregate and double-counts spells.

**Handled in:** `spells.load_report()` — `.unique()` before anything else.

### A2. `nba_id` is null on 18.5% of rows — 11,633 rows

Null rate varies by season, from 2% in 2024-25 to 38% in 2021-22, so a naive
`drop_nulls` would silently delete a third of the earliest season.

Worse, some `player_name` values are **two players' names interleaved**:

```
"Luka Reaves Doncic"        (Luka Doncic + Austin Reaves)
"Alex Mitchell Caruso"      (Alex Caruso + Davion Mitchell)
"Marvin Coulibaly Bagley III"
```

**Handled in:** `ids.resolve_player_ids()` — a four-step ladder: the existing
`nba_id`, then exact `player_name` against the `util` crosswalk, then against
the box score, then a punctuation- and suffix-stripped form of each. Names
that map to more than one id are dropped from the lookup rather than guessed.

**Residual:** 158 rows across 25 names survive all four steps and are
dropped. Almost all are the interleaved names above; a scraper fix upstream
would recover them. 0.25% of the file.

### A3. Concatenated `status` values — 21 rows

```
"Out Out", "Out Out Out", "Questionable Doubtful Out", "Available Out Out"
```

Same root cause as A2 — adjacent report rows merged. Note these are *not* the
same rows as A2: a merged row can have a clean name and a doubled status.

**Handled in:** `spells.load_report()` takes the first whitespace token.
Defensible because the first token is the one belonging to the named player.

### A4. `game_id` is null on 19.2% of rows — 12,059 rows

Nearly all of 2021-22. **This one is a trap**: the column has the same name
as `league_game_schedule.game_id`, so a naive join silently keeps the
injury-side null and drops the whole season. That cost a debugging session
here — see `spells.load_report()`, which renames it to `report_game_id` for
exactly this reason.

**Handled in:** `spells.build_panel()` maps report rows to games on
`(team_slug, report_date)` instead, which is unique because no team plays
twice in a day.

### A5. 35 rows dated on the schedule's wrong clock — **NOT HANDLED**

Four 2021-22 games are filed under the date that `league_game_schedule` uses
(see B2) rather than the true date. They are precisely the 2021-22 rows that
carry a non-null `game_id`, so they evidently came from whatever back-fill
populated that column.

The giveaway is that one game appears twice, under both dates:

```
HOU@POR  2022-03-25: 11 rows, all 11 carrying a game_id   <- wrong clock
         2022-03-26:  9 rows, none carrying a game_id     <- correct
```

The other three (NOP@UTA 2021-11-26, MIN@SAC 2022-02-08, TOR@BKN 2022-02-28)
appear *only* on the wrong date.

**Impact:** `calendar.py` applies a uniform +1 day correction for 2021-22, so
these rows — already on the shifted clock — land on the *previous* game. All
35 map to the wrong game; none map correctly. That can create a spurious
one-game absence or stretch a spell by a game for a handful of 2021-22
players. It is 0.056% of the file and does not move any published figure.

**The fix, if you want it:** prefer the row's own `report_game_id` when it is
non-null and fall back to the `(team_slug, report_date)` join only when it is
null. That is strictly better than the current code for every season, not
just 2021-22. It is roughly fifteen lines in `spells.build_panel()`, but it
changes the spell boundaries slightly, so it needs a full pipeline re-run and
a re-sync of both reports (`scripts/09_sync_report_numbers.py`).

### A6. 17 rows 21 days away from their own `game_id` — **NOT HANDLED**

One game, `22500529` (MIA@CHI), filed under 2026-01-08 while the schedule has
it on 2026-01-29. Almost certainly a postponed game where the report kept the
original date. Harmless here because the date-based join sends those rows to
whatever game actually happened on 2026-01-08, but flag it if you start
trusting `game_id`.

### A7. `reason` is unvalidated free text

2,914 distinct strings for what is really a few dozen clinical categories.
The intended shape is

```
Injury/Illness - <Side> <Body part>; <Ailment>
```

but in practice:

* **925 rows** are blank — `null` or a bare `"-"`.
* **6 rows** have the team name glued to the front:
  `"Portland Trail Blazers Injury/Illness - Abdomen; Tendinopathy"`.
* Casing is inconsistent for the same concept: `Injury Recovery` /
  `Injury recovery`, `N/a` / `N/A`, `Bone Bruise` / `Bone bruise`.
* The same pathology is spelled many ways: `Tendinitis`, `Tendonitis`,
  `Tendinopathy`, `Tendinosis`.
* Compound entries are common: `"Abdominal; Strain/Left Ankle Sprain"`.

**Handled in:** `taxonomy.py`, which parses `reason` into `reason_category`,
`body_region`, `body_side`, `ailment_class` and a set of severity flags, with
ordered keyword rules. After parsing, 1.2% of injury rows have an unknown
body region and 2.7% an unknown ailment.

**One ordering trap worth knowing:** the literal prefix `"Injury/Illness"`
contains the word *illness*, so a rule list that tests for illness before
injury classifies every structured row as an illness. The injury/illness
split has to come from the payload, not the prefix.

---

## B. `nba.league_game_schedule`

### B1. Non-NBA teams — 736 rows, 43 team codes

Preseason fixtures against international and G-League sides: `ADL`, `ALB`,
`BAR`, `BAU`, `BJD`, `BNE` and others. Harmless but they break any assumption
that the file contains 30 teams.

**Handled in:** `calendar._raw_schedule()` filters to the 30 active franchises
from `nba.teams` and drops `season_type == "Pre Season"`.

### B2. 2021-22 dates are one day early — whole season, **critical**

This is the big one. Every 2021-22 row in `league_game_schedule` is dated one
day before the game actually happened. Later seasons are correct.

Three independent confirmations:

1. **Christmas.** The NBA plays five games on 25 December and none on the
   24th. This file has **five games on 2021-12-24** and eight on the 25th.
   From 2022-23 on it correctly shows five on the 25th and none on the 24th.
   `nba.injuries` has it right for 2021-22: five matchups on 25 December.
2. **All-Star.** The 2022 All-Star Game was Sunday 20 February 2022. The file
   says 2022-02-19. The 2023 and 2024 entries are correct.
3. **Opening night.** The 2021-22 season opened on Tuesday 19 October 2021
   with BKN@MIL and GSW@LAL. `nba.injuries` says 2021-10-19;
   `league_game_schedule` says 2021-10-18 for both `game_id`s.

A fourth, incidentally: Klay Thompson returned on 2022-01-09 and the box
score dates that game 2022-01-08.

Also check pre-2021 seasons if you use them — 2009-10 opens on 2009-10-26 in
this file against a real 2009-10-27, so the defect is probably not confined
to 2021-22. It could not be verified here because the injury report only
starts in October 2021, and nothing else in the snapshot provides an
independent date anchor.

**Impact if ignored:** injury-report rows fail to join to games for an entire
season, and any day-level feature (rest days, back-to-backs, schedule
density) is wrong for that season.

**Handled in:** `calendar.report_date_offsets()` **measures** the offset per
season rather than hard-coding it — for each season it scores candidate
offsets of −1, 0 and +1 by how many `(team, date)` pairs align between the
report and the schedule, and takes the best. Current output:

```
2021-22: offset +1  matches 2476/2634   (0: 414, -1: 1533)
2022-23: offset +0  matches 2465/2628
2023-24: offset +0  matches 2457/2624
2024-25: offset +0  matches 2447/2628
2025-26: offset +0  matches 2529/2630
```

The separation is wide enough that this is not a coin flip. The corrected
date lands in a **new** column, `report_date`; `game_date` is left untouched,
because a constant per-season offset cancels in any within-season difference
and only matters for joining.

**If you fix this upstream**, the measured offset becomes 0 and the code keeps
working with no change.

---

## C. `nba.player_box_score`

### C1. The box score does not record absence — **critical, semantic**

A player on a long-term absence is simply **missing from the file**. There is
no DNP row, no zero-minute row, nothing.

```
Golden State played 38 games while Klay Thompson was out in 2021-22.
Klay Thompson box-score rows for those 38 games: 0
```

His first 2021-22 regular-season row is his return game.

**Impact if ignored:** games missed cannot be counted from the box score, and
any attempt to do so silently produces zero for exactly the players who were
hurt longest. This is the single most important thing to know about the
snapshot.

**Handled in:** `spells.build_panel()` derives availability from the injury
report and uses the box score only to establish that a player *did* play
(`min > 0`).

### C2. `min` is null on 17.9% of rows — 105,394 rows

These are DNP rows for players who were active but did not play; `comment`
carries the reason (`"DNP - Coach's Decision"`, `"DND - Injury/Illness"`).
Null means did-not-play, not missing data.

**Handled in:** filled with `0.0`, and "played" is defined as `min > 0`.

---

## D. Other tables

### D1. `nba.player_info`

* Covers **only 2024-25 and 2025-26**, so it cannot supply season-specific
  attributes for earlier seasons.
* `birthdate` is a **string** with a `T00:00:00` suffix (1,184 rows), not a
  date.

**Handled in:** `features.player_attributes()` takes the most recent row per
player and uses only genuinely time-invariant fields (birthdate, listed
height and weight, draft position), computing age per spell from the
birthdate. Players who retired before 2024-25 are missing entirely; 759 of
the 766 players in the spell set are covered.

### D2. `nba.player_season_stats.player_id` is a float-string — 5,039 rows

Values like `"1641645.0"`. A direct `cast(Int64)` raises. Cast via `Float64`
first, or strip the suffix.

**Handled in:** avoided. `player_info` covers 99% of the players needed, so
this table is not used.

### D3. `util.unmatched_player_source_vw`

Currently **empty**, which is the healthy state — a non-empty file means the
NBA/Statyx crosswalk has gaps that will silently drop rows from any
cross-source join. Worth asserting on, not just inspecting.

---

## E. Pitfalls in the processing code, not the data

Three bugs that cost real debugging time here and are easy to reintroduce.
All are fixed in this repo; they are listed because they are properties of
polars and of this data shape, not of this project.

1. **`group_by` order is not guaranteed.** `standings.playin_probability()`
   draws from a seeded generator inside a loop over groups. With an unstable
   group order the simulation was irreproducible, and so was every model
   downstream of it. Iterate over sorted keys.
2. **`sort` is not stable on ties.** `(player_id, season, game_date, game_id)`
   looks unique but is not: a player traded between two teams that then play
   each other is attached to **both sides of one game**. 44 rows in this
   snapshot. The whole per-player game index shifted between runs. Add
   `team_slug` as a final tiebreaker.
3. **`group_by().agg(pl.col(x).first())` does not mean "the first row".** It
   makes no promise about within-group order. Where it meant "as filed on the
   first missed game", it has to be written
   `pl.col(x).sort_by("player_game_idx").first()`.

With all three fixed the pipeline reproduces byte-identically, apart from one
float's last bit in a random forest metric, which is parallel summation order
in scikit-learn.

---

## F. What to do with this

If you are fixing the upstream extract, in priority order:

1. **B2** — correct the 2021-22 (and probably earlier) schedule dates. Check
   every season against the Christmas and All-Star anchors.
2. **A1** — stop the duplicate writes from 2026-02-20.
3. **A2 / A3** — fix the scraper's row splitting, which is producing both the
   interleaved names and the concatenated statuses.
4. **A5** — re-date the 35 rows, or just drop their `game_id` so they stop
   looking authoritative.
5. **A7** — a validated `reason` at source would retire most of
   `taxonomy.py`, but the parser is tested and in no hurry.

**C1 is not a defect to fix** — it is how NBA box scores work. It is listed
because any new work against this snapshot needs to know it.

After any change, run `scripts/10_data_quality_check.py` to see what moved,
then the full pipeline (`scripts/01_build.py` … `09_sync_report_numbers.py`),
which regenerates the reports and re-syncs their numbers automatically.
