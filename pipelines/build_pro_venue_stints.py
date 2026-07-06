#!/usr/bin/env python3
"""
Build canonical professional team/venue stint ranges.

The cache is derived from source-specific team-season venue tables:

- MLB: Lahman HomeGames + Teams + Parks
- NBA: hoopR schedule-derived team home venues
- NFL: nflverse schedule-derived team home stadiums

It is a team/venue/year lookup, not a player appearance table.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
OUT_DB = SCRATCH / "pro_venue_stints.sqlite"
SUMMARY_PATH = SCRATCH / "pro_venue_stints_summary.json"
CURATED_STINTS_PATH = ROOT / "data" / "curation" / "pro_venue_stints.json"

MLB_DB = SCRATCH / "mlb_enrichment.sqlite"
NBA_DB = SCRATCH / "nba_enrichment.sqlite"
NFL_STADIUM_DB = SCRATCH / "nfl_stadium_enrichment.sqlite"

NFL_TEAM_NAMES_BY_ABBR = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "OAK": "Oakland Raiders",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SD": "San Diego Chargers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "STL": "St. Louis Rams",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}


def nfl_team_name_for_season(team: str | None, season: int | str | None) -> str:
    team = str(team or "")
    try:
        year = int(season)
    except (TypeError, ValueError):
        year = None
    if team == "LV" and year is not None and year <= 2019:
        return "Oakland Raiders"
    if team == "LAR" and year is not None and 1995 <= year <= 2015:
        return "St. Louis Rams"
    if team == "LAC" and year is not None and year <= 2016:
        return "San Diego Chargers"
    if team == "WAS" and year is not None:
        if year <= 2019:
            return "Washington Redskins"
        if year <= 2021:
            return "Washington Football Team"
    return NFL_TEAM_NAMES_BY_ABBR.get(team, team)


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.create_function("nfl_team_name", 2, nfl_team_name_for_season)
    con.executescript(
        """
        create table pro_venue_team_seasons (
            sport text not null,
            league text,
            season integer not null,
            team_id text not null,
            team_name text not null,
            venue_id text not null,
            venue_name text not null,
            city text,
            state text,
            country text,
            latitude real,
            longitude real,
            geocode_status text,
            source text not null,
            source_key text,
            notes text,
            primary key (sport, season, team_id, venue_id)
        );

        create table pro_venue_stints (
            stint_id text primary key,
            sport text not null,
            league text,
            team_id text not null,
            team_name text not null,
            venue_id text not null,
            venue_name text not null,
            start_year integer not null,
            end_year integer not null,
            seasons integer not null,
            city text,
            state text,
            country text,
            latitude real,
            longitude real,
            geocode_status text,
            source text not null,
            source_key text,
            notes text
        );

        create index idx_pro_venue_stints_sport_team_year
            on pro_venue_stints(sport, team_id, start_year, end_year);
        create index idx_pro_venue_team_seasons_sport_team_year
            on pro_venue_team_seasons(sport, team_id, season);
        """
    )
    return con


def attach_inputs(con: sqlite3.Connection) -> None:
    if MLB_DB.exists():
        con.execute(f"attach database {sql_quote(str(MLB_DB))} as mlb")
    if NBA_DB.exists():
        con.execute(f"attach database {sql_quote(str(NBA_DB))} as nba")
    if NFL_STADIUM_DB.exists():
        con.execute(f"attach database {sql_quote(str(NFL_STADIUM_DB))} as nfl")


def load_team_seasons(con: sqlite3.Connection) -> None:
    attached = {row[1] for row in con.execute("pragma database_list")}
    if "mlb" in attached:
        con.executescript(
            """
            insert or replace into pro_venue_team_seasons
            (sport, league, season, team_id, team_name, venue_id, venue_name, city, state,
             country, latitude, longitude, geocode_status, source, source_key, notes)
            select
                'MLB',
                h."league.key",
                cast(h."year.key" as integer),
                h."team.key",
                coalesce(nullif(t.team_name, ''), h."team.key"),
                h."park.key",
                p.park_name,
                p.city,
                p.state,
                p.country,
                p.latitude,
                p.longitude,
                p.geocode_status,
                'Lahman HomeGames + Teams + Parks',
                h."team.key" || '|' || h."park.key",
                'Rows represent team-season home park usage from Lahman HomeGames; teams can have more than one home park in a season.'
            from mlb.lahman_homegames h
            left join mlb.lahman_teams t
              on t.yearID = h."year.key"
             and t.lgID = h."league.key"
             and t.teamID = h."team.key"
            left join mlb.mlb_park_geocode_cache p
              on p.park_key = h."park.key"
            where h."park.key" is not null and h."park.key" != '';
            """
        )

    if CURATED_STINTS_PATH.exists():
        curated_rows = json.loads(CURATED_STINTS_PATH.read_text(encoding="utf-8"))
        season_rows = []
        for row in curated_rows:
            start_year = int(row["start_year"])
            end_year = int(row["end_year"])
            for season in range(start_year, end_year + 1):
                season_rows.append(
                    {
                        "sport": row["sport"],
                        "league": row.get("league", row["sport"]),
                        "season": season,
                        "team_id": row.get("team_id") or row["team_name"],
                        "team_name": row["team_name"],
                        "venue_id": row.get("venue_id") or row["venue_name"],
                        "venue_name": row["venue_name"],
                        "city": row.get("city"),
                        "state": row.get("state"),
                        "country": row.get("country"),
                        "latitude": row.get("latitude"),
                        "longitude": row.get("longitude"),
                        "geocode_status": row.get("geocode_status", "curated_label_only"),
                        "source": row.get("source", "Curated pro venue stint"),
                        "source_key": row.get("source_url", ""),
                        "notes": row.get("notes", "Curated historical team venue range."),
                    }
                )
        con.executemany(
            """
            insert or replace into pro_venue_team_seasons
            (sport, league, season, team_id, team_name, venue_id, venue_name, city, state,
             country, latitude, longitude, geocode_status, source, source_key, notes)
            values
            (:sport, :league, :season, :team_id, :team_name, :venue_id, :venue_name, :city, :state,
             :country, :latitude, :longitude, :geocode_status, :source, :source_key, :notes)
            """,
            season_rows,
        )
    if "nba" in attached:
        con.executescript(
            """
            insert or replace into pro_venue_team_seasons
            (sport, league, season, team_id, team_name, venue_id, venue_name, city, state,
             country, latitude, longitude, geocode_status, source, source_key, notes)
            select
                'NBA',
                'NBA',
                cast(v.season as integer),
                v.home_id,
                v.home_display_name,
                v.venue_id,
                v.venue_full_name,
                g.city,
                g.state,
                g.country,
                g.latitude,
                g.longitude,
                g.geocode_status,
                'hoopR schedules home venues',
                v.home_display_name || '|' || v.venue_id,
                'Rows represent the dominant team-season home venue inferred from hoopR schedules.'
            from nba.nba_team_home_venues v
            left join nba.nba_venue_geocode_cache g
              on g.venue_id = v.venue_id
            where v.venue_id is not null and v.venue_id != '';
            """
        )
    if "nfl" in attached:
        con.executescript(
            """
            insert or replace into pro_venue_team_seasons
            (sport, league, season, team_id, team_name, venue_id, venue_name, city, state,
             country, latitude, longitude, geocode_status, source, source_key, notes)
            select
                'NFL',
                'NFL',
                cast(s.season as integer),
                s.team,
                nfl_team_name(s.team, s.season),
                s.stadium_id,
                s.stadium_name,
                g.city,
                null,
                g.country,
                g.latitude,
                g.longitude,
                g.match_status,
                s.source,
                s.team || '|' || s.stadium_id,
                'Rows represent the dominant team-season home stadium inferred from nflverse schedules.'
            from nfl.nfl_team_season_stadiums s
            left join nfl.nfl_stadium_geocode_cache g
              on g.stadium_id = s.stadium_id
             and g.stadium_name = s.stadium_name
            where s.stadium_id is not null and s.stadium_id != '';
            """
        )


def team_season_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return list(
        con.execute(
            """
            select *
            from pro_venue_team_seasons
            order by sport, team_id, venue_id, season
            """
        )
    )


def row_stint_key(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        row["sport"],
        row["league"],
        row["team_id"],
        row["team_name"],
        row["venue_id"],
        row["venue_name"],
        row["city"],
        row["state"],
        row["country"],
        row["latitude"],
        row["longitude"],
        row["geocode_status"],
        row["source"],
    )


def build_stints(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    stints: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    current_key: tuple[Any, ...] | None = None
    for row in rows:
        key = row_stint_key(row)
        season = int(row["season"])
        if current and current_key == key and season == int(current["end_year"]) + 1:
            current["end_year"] = season
            current["seasons"] = int(current["seasons"]) + 1
            current["source_key"] = f"{current['team_id']}|{current['venue_id']}|{current['start_year']}-{current['end_year']}"
            continue
        if current:
            stints.append(current)
        current_key = key
        current = {
            "stint_id": f"{row['sport']}|{row['team_id']}|{row['venue_id']}|{season}",
            "sport": row["sport"],
            "league": row["league"],
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "venue_id": row["venue_id"],
            "venue_name": row["venue_name"],
            "start_year": season,
            "end_year": season,
            "seasons": 1,
            "city": row["city"],
            "state": row["state"],
            "country": row["country"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "geocode_status": row["geocode_status"],
            "source": row["source"],
            "source_key": f"{row['team_id']}|{row['venue_id']}|{season}-{season}",
            "notes": row["notes"],
        }
    if current:
        stints.append(current)
    return stints


def insert_stints(con: sqlite3.Connection, stints: list[dict[str, object]]) -> None:
    con.executemany(
        """
        insert or replace into pro_venue_stints
        (stint_id, sport, league, team_id, team_name, venue_id, venue_name, start_year, end_year,
         seasons, city, state, country, latitude, longitude, geocode_status, source, source_key, notes)
        values
        (:stint_id, :sport, :league, :team_id, :team_name, :venue_id, :venue_name, :start_year, :end_year,
         :seasons, :city, :state, :country, :latitude, :longitude, :geocode_status, :source, :source_key, :notes)
        """,
        stints,
    )
    con.commit()


def write_summary(con: sqlite3.Connection) -> dict[str, object]:
    summary = {
        "sqlite_cache": repo_path(OUT_DB),
        "team_seasons_by_sport": dict(
            con.execute(
                """
                select sport, count(*)
                from pro_venue_team_seasons
                group by sport
                order by sport
                """
            ).fetchall()
        ),
        "stints_by_sport": dict(
            con.execute(
                """
                select sport, count(*)
                from pro_venue_stints
                group by sport
                order by sport
                """
            ).fetchall()
        ),
        "year_ranges_by_sport": {
            row["sport"]: {"start_year": row["start_year"], "end_year": row["end_year"]}
            for row in con.execute(
                """
                select sport, min(start_year) as start_year, max(end_year) as end_year
                from pro_venue_stints
                group by sport
                order by sport
                """
            )
        },
        "notes": [
            "MLB rows can include multiple home parks per team-season because Lahman HomeGames preserves split home schedules.",
            "NBA rows start with the hoopR schedule era currently cached in the repo.",
            "NFL rows start with the nflverse schedule era currently cached in the repo.",
            "Curated rows are sourced historical ranges used only where schedule-derived venue coverage is unavailable.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    con = init_db(OUT_DB)
    attach_inputs(con)
    load_team_seasons(con)
    insert_stints(con, build_stints(team_season_rows(con)))
    summary = write_summary(con)
    con.close()
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
