#!/usr/bin/env python3
"""
Build an all-time NBA/ABA pro-team membership cache from Wikidata.

This uses Wikidata P54 team-membership statements for players already seeded
by the all-time NBA Wikidata cache. Rows are cached conservatively: all team
membership statements are stored, and a flag marks major North American pro
basketball leagues that should become `played_pro` rows in the app warehouse.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_WIKIDATA = ROOT / "data/raw/wikidata/nba_alltime_pro_teams"
NBA_ALLTIME_DB = SCRATCH / "nba_alltime_wikidata.sqlite"
DB_PATH = SCRATCH / "nba_alltime_pro_teams.sqlite"
SUMMARY_PATH = SCRATCH / "nba_alltime_pro_teams_summary.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "HometownHeroesPipeline/0.1 (Wikidata CC0 NBA all-time pro teams cache)"

MAJOR_PRO_LEAGUE_LABELS = {
    "National Basketball Association",
    "American Basketball Association",
    "Basketball Association of America",
    "National Basketball League",
}


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def qid_from_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("Q"):
        return value
    match = re.search(r"/entity/(Q[0-9]+)$", value)
    return match.group(1) if match else None


def parse_point(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    match = re.match(r"Point\((-?[0-9.]+) (-?[0-9.]+)\)", value)
    if not match:
        return None, None
    lon, lat = match.groups()
    return float(lat), float(lon)


def year_from_date(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"^([0-9]{4})", value)
    return int(match.group(1)) if match else None


def retry_after_seconds(exc: HTTPError, fallback: float) -> float:
    value = exc.headers.get("Retry-After")
    if not value:
        return fallback
    value = value.strip()
    if value.isdigit():
        return max(float(value), fallback)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return fallback
    return max(retry_at.timestamp() - time.time(), fallback)


def request_json(query: str, max_retries: int, rate_limit_sleep: float) -> dict:
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode("utf-8")
    req = urllib.request.Request(
        SPARQL_URL,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_exc = exc
            sleep_for = retry_after_seconds(exc, rate_limit_sleep) if exc.code == 429 else min(120.0, attempt * 5.0)
            log(f"  request retry {attempt}/{max_retries} after HTTP {exc.code}; sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        except (URLError, TimeoutError) as exc:
            last_exc = exc
            sleep_for = min(120.0, attempt * 5.0)
            log(f"  request retry {attempt}/{max_retries} after {type(exc).__name__}; sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"request failed after retries: {last_exc}")


def chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def load_players() -> list[dict[str, str]]:
    if not NBA_ALLTIME_DB.exists():
        raise SystemExit(f"Missing {repo_path(NBA_ALLTIME_DB)}. Run pipelines/run_nba_alltime_wikidata.sh first.")
    con = sqlite3.connect(NBA_ALLTIME_DB)
    rows = [
        {
            "bbr_id": str(row[0]),
            "wikidata_qid": str(row[1]),
        }
        for row in con.execute(
            """
            select bbr_id, wikidata_qid
            from nba_alltime_players
            where bbr_id is not null and bbr_id != ''
              and wikidata_qid is not null and wikidata_qid != ''
            order by wikidata_qid
            """
        ).fetchall()
    ]
    con.close()
    return rows


def pro_team_query(qids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?person ?personLabel ?team ?teamLabel ?start ?end ?league ?leagueLabel
       ?hq ?hqLabel ?hqCoord ?venue ?venueLabel ?venueCoord ?countryLabel WHERE {{
  VALUES ?person {{ {values} }}
  ?person p:P54 ?teamStatement .
  ?teamStatement ps:P54 ?team .
  OPTIONAL {{ ?teamStatement pq:P580 ?start . }}
  OPTIONAL {{ ?teamStatement pq:P582 ?end . }}
  OPTIONAL {{ ?team wdt:P118 ?league . }}
  OPTIONAL {{ ?team wdt:P159 ?hq . OPTIONAL {{ ?hq wdt:P625 ?hqCoord . }} }}
  OPTIONAL {{ ?team wdt:P115 ?venue . OPTIONAL {{ ?venue wdt:P625 ?venueCoord . }} }}
  OPTIONAL {{ ?team wdt:P17 ?country . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?personLabel ?start ?teamLabel
"""


