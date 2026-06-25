#!/usr/bin/env python3
"""
Build the unified SQLite warehouse from enrichment caches.

This pipeline reads derived MLB, NFL, and Wikidata cache databases from
scratch/ and writes the query-ready application database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
OUT_DB = SCRATCH / "hometown_heroes.sqlite"
SUMMARY = SCRATCH / "hometown_heroes_sql_summary.json"

MLB_DB = SCRATCH / "mlb_enrichment.sqlite"
NFL_DB = SCRATCH / "nfl_enrichment.sqlite"
WIKIDATA_DB = SCRATCH / "wikidata_education_enrichment.sqlite"
WIKIDATA_BIRTHPLACE_DB = SCRATCH / "wikidata_birthplace_enrichment.sqlite"


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def stable_id(*parts: object) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def require_inputs() -> None:
    missing = [str(path) for path in (MLB_DB, NFL_DB, WIKIDATA_DB) if not path.exists()]
    if missing:
        raise SystemExit("Missing required cache DBs: " + ", ".join(missing))


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        pragma foreign_keys = off;

        drop table if exists players;
        drop table if exists locations;
        drop table if exists player_location_events;
        drop table if exists source_snapshots;
        drop view if exists geocoded_player_location_events;
        drop view if exists player_event_summary;
        drop view if exists sf_50mi_non_pro_player_pool;

        create table players (
            sport text not null,
            player_id text not null,
            display_name text,
            birth_date text,
            birth_year integer,
            debut_year integer,
            final_year integer,
            primary_external_id text,
            source text not null,
            primary key (sport, player_id)
        );

        create table locations (
            location_id text primary key,
            location_kind text not null,
            label text not null,
            city text,
            state text,
            country text,
            latitude real,
            longitude real,
            geocode_status text not null default 'unresolved',
            geocode_source text,
            source text not null,
            source_key text
        );

        create table player_location_events (
            event_id text primary key,
            sport text not null,
            player_id text not null,
            event_type text not null,
            location_id text not null,
            start_year integer,
            end_year integer,
            duration_years integer,
            source text not null,
            source_key text,
            confidence text not null default 'source_reported',
            notes text,
            foreign key (sport, player_id) references players(sport, player_id),
            foreign key (location_id) references locations(location_id)
        );

        create table source_snapshots (
            source_name text primary key,
            source_path text,
            notes text
        );
        """
    )


def register_functions(con: sqlite3.Connection) -> None:
    con.create_function("stable_id", -1, stable_id)


def attach_sources(con: sqlite3.Connection) -> None:
    con.execute(f"attach database {sql_quote(str(MLB_DB))} as mlb")
    con.execute(f"attach database {sql_quote(str(NFL_DB))} as nfl")
    con.execute(f"attach database {sql_quote(str(WIKIDATA_DB))} as wd")
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.execute(f"attach database {sql_quote(str(WIKIDATA_BIRTHPLACE_DB))} as wb")


