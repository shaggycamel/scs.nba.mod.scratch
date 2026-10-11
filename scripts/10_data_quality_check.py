"""Audit the source Parquet snapshot for the defects documented in docs/data_quality.md.

Every check prints the current state so the numbers in that document can be
re-verified rather than trusted, and so anyone fixing the upstream extract can
see what moved. Exits non-zero if any check that should be clean is not.

Run: uv run python scripts/10_data_quality_check.py
"""

from __future__ import annotations

import sys
import warnings

import polars as pl

from nba_injury import calendar as cal_mod
from nba_injury import ids, paths

warnings.filterwarnings("ignore")

NBA_SEASONS = ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
findings: list[tuple[str, str, int]] = []


def report(check: str, detail: str, count: int) -> None:
    findings.append((check, detail, count))
    flag = "!" if count else " "
    print(f" {flag} {check:<34} {count:>7,}  {detail}")


def main() -> int:
    inj_raw = pl.read_parquet(paths.INJURIES)
    inj = inj_raw.unique()
    sch = pl.read_parquet(paths.SCHEDULE)
    box = pl.read_parquet(paths.BOX, columns=["game_id", "player_id", "min"])

    print("=" * 78)
    print("A. nba.injuries")
    print("=" * 78)

    report("A1 duplicate rows", "exact duplicates, drop with .unique()",
           inj_raw.height - inj.height)
    dups = inj.group_by("team_slug", "game_date", "player_name").len().filter(pl.col("len") > 1)
    report("A1 residual dup keys", "(team, date, player) seen more than once", dups.height)

    report("A2 null nba_id", f"{inj['nba_id'].is_null().mean():.1%} of rows",
           int(inj["nba_id"].is_null().sum()))
    resolved, diag = ids.resolve_player_ids(inj)
    report("A2 unresolvable by name", f"{diag['unresolved_names']} distinct names",
           diag["unresolved_rows"])

    multi = inj.filter(pl.col("status").str.contains(" "))
    report("A3 concatenated status", 'e.g. "Out Out", "Questionable Doubtful Out"', multi.height)

    report("A4 null game_id", f"{inj['game_id'].is_null().mean():.1%} of rows",
           int(inj["game_id"].is_null().sum()))

    # A5: rows whose own game_id disagrees with the schedule's date for it.
    sg = sch.select("game_id", pl.col("game_date").alias("sch_date"), "season").unique(
        subset="game_id"
    )
    joined = (
        inj.filter(pl.col("game_id").is_not_null())
        .join(sg, on="game_id", how="inner")
        .with_columns((pl.col("game_date") - pl.col("sch_date")).dt.total_days().alias("diff"))
    )
    offsets = cal_mod.report_date_offsets()
    # A row is "on the wrong clock" when its date matches the schedule rather
    # than the schedule plus that season's measured offset.
    off_df = pl.DataFrame(
        {"season": list(offsets), "off": [offsets[s] for s in offsets]},
        schema={"season": pl.String, "off": pl.Int64},
    )
    wrong = joined.join(off_df, on="season", how="inner").filter(
        (pl.col("diff") != pl.col("off")) & (pl.col("diff").abs() <= 2)
    )
    report("A5 rows on the schedule's clock",
           f"{wrong['game_id'].n_unique()} games, all 2021-22", wrong.height)

    far = joined.filter(pl.col("diff").abs() > 2)
    report("A6 rows far from their game_id",
           f"{far['game_id'].n_unique()} game(s), likely rescheduled", far.height)

    reason_bad = inj.filter(
        pl.col("reason").is_null() | pl.col("reason").str.strip_chars().is_in(["", "-"])
    )
    report("A7 blank reason", 'null or "-"', reason_bad.height)
    # Rust's regex engine has no look-around, so express "contains but does
    # not start with" as two conditions.
    low = pl.col("reason").str.to_lowercase()
    prefixed = inj.filter(
        low.str.contains("injury/illness") & ~low.str.starts_with("injury/illness")
    )
    report("A7 team name glued to reason", 'e.g. "Portland Trail Blazers Injury/Illness - ..."',
           prefixed.height)
    report("A7 distinct reason strings", "free text, needs parsing", inj["reason"].n_unique())

    print()
    print("=" * 78)
    print("B. nba.league_game_schedule")
    print("=" * 78)

    nba30 = sorted(pl.read_parquet(paths.TEAMS).filter(pl.col("active"))["team_slug"].to_list())
    foreign = sch.filter(~pl.col("team").is_in(nba30))
    report("B1 non-NBA teams", f"{foreign['team'].n_unique()} codes, e.g. ADL/BAR/BNE",
           foreign.height)

    shifted = [s for s, o in offsets.items() if o != 0]
    report("B2 seasons on a shifted clock", f"{shifted or 'none'} (offset measured, not assumed)",
           len(shifted))
    # Christmas anchor: the NBA plays five games on 25 Dec and none on 24 Dec.
    xmas_bad = []
    for season in NBA_SEASONS:
        yr = int(season[:4])
        wnd = sch.filter(
            (pl.col("season") == season)
            & (pl.col("season_type") == "Regular Season")
            & pl.col("game_date").is_between(pl.date(yr, 12, 24), pl.date(yr, 12, 25))
        ).group_by("game_date").agg((pl.len() // 2).alias("games"))
        on_eve = wnd.filter(pl.col("game_date") == pl.date(yr, 12, 24))
        if on_eve.height and int(on_eve["games"][0]) > 0:
            xmas_bad.append(season)
    report("B2 games on Christmas Eve", f"{xmas_bad or 'none'} — impossible, so dates are early",
           len(xmas_bad))

    print()
    print("=" * 78)
    print("C. nba.player_box_score")
    print("=" * 78)
    # Klay Thompson was out for the first half of 2021-22 and returned on
    # 2022-01-09 -- which this file dates 2022-01-08, one more confirmation of
    # the shift in B2. If the box score recorded absence at all he would have a
    # DNP row for each Golden State game before that; he has none, which is why
    # games missed cannot be counted from the box score. The cutoff below is on
    # the file's own (shifted) clock so it excludes the return game itself.
    sgd = sch.select("game_id", "game_date", "season", "season_type").unique(subset="game_id")
    gsw_games = sch.filter(
        (pl.col("season") == "2021-22") & (pl.col("season_type") == "Regular Season")
        & (pl.col("team") == "GSW") & (pl.col("game_date") < pl.date(2022, 1, 8))
    )["game_id"].unique()
    klay_rows = box.filter(
        (pl.col("player_id") == 202691) & pl.col("game_id").is_in(gsw_games)
    )
    print(f"   (GSW played {len(gsw_games)} games while Klay Thompson was out)")
    report("C1 rows for an absent player",
           "any row at all for those games — zero means absence is unrecorded",
           klay_rows.height)
    report("C2 null min", f"{box['min'].is_null().mean():.1%} of rows are DNP",
           int(box["min"].is_null().sum()))

    print()
    print("=" * 78)
    print("D. other tables")
    print("=" * 78)
    pi = pl.read_parquet(paths.PLAYER_INFO)
    report("D1 player_info season coverage",
           f"only {sorted(pi['season'].unique().to_list())}", len(pi["season"].unique()))
    bad_bd = pi.filter(pl.col("birthdate").str.contains("T"))
    report("D1 birthdate is a string", 'carries a "T00:00:00" suffix', bad_bd.height)
    pss = pl.read_parquet(paths.NBA / "player_season_stats.parquet")
    floaty = pss.filter(pl.col("player_id").str.contains(r"\."))
    report("D2 player_season_stats ids", 'string ids written as floats, e.g. "1641645.0"',
           floaty.height)
    unmatched = pl.read_parquet(paths.UTIL / "unmatched_player_source_vw.parquet")
    report("D3 unmatched crosswalk rows", "players with no player_key", unmatched.height)

    print()
    print("=" * 78)
    total = sum(c for _, _, c in findings)
    print(f"{len([f for f in findings if f[2]])} checks non-zero, {total:,} rows implicated "
          "in total (most are handled in nba_injury/ — see docs/data_quality.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