def init_db(reset: bool) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    if reset:
        con.executescript(
            """
            drop table if exists nba_alltime_team_memberships;
            drop table if exists wikidata_fetch_chunks;
            """
        )
    con.executescript(
        """
        create table if not exists nba_alltime_team_memberships (
            bbr_id text not null,
            wikidata_qid text,
            person_label text,
            team_qid text not null,
            team_label text,
            league_qid text,
            league_label text,
            start_date text,
            end_date text,
            start_year integer,
            end_year integer,
            hq_qid text,
            hq_label text,
            hq_latitude real,
            hq_longitude real,
            venue_qid text,
            venue_label text,
            venue_latitude real,
            venue_longitude real,
            country_label text,
            is_major_pro integer not null default 0,
            raw_cache_path text,
            primary key (bbr_id, team_qid, start_date, end_date)
        );

        create table if not exists wikidata_fetch_chunks (
            query_name text not null,
            chunk_index integer not null,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (query_name, chunk_index)
        );
        """
    )
    return con


def fetch_or_load(cache_path: Path, query: str, max_retries: int, rate_limit_sleep: float) -> dict:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = request_json(query, max_retries=max_retries, rate_limit_sleep=rate_limit_sleep)
    cache_path.write_text(json.dumps(data))
    return data


def process_bindings(con: sqlite3.Connection, chunk_rows: list[dict[str, str]], data: dict, cache_path: Path) -> int:
    bbr_by_qid = {row["wikidata_qid"]: row["bbr_id"] for row in chunk_rows}
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        qid = qid_from_url(binding.get("person", {}).get("value"))
        team_qid = qid_from_url(binding.get("team", {}).get("value"))
        if not qid or not team_qid:
            continue
        hq_lat, hq_lon = parse_point(binding.get("hqCoord", {}).get("value"))
        venue_lat, venue_lon = parse_point(binding.get("venueCoord", {}).get("value"))
        start_date = binding.get("start", {}).get("value", "")[:10]
        end_date = binding.get("end", {}).get("value", "")[:10]
        league_label = binding.get("leagueLabel", {}).get("value", "")
        out.append(
            {
                "bbr_id": bbr_by_qid.get(qid),
                "wikidata_qid": qid,
                "person_label": binding.get("personLabel", {}).get("value", ""),
                "team_qid": team_qid,
                "team_label": binding.get("teamLabel", {}).get("value", ""),
                "league_qid": qid_from_url(binding.get("league", {}).get("value")),
                "league_label": league_label,
                "start_date": start_date,
                "end_date": end_date,
                "start_year": year_from_date(start_date),
                "end_year": year_from_date(end_date),
                "hq_qid": qid_from_url(binding.get("hq", {}).get("value")),
                "hq_label": binding.get("hqLabel", {}).get("value", ""),
                "hq_latitude": hq_lat,
                "hq_longitude": hq_lon,
                "venue_qid": qid_from_url(binding.get("venue", {}).get("value")),
                "venue_label": binding.get("venueLabel", {}).get("value", ""),
                "venue_latitude": venue_lat,
                "venue_longitude": venue_lon,
                "country_label": binding.get("countryLabel", {}).get("value", ""),
                "is_major_pro": 1 if league_label in MAJOR_PRO_LEAGUE_LABELS else 0,
                "raw_cache_path": repo_path(cache_path),
            }
        )
    con.executemany(
        """
        insert or replace into nba_alltime_team_memberships
        (bbr_id, wikidata_qid, person_label, team_qid, team_label, league_qid, league_label,
         start_date, end_date, start_year, end_year, hq_qid, hq_label, hq_latitude, hq_longitude,
         venue_qid, venue_label, venue_latitude, venue_longitude, country_label, is_major_pro, raw_cache_path)
        values
        (:bbr_id, :wikidata_qid, :person_label, :team_qid, :team_label, :league_qid, :league_label,
         :start_date, :end_date, :start_year, :end_year, :hq_qid, :hq_label, :hq_latitude, :hq_longitude,
         :venue_qid, :venue_label, :venue_latitude, :venue_longitude, :country_label, :is_major_pro, :raw_cache_path)
        """,
        [row for row in out if row["bbr_id"]],
    )
    con.commit()
    return len(out)


