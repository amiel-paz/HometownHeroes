#!/usr/bin/env python3
"""
Build an all-time NBA/ABA player identity cache from Wikidata.

This uses Wikidata's Basketball Reference NBA player ID property (P2685) to
seed a broader historical NBA/ABA player universe than the current hoopR
2002-current cache. Raw SPARQL responses are cached under data/raw/wikidata
so interrupted runs can resume.
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
RAW_WIKIDATA = ROOT / "data/raw/wikidata/nba_alltime"
DB_PATH = SCRATCH / "nba_alltime_wikidata.sqlite"
SUMMARY_PATH = SCRATCH / "nba_alltime_wikidata_summary.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "HometownHeroesPipeline/0.1 (Wikidata CC0 NBA all-time identity cache)"


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


def article_title(article_url: str | None) -> str:
    if not article_url:
        return ""
    return urllib.parse.unquote(article_url.rsplit("/", 1)[-1]).replace("_", " ")


def file_title(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = urllib.parse.unquote(value.rsplit("/", 1)[-1]).replace("_", " ")
    if value.casefold().startswith("file:"):
        return value
    return f"File:{value}"


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


def chunks(rows: list[str], size: int) -> list[list[str]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def alltime_players_query() -> str:
    return """
SELECT ?person ?personLabel ?bbr ?birthDate ?deathDate ?image ?article WHERE {
  ?person wdt:P2685 ?bbr .
  OPTIONAL { ?person wdt:P569 ?birthDate . }
  OPTIONAL { ?person wdt:P570 ?deathDate . }
  OPTIONAL { ?person wdt:P18 ?image . }
  OPTIONAL {
    ?article schema:about ?person ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY ?bbr
"""


def birthplace_query(qids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?person ?birthplace ?birthplaceLabel ?coord ?locatedInLabel ?countryLabel WHERE {{
  VALUES ?person {{ {values} }}
  ?person wdt:P19 ?birthplace .
  OPTIONAL {{ ?birthplace wdt:P625 ?coord . }}
  OPTIONAL {{ ?birthplace wdt:P131 ?locatedIn . }}
  OPTIONAL {{ ?birthplace wdt:P17 ?country . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def education_query(qids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?person ?school ?schoolLabel ?coord ?locatedInLabel ?isHighSchool ?isUniversity WHERE {{
  VALUES ?person {{ {values} }}
  ?person wdt:P69 ?school .
  OPTIONAL {{ ?school wdt:P625 ?coord . }}
  OPTIONAL {{ ?school wdt:P131 ?locatedIn . }}
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q9826 }} AS ?isHighSchool)
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q3918 }} AS ?isUniversity)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def init_db(reset: bool) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    if reset:
        con.executescript(
            """
            drop table if exists nba_alltime_players;
            drop table if exists nba_alltime_birthplaces;
            drop table if exists nba_alltime_education;
            drop table if exists wikidata_fetch_chunks;
            """
        )
    con.executescript(
        """
        create table if not exists nba_alltime_players (
            bbr_id text primary key,
            wikidata_qid text,
            display_name text,
            birth_date text,
            birth_year integer,
            death_date text,
            enwiki_title text,
            image_file text,
            raw_cache_path text
        );

        create table if not exists nba_alltime_birthplaces (
            bbr_id text not null,
            wikidata_qid text,
            birthplace_qid text,
            birthplace_label text,
            located_in_label text,
            country_label text,
            latitude real,
            longitude real,
            raw_cache_path text,
            primary key (bbr_id, birthplace_qid)
        );

        create table if not exists nba_alltime_education (
            bbr_id text not null,
            wikidata_qid text,
            school_qid text,
            school_label text,
            located_in_label text,
            is_high_school integer,
            is_university integer,
            latitude real,
            longitude real,
            raw_cache_path text,
            primary key (bbr_id, school_qid)
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


def fetch_players(con: sqlite3.Connection, max_retries: int, rate_limit_sleep: float) -> None:
    cache_path = RAW_WIKIDATA / "nba_alltime_players.json"
    if con.execute("select 1 from wikidata_fetch_chunks where query_name='players' and chunk_index=0 and status='ok'").fetchone():
        if cache_path.exists():
            return
    log("fetching all-time NBA/ABA player identities from Wikidata P2685")
    data = fetch_or_load(cache_path, alltime_players_query(), max_retries, rate_limit_sleep)
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        birth_date = binding.get("birthDate", {}).get("value", "")
        out.append(
            {
                "bbr_id": binding.get("bbr", {}).get("value", ""),
                "wikidata_qid": qid_from_url(binding.get("person", {}).get("value")),
                "display_name": binding.get("personLabel", {}).get("value", ""),
                "birth_date": birth_date[:10] if birth_date else "",
                "birth_year": int(birth_date[:4]) if re.match(r"^[0-9]{4}", birth_date) else None,
                "death_date": binding.get("deathDate", {}).get("value", "")[:10],
                "enwiki_title": article_title(binding.get("article", {}).get("value")),
                "image_file": file_title(binding.get("image", {}).get("value")),
                "raw_cache_path": repo_path(cache_path),
            }
        )
    con.executemany(
        """
        insert or replace into nba_alltime_players
        (bbr_id, wikidata_qid, display_name, birth_date, birth_year, death_date, enwiki_title, image_file, raw_cache_path)
        values
        (:bbr_id, :wikidata_qid, :display_name, :birth_date, :birth_year, :death_date, :enwiki_title, :image_file, :raw_cache_path)
        """,
        [row for row in out if row["bbr_id"]],
    )
    con.execute(
        """
        insert or replace into wikidata_fetch_chunks
        (query_name, chunk_index, cache_path, row_count, status)
        values ('players', 0, ?, ?, 'ok')
        """,
        (repo_path(cache_path), len(out)),
    )
    con.commit()
    log(f"  cached {len(out)} all-time player identities")


def player_qids(con: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            """
            select wikidata_qid
            from nba_alltime_players
            where wikidata_qid is not null and wikidata_qid != ''
            order by wikidata_qid
            """
        ).fetchall()
    ]


def fetch_birthplaces(
    con: sqlite3.Connection,
    qids: list[str],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    bbr_by_qid = dict(con.execute("select wikidata_qid, bbr_id from nba_alltime_players where wikidata_qid is not null"))
    grouped = chunks(qids, chunk_size)
    for chunk_index, chunk_qids in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"nba_alltime_birthplaces_chunk_{chunk_index:05d}.json"
        if con.execute(
            "select 1 from wikidata_fetch_chunks where query_name='birthplace' and chunk_index=? and status='ok'",
            (chunk_index,),
        ).fetchone() and cache_path.exists():
            continue
        log(f"fetching NBA all-time birthplace chunk {chunk_index + 1}/{len(grouped)} ({len(chunk_qids)} players)")
        data = fetch_or_load(cache_path, birthplace_query(chunk_qids), max_retries, rate_limit_sleep)
        out = []
        for binding in data.get("results", {}).get("bindings", []):
            qid = qid_from_url(binding.get("person", {}).get("value"))
            birthplace_qid = qid_from_url(binding.get("birthplace", {}).get("value"))
            lat, lon = parse_point(binding.get("coord", {}).get("value"))
            out.append(
                {
                    "bbr_id": bbr_by_qid.get(qid),
                    "wikidata_qid": qid,
                    "birthplace_qid": birthplace_qid,
                    "birthplace_label": binding.get("birthplaceLabel", {}).get("value", ""),
                    "located_in_label": binding.get("locatedInLabel", {}).get("value", ""),
                    "country_label": binding.get("countryLabel", {}).get("value", ""),
                    "latitude": lat,
                    "longitude": lon,
                    "raw_cache_path": repo_path(cache_path),
                }
            )
        con.executemany(
            """
            insert or replace into nba_alltime_birthplaces
            (bbr_id, wikidata_qid, birthplace_qid, birthplace_label, located_in_label, country_label, latitude, longitude, raw_cache_path)
            values
            (:bbr_id, :wikidata_qid, :birthplace_qid, :birthplace_label, :located_in_label, :country_label, :latitude, :longitude, :raw_cache_path)
            """,
            [row for row in out if row["bbr_id"] and row["birthplace_qid"]],
        )
        con.execute(
            """
            insert or replace into wikidata_fetch_chunks
            (query_name, chunk_index, cache_path, row_count, status)
            values ('birthplace', ?, ?, ?, 'ok')
            """,
            (chunk_index, repo_path(cache_path), len(out)),
        )
        con.commit()
        log(f"  cached {len(out)} birthplace rows")
        time.sleep(sleep_seconds)


def fetch_education(
    con: sqlite3.Connection,
    qids: list[str],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    bbr_by_qid = dict(con.execute("select wikidata_qid, bbr_id from nba_alltime_players where wikidata_qid is not null"))
    grouped = chunks(qids, chunk_size)
    for chunk_index, chunk_qids in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"nba_alltime_education_chunk_{chunk_index:05d}.json"
        if con.execute(
            "select 1 from wikidata_fetch_chunks where query_name='education' and chunk_index=? and status='ok'",
            (chunk_index,),
        ).fetchone() and cache_path.exists():
            continue
        log(f"fetching NBA all-time education chunk {chunk_index + 1}/{len(grouped)} ({len(chunk_qids)} players)")
        data = fetch_or_load(cache_path, education_query(chunk_qids), max_retries, rate_limit_sleep)
        out = []
        for binding in data.get("results", {}).get("bindings", []):
            qid = qid_from_url(binding.get("person", {}).get("value"))
            school_qid = qid_from_url(binding.get("school", {}).get("value"))
            lat, lon = parse_point(binding.get("coord", {}).get("value"))
            out.append(
                {
                    "bbr_id": bbr_by_qid.get(qid),
                    "wikidata_qid": qid,
                    "school_qid": school_qid,
                    "school_label": binding.get("schoolLabel", {}).get("value", ""),
                    "located_in_label": binding.get("locatedInLabel", {}).get("value", ""),
                    "is_high_school": 1 if binding.get("isHighSchool", {}).get("value") == "true" else 0,
                    "is_university": 1 if binding.get("isUniversity", {}).get("value") == "true" else 0,
                    "latitude": lat,
                    "longitude": lon,
                    "raw_cache_path": repo_path(cache_path),
                }
            )
        con.executemany(
            """
            insert or replace into nba_alltime_education
            (bbr_id, wikidata_qid, school_qid, school_label, located_in_label, is_high_school, is_university, latitude, longitude, raw_cache_path)
            values
            (:bbr_id, :wikidata_qid, :school_qid, :school_label, :located_in_label, :is_high_school, :is_university, :latitude, :longitude, :raw_cache_path)
            """,
            [row for row in out if row["bbr_id"] and row["school_qid"]],
        )
        con.execute(
            """
            insert or replace into wikidata_fetch_chunks
            (query_name, chunk_index, cache_path, row_count, status)
            values ('education', ?, ?, ?, 'ok')
            """,
            (chunk_index, repo_path(cache_path), len(out)),
        )
        con.commit()
        log(f"  cached {len(out)} education rows")
        time.sleep(sleep_seconds)


def write_summary(con: sqlite3.Connection) -> None:
    summary = {
        "sqlite_cache": repo_path(DB_PATH),
        "players": con.execute("select count(*) from nba_alltime_players").fetchone()[0],
        "players_with_birthplace": con.execute("select count(distinct bbr_id) from nba_alltime_birthplaces").fetchone()[0],
        "players_with_geocoded_birthplace": con.execute(
            "select count(distinct bbr_id) from nba_alltime_birthplaces where latitude is not null and longitude is not null"
        ).fetchone()[0],
        "players_with_education": con.execute("select count(distinct bbr_id) from nba_alltime_education").fetchone()[0],
        "players_with_high_school": con.execute(
            "select count(distinct bbr_id) from nba_alltime_education where is_high_school = 1"
        ).fetchone()[0],
        "players_with_college": con.execute(
            "select count(distinct bbr_id) from nba_alltime_education where is_university = 1"
        ).fetchone()[0],
        "players_with_image_file": con.execute(
            "select count(*) from nba_alltime_players where image_file is not null and image_file != ''"
        ).fetchone()[0],
        "notes": [
            "All-time NBA/ABA identity is seeded from Wikidata P2685 Basketball Reference NBA player ID.",
            "This runner does not scrape Basketball-Reference pages.",
            "Education rows are Wikidata P69 attended/educated-at associations, not confirmed sports participation.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    log(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Wikidata all-time NBA/ABA player identity and location cache.")
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--max-chunks", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=10)
    parser.add_argument("--rate-limit-sleep", type=float, default=90.0)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--skip-birthplace", action="store_true")
    parser.add_argument("--skip-education", action="store_true")
    args = parser.parse_args()

    max_chunks = None if args.max_chunks == 0 else args.max_chunks
    con = init_db(args.reset)
    fetch_players(con, args.max_retries, args.rate_limit_sleep)
    qids = player_qids(con)
    if not args.skip_birthplace:
        fetch_birthplaces(con, qids, args.chunk_size, max_chunks, args.sleep_seconds, args.max_retries, args.rate_limit_sleep)
    if not args.skip_education:
        fetch_education(con, qids, args.chunk_size, max_chunks, args.sleep_seconds, args.max_retries, args.rate_limit_sleep)
    write_summary(con)
    con.close()


if __name__ == "__main__":
    main()
