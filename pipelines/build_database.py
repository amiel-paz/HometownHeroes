#!/usr/bin/env python3
"""
Build the unified SQLite warehouse from enrichment caches.

This pipeline reads derived MLB, NFL, and Wikidata cache databases from
scratch/ and writes the query-ready application database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
OUT_DB = SCRATCH / "HometownHeroes.sqlite"
SUMMARY = SCRATCH / "HometownHeroesSqlSummary.json"

MLB_DB = SCRATCH / "mlb_enrichment.sqlite"
NFL_DB = SCRATCH / "nfl_enrichment.sqlite"
NBA_DB = SCRATCH / "nba_enrichment.sqlite"
NBA_ALLTIME_DB = SCRATCH / "nba_alltime_wikidata.sqlite"
NBA_ALLTIME_PRO_TEAMS_DB = SCRATCH / "nba_alltime_pro_teams.sqlite"
WIKIDATA_DB = SCRATCH / "wikidata_education_enrichment.sqlite"
WIKIDATA_BIRTHPLACE_DB = SCRATCH / "wikidata_birthplace_enrichment.sqlite"
NFL_STADIUM_DB = SCRATCH / "nfl_stadium_enrichment.sqlite"
HONORS_DB = SCRATCH / "player_honors.sqlite"
PLAYER_MEDIA_DB = SCRATCH / "player_media.sqlite"

NFL_CAREER_YEAR_OVERRIDES = [
    {
        "player_id": "00-0003942",
        "debut_year": 1992,
        "final_year": None,
        "source": "Pro Football Reference identifier DaviAn23; 1992 Houston Oilers roster/draft association",
        "notes": "Use first sourced pro team association for profile debut display; nflverse rookie_season remains the conservative first stat season.",
    }
]


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def stable_id(*parts: object) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def person_match_key(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def require_inputs() -> None:
    missing = [repo_path(path) for path in (MLB_DB, NFL_DB, WIKIDATA_DB) if not path.exists()]
    if missing:
        raise SystemExit("Missing required cache DBs: " + ", ".join(missing))


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        pragma foreign_keys = off;

        drop table if exists players;
        drop table if exists locations;
        drop table if exists player_location_events;
        drop table if exists player_honor_summary;
        drop table if exists player_media;
        drop table if exists source_snapshots;
        drop view if exists geocoded_player_location_events;
        drop view if exists player_event_summary;
        drop view if exists pro_career_summary;
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

        create table player_honor_summary (
            sport text not null,
            player_id text not null,
            all_star_count integer not null default 0,
            all_pro_count integer not null default 0,
            hof_inducted integer not null default 0,
            hof_year integer,
            honor_sources text,
            primary key (sport, player_id),
            foreign key (sport, player_id) references players(sport, player_id)
        );

        create table player_media (
            sport text not null,
            player_id text not null,
            wikidata_qid text,
            person_label text,
            article_title text,
            media_source text,
            file_title text,
            thumbnail_url text,
            original_url text,
            source_page_url text,
            author text,
            credit text,
            license text,
            license_url text,
            attribution_text text,
            attribution_required integer not null default 1,
            usable integer not null default 0,
            rejection_reason text,
            raw_cache_path text,
            fetched_at text,
            primary key (sport, player_id),
            foreign key (sport, player_id) references players(sport, player_id)
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
    con.create_function("person_match_key", 1, person_match_key)


def attach_sources(con: sqlite3.Connection) -> None:
    con.execute(f"attach database {sql_quote(str(MLB_DB))} as mlb")
    con.execute(f"attach database {sql_quote(str(NFL_DB))} as nfl")
    if NBA_DB.exists():
        con.execute(f"attach database {sql_quote(str(NBA_DB))} as nba")
    if NBA_ALLTIME_DB.exists():
        con.execute(f"attach database {sql_quote(str(NBA_ALLTIME_DB))} as nba_alltime")
    if NBA_ALLTIME_PRO_TEAMS_DB.exists():
        con.execute(f"attach database {sql_quote(str(NBA_ALLTIME_PRO_TEAMS_DB))} as nba_pro_alltime")
    con.execute(f"attach database {sql_quote(str(WIKIDATA_DB))} as wd")
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.execute(f"attach database {sql_quote(str(WIKIDATA_BIRTHPLACE_DB))} as wb")
    if NFL_STADIUM_DB.exists():
        con.execute(f"attach database {sql_quote(str(NFL_STADIUM_DB))} as ns")
    if HONORS_DB.exists():
        con.execute(f"attach database {sql_quote(str(HONORS_DB))} as honors")
    if PLAYER_MEDIA_DB.exists():
        con.execute(f"attach database {sql_quote(str(PLAYER_MEDIA_DB))} as media")


def prepare_nba_alltime_mapping(con: sqlite3.Connection) -> None:
    if not NBA_ALLTIME_DB.exists():
        return
    con.executescript(
        """
        drop table if exists temp.nba_qid_current_ids;
        drop table if exists temp.nba_current_name_year_ids;
        drop table if exists temp.nba_alltime_player_map;

        create temp table nba_qid_current_ids (
            wikidata_qid text primary key,
            current_player_id text
        );

        create temp table nba_current_name_year_ids as
        select
            person_match_key(display_name) as match_name,
            cast(birth_year as integer) as birth_year,
            min(athlete_id) as current_player_id,
            count(distinct athlete_id) as player_count
        from nba.nba_players
        where athlete_id is not null and athlete_id != ''
          and display_name is not null and display_name != ''
          and birth_year is not null and birth_year != ''
        group by person_match_key(display_name), cast(birth_year as integer);

        create index temp.idx_nba_current_name_year
        on nba_current_name_year_ids(match_name, birth_year);

        insert or ignore into nba_qid_current_ids
        select wikidata_qid, source_player_id
        from wd.wikidata_education_events
        where sport = 'NBA'
          and wikidata_qid is not null and wikidata_qid != ''
          and source_player_id is not null and source_player_id != '';
        """
    )
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.executescript(
            """
            insert or ignore into nba_qid_current_ids
            select wikidata_qid, source_player_id
            from wb.wikidata_birthplace_events
            where sport = 'NBA'
              and wikidata_qid is not null and wikidata_qid != ''
              and source_player_id is not null and source_player_id != '';
            """
        )
    if NBA_ALLTIME_PRO_TEAMS_DB.exists():
        con.executescript(
            """
            insert or replace into locations
            (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
            select
                stable_id('NBA_ALLTIME_PRO_TEAM', team_qid),
                'pro_team',
                team_label,
                nullif(hq_label, ''),
                null,
                nullif(country_label, ''),
                coalesce(hq_latitude, venue_latitude),
                coalesce(hq_longitude, venue_longitude),
                case
                    when coalesce(hq_latitude, venue_latitude) is not null
                     and coalesce(hq_longitude, venue_longitude) is not null
                    then 'matched'
                    else 'unresolved'
                end,
                case
                    when hq_latitude is not null and hq_longitude is not null then 'Wikidata P159 team headquarters/city coordinate'
                    when venue_latitude is not null and venue_longitude is not null then 'Wikidata P115 home venue coordinate'
                    else 'unresolved'
                end,
                'Wikidata P54 NBA/ABA all-time team membership',
                team_qid
            from nba_pro_alltime.nba_alltime_team_memberships
            where is_major_pro = 1
              and team_qid is not null and team_qid != '';
            """
        )
    if PLAYER_MEDIA_DB.exists():
        con.executescript(
            """
            insert or ignore into nba_qid_current_ids
            select wikidata_qid, source_player_id
            from media.media_candidates
            where sport = 'NBA'
              and wikidata_qid is not null and wikidata_qid != ''
              and source_player_id is not null and source_player_id != ''
              and source_player_id not like 'bbr:%'
              and source_player_id glob '[0-9]*';
            """
        )
    con.executescript(
        """
        create temp table nba_alltime_player_map as
        select
            p.bbr_id,
            p.wikidata_qid,
            coalesce(m.current_player_id, n.current_player_id, 'bbr:' || p.bbr_id) as player_id
        from nba_alltime.nba_alltime_players p
        left join nba_qid_current_ids m
          on m.wikidata_qid = p.wikidata_qid
        left join nba_current_name_year_ids n
          on n.match_name = person_match_key(p.display_name)
         and n.birth_year = cast(p.birth_year as integer)
         and n.player_count = 1
        where p.bbr_id is not null and p.bbr_id != '';

        create index temp.idx_nba_alltime_map_bbr on nba_alltime_player_map(bbr_id);
        create index temp.idx_nba_alltime_map_player on nba_alltime_player_map(player_id);
        """
    )


def load_players(con: sqlite3.Connection) -> None:
    con.execute(
        """
        create temp table nfl_career_year_overrides (
            player_id text primary key,
            debut_year integer,
            final_year integer,
            source text,
            notes text
        )
        """
    )
    con.executemany(
        """
        insert into nfl_career_year_overrides
        (player_id, debut_year, final_year, source, notes)
        values (:player_id, :debut_year, :final_year, :source, :notes)
        """,
        NFL_CAREER_YEAR_OVERRIDES,
    )
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
            coalesce(o.debut_year, cast(nullif(rookie_season, '') as integer)),
            coalesce(o.final_year, cast(nullif(last_season, '') as integer)),
            pfr_id,
            case
                when o.player_id is not null
                    then 'nflverse players profile + curated career-year override'
                else 'nflverse players profile'
            end
        from nfl.nfl_players
        left join nfl_career_year_overrides o
          on o.player_id = coalesce(nullif(gsis_id, ''), pfr_id)
        where coalesce(nullif(gsis_id, ''), nullif(pfr_id, '')) is not null;
        """
    )
    if NBA_DB.exists():
        con.executescript(
            """
            insert or ignore into players
            (sport, player_id, display_name, birth_date, birth_year, debut_year, final_year, primary_external_id, source)
            select
                'NBA',
                athlete_id,
                display_name,
                nullif(date_of_birth, ''),
                cast(nullif(birth_year, '') as integer),
                cast(first_season as integer),
                cast(last_season as integer),
                athlete_id,
                source
            from nba.nba_players
            where athlete_id is not null and athlete_id != '';
            """
        )
    if NBA_ALLTIME_DB.exists():
        con.executescript(
            """
            insert or ignore into players
            (sport, player_id, display_name, birth_date, birth_year, debut_year, final_year, primary_external_id, source)
            select
                'NBA',
                m.player_id,
                p.display_name,
                nullif(p.birth_date, ''),
                p.birth_year,
                null,
                null,
                p.bbr_id,
                'Wikidata P2685 Basketball Reference NBA player ID'
            from nba_alltime.nba_alltime_players p
            join nba_alltime_player_map m on m.bbr_id = p.bbr_id
            where p.bbr_id is not null and p.bbr_id != '';
            """
        )
    if NBA_ALLTIME_PRO_TEAMS_DB.exists():
        con.executescript(
            """
            create temp table if not exists existing_nba_pro_events as
            select sport, player_id, location_id
            from player_location_events
            where sport = 'NBA'
              and event_type = 'played_pro';

            create index if not exists temp.idx_existing_nba_pro_events
            on existing_nba_pro_events(player_id, location_id);

            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NBA_ALLTIME', map.player_id, 'played_pro', t.team_qid, t.start_year, t.end_year),
                'NBA',
                map.player_id,
                'played_pro',
                stable_id('NBA_ALLTIME_PRO_TEAM', t.team_qid),
                t.start_year,
                t.end_year,
                case
                    when t.start_year is not null and t.end_year is not null
                    then max(1, t.end_year - t.start_year)
                    else null
                end,
                'Wikidata P54 NBA/ABA team membership',
                t.team_qid,
                'source_reported',
                'Team membership dates come from Wikidata P54 qualifiers. Duration is an approximate season count from year span. Coordinates prefer team headquarters/city to avoid current-arena anachronism.'
            from nba_pro_alltime.nba_alltime_team_memberships t
            join nba_alltime_player_map map on map.bbr_id = t.bbr_id
            left join existing_nba_pro_events existing
              on existing.player_id = map.player_id
             and existing.location_id = stable_id('NBA_ALLTIME_PRO_TEAM', t.team_qid)
            where t.is_major_pro = 1
              and t.team_qid is not null and t.team_qid != ''
              and coalesce(t.hq_latitude, t.venue_latitude) is not null
              and coalesce(t.hq_longitude, t.venue_longitude) is not null
              and existing.player_id is null;
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
            case
                when (birth_city is null or birth_city = '') and upper(coalesce(birth_country, '')) in ('P.R.', 'PR', 'PRI') then 'Puerto Rico'
                when birth_state is not null and birth_state != '' then birth_city || ', ' || birth_state
                when upper(coalesce(birth_country, '')) in ('P.R.', 'PR', 'PRI') then birth_city || ', Puerto Rico'
                when birth_country is not null and birth_country != '' then birth_city || ', ' || birth_country
                else birth_city
            end,
            birth_city,
            birth_state,
            birth_country,
            latitude,
            longitude,
            geocode_status,
            coalesce(geocode_source, 'Census Gazetteer place centroid'),
            'MLB birth geocode cache',
            birth_city || '|' || birth_state || '|' || birth_country
        from mlb.mlb_birthplace_geocode_cache
        where birth_country is not null
          and (birth_city is not null or upper(coalesce(birth_country, '')) in ('P.R.', 'PR', 'PRI'));

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
    if NBA_DB.exists():
        con.executescript(
            """
            insert or replace into locations
            (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
            select
                stable_id('NBA', 'birthplace', birth_city, birth_state, birth_country),
                'birthplace',
                case
                    when birth_state is not null and birth_state != '' then birth_city || ', ' || birth_state
                    else birth_city || ', ' || birth_country
                end,
                birth_city,
                birth_state,
                birth_country,
                latitude,
                longitude,
                geocode_status,
                'Census Gazetteer place centroid',
                'hoopR NBA roster birthplace cache',
                birth_city || '|' || birth_state || '|' || birth_country
            from nba.nba_birthplace_geocode_cache
            where birth_city is not null and birth_city != '';

            insert or replace into locations
            (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
            select
                stable_id('NBA', 'pro_venue', venue_id, venue_full_name),
                'pro_venue',
                venue_full_name,
                city,
                state,
                country,
                latitude,
                longitude,
                geocode_status,
                'Census Gazetteer venue city centroid',
                'hoopR NBA schedules venue city cache',
                venue_id || '|' || venue_full_name
            from nba.nba_venue_geocode_cache
            where venue_id is not null and venue_id != '';
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
    if NBA_ALLTIME_DB.exists():
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
                'Wikidata P19 place of birth; NBA all-time P2685 cache',
                birthplace_qid
            from nba_alltime.nba_alltime_birthplaces
            where birthplace_qid is not null and birthplace_qid != '';

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
                'Wikidata P69 educated at; NBA all-time P2685 cache',
                school_qid
            from nba_alltime.nba_alltime_education
            where school_qid is not null and school_qid != '';
            """
        )
    if NFL_STADIUM_DB.exists():
        con.executescript(
            """
            insert or replace into locations
            (location_id, location_kind, label, city, state, country, latitude, longitude, geocode_status, geocode_source, source, source_key)
            select
                stable_id('NFL', 'pro_stadium', stadium_id, stadium_name),
                'pro_stadium',
                stadium_name,
                nullif(city, ''),
                null,
                nullif(country, ''),
                latitude,
                longitude,
                match_status,
                geocode_source,
                'nflverse schedules + Wikidata stadium geocode',
                stadium_id || '|' || stadium_name
            from ns.nfl_stadium_geocode_cache
            where latitude is not null and longitude is not null;
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
        where p.birthCountry is not null
          and (p.birthCity is not null or upper(coalesce(p.birthCountry, '')) in ('P.R.', 'PR', 'PRI'));

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
            null,
            null,
            null,
            'nflverse players college_name + College Scorecard',
            college_name,
            'source_reported',
            'College association only; attendance/participation dates are not in nflverse players.'
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
    if NBA_DB.exists():
        con.executescript(
            """
            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NBA', athlete_id, 'born', birth_place_city, birth_place_state, birth_place_country),
                'NBA',
                athlete_id,
                'born',
                stable_id('NBA', 'birthplace', birth_place_city, birth_place_state, birth_place_country),
                cast(nullif(birth_year, '') as integer),
                cast(nullif(birth_year, '') as integer),
                null,
                'hoopR NBA roster snapshots',
                athlete_id,
                'source_reported',
                'Birthplace fields currently come from current/recent hoopR roster snapshots only.'
            from nba.nba_players
            where birth_place_city is not null and birth_place_city != ''
              and birth_place_country is not null and birth_place_country != '';

            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NBA', athlete_id, 'played_pro', team_id, venue_id),
                'NBA',
                athlete_id,
                'played_pro',
                stable_id('NBA', 'pro_venue', venue_id, venue_full_name),
                cast(start_year as integer),
                cast(end_year as integer),
                cast(seasons as integer),
                source,
                team_display_name || '|' || team_id || '|' || venue_id || '|' || season_list,
                'roster_home_venue_city_inferred',
                'Player/team season association joined to the team season home venue; coordinates are venue city centroids, not exact arena coordinates.'
            from nba.nba_pro_venue_events
            where athlete_id is not null and athlete_id != ''
              and venue_id is not null and venue_id != '';
            """
        )
    if WIKIDATA_BIRTHPLACE_DB.exists():
        con.executescript(
            """
            drop table if exists temp.existing_geocoded_born_events;
            create temp table existing_geocoded_born_events as
            select e.sport, e.player_id
            from player_location_events e
            join locations l on l.location_id = e.location_id
            where e.event_type = 'born'
              and l.latitude is not null
              and l.longitude is not null;

            create index if not exists temp.idx_existing_geocoded_born_events
            on existing_geocoded_born_events(sport, player_id);

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
                  from existing_geocoded_born_events e
                  where e.sport = b.sport
                    and e.player_id = b.source_player_id
              );
            """
        )
    if NBA_ALLTIME_DB.exists():
        con.executescript(
            """
            drop table if exists temp.existing_geocoded_born_events;
            create temp table existing_geocoded_born_events as
            select e.sport, e.player_id
            from player_location_events e
            join locations l on l.location_id = e.location_id
            where e.event_type = 'born'
              and l.latitude is not null
              and l.longitude is not null;

            create index if not exists temp.idx_existing_geocoded_born_events
            on existing_geocoded_born_events(sport, player_id);

            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NBA_ALLTIME', m.player_id, 'born', b.birthplace_qid),
                'NBA',
                m.player_id,
                'born',
                stable_id('WIKIDATA_BIRTHPLACE', b.birthplace_qid),
                p.birth_year,
                p.birth_year,
                null,
                'Wikidata P19 place of birth; NBA all-time P2685 cache',
                b.birthplace_qid,
                'source_reported',
                'P19 means place of birth. Coordinates are for the birthplace entity, usually a city/place centroid.'
            from nba_alltime.nba_alltime_birthplaces b
            join nba_alltime.nba_alltime_players p on p.bbr_id = b.bbr_id
            join nba_alltime_player_map m on m.bbr_id = b.bbr_id
            where b.birthplace_qid is not null and b.birthplace_qid != ''
              and not exists (
                  select 1
                  from existing_geocoded_born_events e
                  where e.sport = 'NBA'
                    and e.player_id = m.player_id
              );

            drop table if exists temp.existing_nba_event_keys;
            create temp table existing_nba_event_keys as
            select sport, player_id, event_type, location_id
            from player_location_events
            where sport = 'NBA';

            create index temp.idx_existing_nba_event_keys
            on existing_nba_event_keys(player_id, event_type, location_id);

            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NBA_ALLTIME', m.player_id,
                    case
                        when e.is_high_school = 1 then 'attended_high_school'
                        when e.is_university = 1 then 'attended_college'
                        else 'attended_school'
                    end,
                    e.school_qid),
                'NBA',
                m.player_id,
                case
                    when e.is_high_school = 1 then 'attended_high_school'
                    when e.is_university = 1 then 'attended_college'
                    else 'attended_school'
                end,
                stable_id('WIKIDATA', e.school_qid),
                null,
                null,
                null,
                'Wikidata P69 educated at; NBA all-time P2685 cache',
                e.school_qid,
                'source_reported',
                'P69 means educated at / attended; it does not prove sports participation.'
            from nba_alltime.nba_alltime_education e
            join nba_alltime_player_map m on m.bbr_id = e.bbr_id
            left join existing_nba_event_keys existing
              on existing.player_id = m.player_id
             and existing.location_id = stable_id('WIKIDATA', e.school_qid)
             and existing.event_type = case
                 when e.is_high_school = 1 then 'attended_high_school'
                 when e.is_university = 1 then 'attended_college'
                 else 'attended_school'
             end
            where e.school_qid is not null and e.school_qid != ''
              and existing.player_id is null;
            """
        )
    if NFL_STADIUM_DB.exists():
        con.executescript(
            """
            insert or replace into player_location_events
            (event_id, sport, player_id, event_type, location_id, start_year, end_year, duration_years, source, source_key, confidence, notes)
            select
                stable_id('NFL', e.player_id, 'played_pro', e.team, e.stadium_id, e.stadium_name),
                'NFL',
                e.player_id,
                'played_pro',
                stable_id('NFL', 'pro_stadium', e.stadium_id, e.stadium_name),
                e.start_year,
                e.end_year,
                e.seasons,
                e.source,
                e.team || '|' || e.stadium_id || '|' || e.season_list,
                'roster_home_stadium_inferred',
                'Roster-season association joined to the team season home stadium inferred from nflverse schedules; this is not a game appearance log.'
            from ns.nfl_pro_stadium_events e
            join players p on p.sport = 'NFL' and p.player_id = e.player_id;
            """
        )


def load_honors(con: sqlite3.Connection) -> None:
    if not HONORS_DB.exists():
        return
    con.executescript(
        """
        insert or replace into player_honor_summary
        (sport, player_id, all_star_count, all_pro_count, hof_inducted, hof_year, honor_sources)
        select
            h.sport,
            h.source_player_id,
            h.all_star_count,
            h.all_pro_count,
            h.hof_inducted,
            h.hof_year,
            h.honor_sources
        from honors.player_honor_summary h
        join players p on p.sport = h.sport and p.player_id = h.source_player_id;
        """
    )


def load_media(con: sqlite3.Connection) -> None:
    if not PLAYER_MEDIA_DB.exists():
        return
    con.executescript(
        """
        insert or replace into player_media
        (sport, player_id, wikidata_qid, person_label, article_title, media_source, file_title,
         thumbnail_url, original_url, source_page_url, author, credit, license, license_url,
         attribution_text, attribution_required, usable, rejection_reason, raw_cache_path, fetched_at)
        select
            m.sport,
            m.source_player_id,
            m.wikidata_qid,
            m.person_label,
            m.article_title,
            m.media_source,
            m.file_title,
            m.thumbnail_url,
            m.original_url,
            m.source_page_url,
            m.author,
            m.credit,
            m.license,
            m.license_url,
            m.attribution_text,
            m.attribution_required,
            m.usable,
            m.rejection_reason,
            m.raw_cache_path,
            m.fetched_at
        from media.player_media m
        join players p on p.sport = m.sport and p.player_id = m.source_player_id;
        """
    )
    if NBA_ALLTIME_DB.exists():
        con.executescript(
            """
            insert or replace into player_media
            (sport, player_id, wikidata_qid, person_label, article_title, media_source, file_title,
             thumbnail_url, original_url, source_page_url, author, credit, license, license_url,
             attribution_text, attribution_required, usable, rejection_reason, raw_cache_path, fetched_at)
            select
                m.sport,
                map.player_id,
                m.wikidata_qid,
                m.person_label,
                m.article_title,
                m.media_source,
                m.file_title,
                m.thumbnail_url,
                m.original_url,
                m.source_page_url,
                m.author,
                m.credit,
                m.license,
                m.license_url,
                m.attribution_text,
                m.attribution_required,
                m.usable,
                m.rejection_reason,
                m.raw_cache_path,
                m.fetched_at
            from media.player_media m
            join nba_alltime_player_map map
              on m.sport = 'NBA'
             and m.source_player_id = 'bbr:' || map.bbr_id
            join players p on p.sport = 'NBA' and p.player_id = map.player_id;
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
        create index idx_player_media_usable on player_media(sport, player_id, usable);

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

        create view pro_career_summary as
        select
            p.sport,
            p.player_id,
            p.display_name,
            min(e.start_year) as pro_start_year,
            max(e.end_year) as pro_end_year,
            sum(e.duration_years) as pro_location_seasons,
            group_concat(distinct e.source) as sources
        from players p
        join player_location_events e
          on p.sport = e.sport and p.player_id = e.player_id
        where e.event_type = 'played_pro'
          and e.start_year is not null
          and e.end_year is not null
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
        ("mlb_enrichment", repo_path(MLB_DB), "MLB Lahman/Chadwick/Scorecard/Census cache"),
        ("geonames", "data/raw/geography/geonames", "GeoNames cities500/admin1/countryInfo cache for non-US MLB birthplace centroids; CC BY 4.0"),
        ("nfl_enrichment", repo_path(NFL_DB), "NFL nflverse/Scorecard cache"),
        ("wikidata_education", repo_path(WIKIDATA_DB), "Wikidata P69 education cache"),
    ]
    if NBA_DB.exists():
        rows.append(("nba_enrichment", repo_path(NBA_DB), "NBA hoopR CC BY 4.0 player/team/venue cache"))
    if WIKIDATA_BIRTHPLACE_DB.exists():
        rows.append(("wikidata_birthplace", repo_path(WIKIDATA_BIRTHPLACE_DB), "Wikidata P19 birthplace cache"))
    if NFL_STADIUM_DB.exists():
        rows.append(("nfl_stadium_enrichment", repo_path(NFL_STADIUM_DB), "NFL schedule-derived home stadium geocode cache"))
    if HONORS_DB.exists():
        rows.append(("player_honors", repo_path(HONORS_DB), "MLB Lahman honors plus NFL Wikidata/Wikipedia honors cache"))
    if PLAYER_MEDIA_DB.exists():
        rows.append(("player_media", repo_path(PLAYER_MEDIA_DB), "Wikidata/Wikimedia player profile media metadata cache"))
    if NBA_ALLTIME_DB.exists():
        rows.append(("nba_alltime_wikidata", repo_path(NBA_ALLTIME_DB), "Wikidata P2685 all-time NBA/ABA identity, birthplace, and education cache"))
    if NBA_ALLTIME_PRO_TEAMS_DB.exists():
        rows.append(("nba_alltime_pro_teams", repo_path(NBA_ALLTIME_PRO_TEAMS_DB), "Wikidata P54 all-time NBA/ABA pro-team membership cache"))
    con.executemany("insert or replace into source_snapshots values (?, ?, ?)", rows)


def write_summary(con: sqlite3.Connection) -> dict:
    summary = {
        "sqlite_database": repo_path(OUT_DB),
        "players": dict(con.execute("select sport, count(*) from players group by sport").fetchall()),
        "locations": con.execute("select count(*) from locations").fetchone()[0],
        "locations_geocoded": con.execute("select count(*) from locations where latitude is not null and longitude is not null").fetchone()[0],
        "events": con.execute("select count(*) from player_location_events").fetchone()[0],
        "events_by_type": dict(con.execute("select event_type, count(*) from player_location_events group by event_type order by event_type").fetchall()),
        "pro_career_summary_players": dict(con.execute("select sport, count(*) from pro_career_summary group by sport order by sport").fetchall()),
        "honor_summary_players": dict(con.execute("select sport, count(*) from player_honor_summary group by sport order by sport").fetchall()),
        "media_players": dict(con.execute("select sport, count(*) from player_media where usable = 1 group by sport order by sport").fetchall()),
        "hof_players": dict(
            con.execute("select sport, count(*) from player_honor_summary where hof_inducted = 1 group by sport order by sport").fetchall()
        ),
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
    create_schema(con)
    attach_sources(con)
    prepare_nba_alltime_mapping(con)
    load_players(con)
    load_locations(con)
    load_events(con)
    load_honors(con)
    load_media(con)
    create_indexes_and_views(con)
    add_sources(con)
    con.commit()
    summary = write_summary(con)
    con.close()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