def fetch_memberships(
    con: sqlite3.Connection,
    rows: list[dict[str, str]],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    grouped = chunks(rows, chunk_size)
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"nba_alltime_pro_teams_chunk_{chunk_index:05d}.json"
        if con.execute(
            "select 1 from wikidata_fetch_chunks where query_name='team_memberships' and chunk_index=? and status='ok'",
            (chunk_index,),
        ).fetchone() and cache_path.exists():
            continue
        log(f"fetching NBA all-time team membership chunk {chunk_index + 1}/{len(grouped)} ({len(chunk_rows)} players)")
        try:
            data = fetch_or_load(cache_path, pro_team_query([row["wikidata_qid"] for row in chunk_rows]), max_retries, rate_limit_sleep)
            row_count = process_bindings(con, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (query_name, chunk_index, cache_path, row_count, status)
                values ('team_memberships', ?, ?, ?, 'ok')
                """,
                (chunk_index, repo_path(cache_path), row_count),
            )
            con.commit()
            log(f"  cached {row_count} team membership rows")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (query_name, chunk_index, cache_path, row_count, status)
                values ('team_memberships', ?, ?, 0, ?)
                """,
                (chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        time.sleep(sleep_seconds)


def write_summary(con: sqlite3.Connection) -> None:
    summary = {
        "sqlite_cache": repo_path(DB_PATH),
        "team_membership_rows": con.execute("select count(*) from nba_alltime_team_memberships").fetchone()[0],
        "major_pro_rows": con.execute("select count(*) from nba_alltime_team_memberships where is_major_pro = 1").fetchone()[0],
        "players_with_major_pro_rows": con.execute(
            "select count(distinct bbr_id) from nba_alltime_team_memberships where is_major_pro = 1"
        ).fetchone()[0],
        "major_pro_rows_with_start_year": con.execute(
            "select count(*) from nba_alltime_team_memberships where is_major_pro = 1 and start_year is not null"
        ).fetchone()[0],
        "major_pro_rows_with_location": con.execute(
            """
            select count(*)
            from nba_alltime_team_memberships
            where is_major_pro = 1
              and ((hq_latitude is not null and hq_longitude is not null)
                   or (venue_latitude is not null and venue_longitude is not null))
            """
        ).fetchone()[0],
        "major_pro_leagues": [
            dict(row)
            for row in con.execute(
                """
                select league_label, count(*) as rows
                from nba_alltime_team_memberships
                where is_major_pro = 1
                group by league_label
                order by rows desc, league_label
                """
            )
        ],
        "notes": [
            "Rows come from Wikidata P54 team membership statements.",
            "Warehouse integration should prefer team headquarters/city coordinates over current arena coordinates for historical teams.",
            "Start/end years are only as complete as Wikidata qualifiers.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    log(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Wikidata all-time NBA/ABA pro-team membership cache.")
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--max-chunks", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-retries", type=int, default=20)
    parser.add_argument("--rate-limit-sleep", type=float, default=120.0)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    max_chunks = None if args.max_chunks == 0 else args.max_chunks
    con = init_db(args.reset)
    con.row_factory = sqlite3.Row
    rows = load_players()
    fetch_memberships(con, rows, args.chunk_size, max_chunks, args.sleep_seconds, args.max_retries, args.rate_limit_sleep)
    write_summary(con)
    con.close()


if __name__ == "__main__":
    main()