def load_players(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        insert or ignore into players
        (sport, player_id, display_name, birth_date, birth_year, debut_year, final_year, primary_external_id, source)
        select
            'MLB',
            playerID,
            name,
            null,
            cast(birthYear as integer),
            cast(substr(debut, 1, 4) as integer),
            cast(substr(finalGame, 1, 4) as integer),
            bbrefID,
            'Lahman People'
        from mlb.mlb_players
        where playerID is not null;

        insert or ignore into players
        (sport, player_id, display_name, birth_date, birth_year, debut_year, final_year, primary_external_id, source)
        select
            'NFL',
            coalesce(nullif(gsis_id, ''), pfr_id),
            display_name,
            nullif(birth_date, ''),
            cast(substr(nullif(birth_date, ''), 1, 4) as integer),
            cast(nullif(rookie_season, '') as integer),
            cast(nullif(last_season, '') as integer),
            pfr_id,
            'nflverse players'
        from nfl.nfl_players
        where coalesce(nullif(gsis_id, ''), nullif(pfr_id, '')) is not null;
        """
    )


def load_locations(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        insert or replace into locations
        (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
        select
            stable_id('MLB', 'birthplace', birth_city, birth_state, birth_country),
            'birthplace',
            birth_city || ', ' || birth_state,
            birth_city,
            birth_state,
            birth_country,
            latitude,
            longitude,
            geocode_status,
            'Census Gazetteer place centroid',
            'MLB birth geocode cache',
            birth_city || '|' || birth_state || '|' || birth_country
        from mlb.mlb_birthplace_geocode_cache
        where birth_city is not null and birth_state is not null;

        insert or replace into locations
        (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
        select
            stable_id('MLB', 'college', schoolID),
            'college',
            lahman_name,
            lahman_city,
            lahman_state,
            lahman_country,
            cast(resolved_latitude as real),
            cast(resolved_longitude as real),
            case when resolved_latitude is not null and resolved_longitude is not null then 'matched' else 'unresolved' end,
            resolved_source,
            'Lahman Schools + Scorecard/Census geocode',
            schoolID
        from mlb.mlb_school_geocode_cache;

        insert or replace into locations
        (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
        select
            stable_id('MLB', 'pro_park', park_key),
            'pro_park',
            park_name,
            city,
            state,
            country,
            latitude,
            longitude,
            geocode_status,
            'Census Gazetteer city centroid',
            'Lahman Parks + Census geocode',
            park_key
        from mlb.mlb_park_geocode_cache;

        insert or replace into locations
        (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
        select
            stable_id('NFL', 'college', college_name),
            'college',
            coalesce(institution_name, college_name),
            city,
            state,
            'USA',
            latitude,
            longitude,
            match_status,
            'College Scorecard institution',
            'nflverse college_name + Scorecard geocode',
            college_name
        from nfl.nfl_college_geocode_cache;

        insert or replace into locations
        (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
        select
            stable_id('WIKIDATA', school_qid),
            case
                when is_high_school = 1 then 'high_school'
                when is_university = 1 then 'college'
                else 'education'
            end,
            school_label,
            located_in_label,
            null,
            null,
            latitude,
            longitude,
            case when latitude is not null and longitude is not null then 'matched' else 'unresolved' end,
            'Wikidata P625 coordinate',
            'Wikidata P69 educated at',
            school_qid
        from wd.wikidata_education_events
        where school_qid is not null and school_qid != '';
        """
    )
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.executescript(
            """
            insert or replace into locations
            (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
            select
                stable_id('WIKIDATA_BIRTHPLACE', birthplace_qid),
                'birthplace',
                case
                    when located_in_label is not null and located_in_label != ''
                    then birthplace_label || ', ' || located_in_label
                    else birthplace_label
                end,
                birthplace_label,
                nullif(located_in_label, ''),
                nullif(country_label, ''),
                latitude,
                longitude,
                case when latitude is not null and longitude is not null then 'matched' else 'unresolved' end,
                'Wikidata P625 coordinate',
                'Wikidata P19 place of birth',
                birthplace_qid
            from wb.wikidata_birthplace_events
            where birthplace_qid is not null and birthplace_qid != '';
            """
        )


