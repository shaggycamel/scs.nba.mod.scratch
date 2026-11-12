# scs.nba.mod.scratch

Ad hoc NBA analysis, aimed at predicting player usage (`usg_pct`) for a player's
next upcoming game using only information known before tip-off. Python + `uv`,
`polars`, read-only database access; see the workspace `AGENTS.md` for project
rules.

## Data

`data/gen.py` snapshots the source database to Parquet under `data/`, one
directory per schema:

- `data/nba/` — NBA.com-sourced tables (box scores, schedule, rosters,
  injuries, transactions).
- `data/statyx/` — Statyx-sourced tables (advanced stats, play types,
  contracts, usage shock, etc.).
- `data/util/` — views, not base tables (see the `table_type` filter in
  `data/gen.py`).

### `data/util/` — matching NBA and Statyx ids

The two sources number players differently: `player_id` in `data/nba/*` is an
NBA.com id, `player_id` in `data/statyx/*` is a Statyx id. The `util` objects
are the crosswalk between them, so any join across the two sources must go
through them rather than through player names.

- `player_id_map_vw.parquet` — one row per player, keyed by `player_key`, with
  the per-platform id and name side by side (`nba_id`/`nba_name`,
  `statyx_id`/`statyx_name`, plus `espn_*` and `yahoo_*`). This is the table to
  join on: map NBA `player_id` → `player_key` via `nba_id`, and `player_key` →
  Statyx `player_id` via `statyx_id`. Platform ids are nullable — a player
  present in one source may have no counterpart id in another.
- `active_player_vw.parquet` — long form of the same mapping, one row per
  (`season`, `platform`, `source_id`), carrying `player_key` and
  `conformed_name`. Use it when the match needs to be season-aware.
- `player_directory_vw.parquet` — every (`season`, `platform`, `source_id`,
  `source_name`) seen across the sources, matched or not.
- `unmatched_player_source_vw.parquet` — the directory rows with no
  `player_key`, i.e. players that failed to match. A non-empty file here means
  the crosswalk has gaps that will silently drop rows from cross-source joins.