def load_events(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        insert or replace into player_location_events
        (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
        select
            stable_id('MLB', p.playerID, 'born', p.birthCity, p.birthState, p.birthCountry),
            'MLB',
            p.playerID,
            'born',
            stable_id('MLB', 'birthplace', p.birthCity, p.birthState, p.birthCountry),
            cast(p.birthYear as integer),
            cast(p.birthYear as integer),
            null,
            'Lahman People',
            p.playerID,
            'source_reported',
            null
        from mlb.mlb_players p
        where p.birthCity is not null and p.birthState is not null and p.birthCountry is not null;

        insert or replace into player_location_events
        (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
        select
            stable_id('MLB', playerID, 'played_college', schoolID),
            'MLB',
            playerID,
            'played_college',
            stable_id('MLB', 'college', schoolID),
            cast(start_year as integer),
            cast(end_year as integer),
            cast(years as integer),
            'Lahman CollegePlaying',
            schoolID,
            'source_reported',
            'CollegePlaying is sports participation, not merely attendance.'
        from mlb.mlb_college_events;

        insert or replace into player_location_events
        (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
        select
            stable_id('MLB', playerID, 'played_pro', park_key),
            'MLB',
            playerID,
            'played_pro',
            stable_id('MLB', 'pro_park', park_key),
            cast(start_year as integer),
            cast(end_year as integer),
            cast(seasons as integer),
            'Lahman Appearances + HomeGames + Parks',
            park_key,
            'source_reported',
            'Park coordinates currently use city centroids unless exact venue coordinates were present upstream.'
        from mlb.mlb_pro_park_events;

        insert or replace into player_location_events
        (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
        select
            stable_id('NFL', coalesce(nullif(gsis_id, ''), pfr_id), 'attended_college', college_name),
            'NFL',
            coalesce(nullif(gsis_id, ''), pfr_id),
            'attended_college',
            stable_id('NFL', 'college', college_name),
            cast(nullif(rookie_season, '') as integer) - 4,
            cast(nullif(rookie_season, '') as integer) - 1,
            null,
            'nflverse players college_name + College Scorecard',
            college_name,
            'inferred_years',
            'Years inferred from rookie season; college attendance/participation dates are not in nflverse players.'
        from nfl.nfl_players
        where college_name is not null and college_name != ''
          and coalesce(nullif(gsis_id, ''), nullif(pfr_id, '')) is not null;

        insert or replace into player_location_events
        (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
        select
            stable_id(sport, source_player_id,
                case
                    when is_high_school = 1 then 'attended_high_school'
                    when is_university = 1 then 'attended_college'
                    else 'attended_school'
                end,
                school_qid),
            sport,
            source_player_id,
            case
                when is_high_school = 1 then 'attended_high_school'
                when is_university = 1 then 'attended_college'
                else 'attended_school'
            end,
            stable_id('WIKIDATA', school_qid),
            null,
            null,
            null,
            'Wikidata P69 educated at',
            school_qid,
            'source_reported',
            'P69 means educated at / attended; it does not prove sports participation.'
        from wd.wikidata_education_events
        where source_player_id is not null and source_player_id != ''
          and school_qid is not null and school_qid != '';
        """
    )
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.executescript(
            """
            create temp table if not exists existing_born_events as
            select sport, player_id
            from player_location_events
            where event_type = 'born';

            create index if not exists temp.idx_existing_born_events
            on existing_born_events(sport, player_id);

            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id(b.sport, b.source_player_id, 'born', b.birthplace_qid),
                b.sport,
                b.source_player_id,
                'born',
                stable_id('WIKIDATA_BIRTHPLACE', b.birthplace_qid),
                p.birth_year,
                p.birth_year,
                null,
                'Wikidata P19 place of birth',
                b.birthplace_qid,
                'source_reported',
                'P19 means place of birth. Coordinates are for the birthplace entity, usually a city/place centroid.'
            from wb.wikidata_birthplace_events b
            join players p on p.sport = b.sport and p.player_id = b.source_player_id
            where b.source_player_id is not null and b.source_player_id != ''
              and b.birthplace_qid is not null and b.birthplace_qid != ''
              and not exists (
                  select 1
                  from existing_born_events e
                  where e.sport = b.sport
                    and e.player_id = b.source_player_id
              );
            """
        )


def create_indexes_and_views(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create index idx_events_player on player_location_events(sport, player_id);
        create index idx_events_type on player_location_events(event_type);
        create index idx_events_location on player_location_events(location_id);
        create index idx_locations_kind on locations(location_kind);
        create index idx_locations_lat_lon on locations(latitude, longitude);

        create view geocoded_player_location_events as
        select
            e.event_id,
            e.sport,
            e.player_id,
            p.display_name,
            e.event_type,
            e.start_year,
            e.end_year,
            e.duration_years,
            e.source,
            e.confidence,
            e.notes,
            l.location_id,
            l.location_kind,
            l.label as location_label,
            l.city,
            l.state,
            l.country,
            l.latitude,
            l.longitude,
            l.geocode_status,
            l.geocode_source
        from player_location_events e
        join players p on p.sport = e.sport and p.player_id = e.player_id
        join locations l on l.location_id = e.location_id
        where l.latitude is not null and l.longitude is not null;

        create view player_event_summary as
        select
            p.sport,
            p.player_id,
            p.display_name,
            count(e.event_id) as event_count,
            group_concat(distinct e.event_type) as event_types,
            min(e.start_year) as first_event_year,
            max(e.end_year) as last_event_year
        from players p
        left join player_location_events e
          on p.sport = e.sport and p.player_id = e.player_id
        group by p.sport, p.player_id, p.display_name;

        create view sf_50mi_non_pro_player_pool as
        with distances as (
            select
                *,
                3958.7613 * 2 * asin(
                    min(1.0, sqrt(
                        pow(sin(((latitude - 37.7749) * 0.017453292519943295) / 2), 2) +
                        cos(37.7749 * 0.017453292519943295) *
                        cos(latitude * 0.017453292519943295) *
                        pow(sin(((longitude - -122.4194) * 0.017453292519943295) / 2), 2)
                    ))
                ) as distance_mi
            from geocoded_player_location_events
            where event_type != 'played_pro'
        )
        select *
        from distances
        where distance_mi <= 50.0;
        """
    )


def add_sources(con: sqlite3.Connection) -> None:
    rows = [
        ("mlb_enrichment", str(MLB_DB), "MLB Lahman/Chadwick/Scorecard/Census cache"),
        ("nfl_enrichment", str(NFL_DB), "NFL nflverse/Scorecard cache"),
        ("wikidata_education", str(WIKIDATA_DB), "Wikidata P69 education cache"),
    ]
    if WIKIDATA_BIRTHPLACE_DB.exists():
        rows.append(("wikidata_birthplace", str(WIKIDATA_BIRTHPLACE_DB), "Wikidata P19 birthplace cache"))
    con.executemany("insert or replace into source_snapshots values (?, ?, ?)", rows)


def write_summary(con: sqlite3.Connection) -> dict:
    summary = {
        "sqlite_database": str(OUT_DB),
        "players": dict(con.execute("select sport, count(*) from players group by sport").fetchall()),
        "locations": con.execute("select count(*) from locations").fetchone()[0],
        "locations_geocoded": con.execute("select count(*) from locations where latitude is not null and longitude is not null").fetchone()[0],
        "events": con.execute("select count(*) from player_location_events").fetchone()[0],
        "events_by_type": dict(con.execute("select event_type, count(*) from player_location_events group by event_type order by event_type").fetchall()),
        "geocoded_events": con.execute("select count(*) from geocoded_player_location_events").fetchone()[0],
        "sf_50mi_non_pro_events": con.execute("select count(*) from sf_50mi_non_pro_player_pool").fetchone()[0],
        "sf_50mi_non_pro_players": con.execute("select count(distinct sport || ':' || player_id) from sf_50mi_non_pro_player_pool").fetchone()[0],
    }
    SUMMARY.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    require_inputs()
    if OUT_DB.exists():
        OUT_DB.unlink()
    con = sqlite3.connect(OUT_DB)
    register_functions(con)
    attach_sources(con)
    create_schema(con)
    load_players(con)
    load_locations(con)
    load_events(con)
    create_indexes_and_views(con)
    add_sources(con)
    con.commit()
    summary = write_summary(con)
    con.close()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
