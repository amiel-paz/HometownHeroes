#!/usr/bin/env python3
"""
Build the Wikidata education enrichment cache.

The pipeline uses Wikidata structured data (P69 education) plus identifiers
already present in the source snapshots. Raw SPARQL responses are cached under
data/raw/wikidata so runs can resume without refetching completed chunks.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_WIKIDATA = ROOT / "data/raw/wikidata/education"
CHADWICK_DATA = ROOT / "data/raw/mlb/chadwick-register-master/extracted/register-master/data"
NFL_PLAYERS_CSV = ROOT / "data/raw/nfl/nflverse-players-2026-06-24/players.csv"
NBA_DB = SCRATCH / "nba_enrichment.sqlite"
DB_PATH = SCRATCH / "wikidata_education_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "wikidata_education_summary.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "HometownHeroesPipeline/0.1 (CC0 structured data)"


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


def load_mlb_wikidata_ids() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(CHADWICK_DATA.glob("people-*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                qid = row.get("key_wikidata", "").strip()
                bbref = row.get("key_bbref", "").strip()
                if qid and bbref:
                    rows.append(
                        {
                            "sport": "MLB",
                            "source_player_id": bbref,
                            "wikidata_qid": qid,
                            "external_property": "P1825",
                            "external_id": bbref,
                        }
                    )
    rows.sort(key=lambda r: r["wikidata_qid"])
    return rows


def pfr_variants(pfr_id: str) -> list[str]:
    pfr_id = (pfr_id or "").strip()
    if not pfr_id:
        return []
    if "/" not in pfr_id:
        return [f"{pfr_id[0]}/{pfr_id}"]
    return [pfr_id]


def load_nfl_pfr_ids() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with NFL_PLAYERS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pfr = row.get("pfr_id", "").strip()
            gsis = row.get("gsis_id", "").strip()
            for variant in pfr_variants(pfr):
                rows.append(
                    {
                        "sport": "NFL",
                        "source_player_id": gsis or pfr,
                        "wikidata_qid": "",
                        "external_property": "P3561",
                        "external_id": variant,
                    }
                )
    rows.sort(key=lambda r: r["external_id"])
    return rows


def load_nba_espn_ids() -> list[dict[str, str]]:
    if not NBA_DB.exists():
        log(f"NBA cache missing: {NBA_DB}")
        return []
    con = sqlite3.connect(NBA_DB)
    rows = [
        {
            "sport": "NBA",
            "source_player_id": str(row[0]),
            "wikidata_qid": "",
            "external_property": "P3685",
            "external_id": str(row[0]),
        }
        for row in con.execute(
            """
            select athlete_id
            from nba_players
            where athlete_id is not null and athlete_id != ''
            order by athlete_id
            """
        ).fetchall()
    ]
    con.close()
    return rows


def chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def sparql_request(query: str) -> dict:
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
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def mlb_query(qids: list[str]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?person ?personLabel ?school ?schoolLabel ?coord ?locatedInLabel ?isHighSchool ?isUniversity WHERE {{
  VALUES ?person {{ {values} }}
  ?person wdt:P69 ?school .
  OPTIONAL {{ ?school wdt:P625 ?coord . }}
  OPTIONAL {{ ?school wdt:P131 ?locatedIn . }}
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q9826 }} AS ?isHighSchool)
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q3918 }} AS ?isUniversity)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def nfl_query(external_ids: list[str]) -> str:
    values = " ".join(json.dumps(v) for v in external_ids)
    return f"""
SELECT ?pfr ?person ?personLabel ?school ?schoolLabel ?coord ?locatedInLabel ?isHighSchool ?isUniversity WHERE {{
  VALUES ?pfr {{ {values} }}
  ?person wdt:P3561 ?pfr ;
          wdt:P69 ?school .
  OPTIONAL {{ ?school wdt:P625 ?coord . }}
  OPTIONAL {{ ?school wdt:P131 ?locatedIn . }}
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q9826 }} AS ?isHighSchool)
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q3918 }} AS ?isUniversity)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def nba_query(external_ids: list[str]) -> str:
    values = " ".join(json.dumps(v) for v in external_ids)
    return f"""
SELECT ?espn ?person ?personLabel ?school ?schoolLabel ?coord ?locatedInLabel ?isHighSchool ?isUniversity WHERE {{
  VALUES ?espn {{ {values} }}
  ?person wdt:P3685 ?espn ;
          wdt:P69 ?school .
  OPTIONAL {{ ?school wdt:P625 ?coord . }}
  OPTIONAL {{ ?school wdt:P131 ?locatedIn . }}
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q9826 }} AS ?isHighSchool)
  BIND(EXISTS {{ ?school wdt:P31/wdt:P279* wd:Q3918 }} AS ?isUniversity)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def init_db() -> sqlite3.Connection:
    SCRATCH.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        create table if not exists wikidata_source_ids (
            sport text,
            source_player_id text,
            wikidata_qid text,
            external_property text,
            external_id text,
            primary key (sport, source_player_id, external_property, external_id)
        );

        create table if not exists wikidata_education_events (
            sport text,
            source_player_id text,
            wikidata_qid text,
            external_property text,
            external_id text,
            person_label text,
            school_qid text,
            school_label text,
            located_in_label text,
            is_high_school integer,
            is_university integer,
            latitude real,
            longitude real,
            raw_cache_path text,
            primary key (sport, source_player_id, wikidata_qid, school_qid, external_id)
        );

        create table if not exists wikidata_fetch_chunks (
            sport text,
            chunk_index integer,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (sport, chunk_index)
        );
        """
    )
    con.commit()
    return con


def insert_source_ids(con: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    con.executemany(
        """
        insert or ignore into wikidata_source_ids
        (sport, source_player_id, wikidata_qid, external_property, external_id)
        values (:sport, :source_player_id, :wikidata_qid, :external_property, :external_id)
        """,
        rows,
    )
    con.commit()


def existing_chunk(con: sqlite3.Connection, sport: str, chunk_index: int) -> str | None:
    row = con.execute(
        "select cache_path from wikidata_fetch_chunks where sport=? and chunk_index=? and status='ok'",
        (sport, chunk_index),
    ).fetchone()
    return row[0] if row else None


def bool_value(binding: dict, key: str) -> int:
    value = binding.get(key, {}).get("value")
    return 1 if value == "true" else 0


def classify_school_label(label: str, is_high_school: int, is_university: int) -> tuple[int, int]:
    lowered = label.casefold()
    if not is_high_school and "high school" in lowered:
        is_high_school = 1
    if not is_university and ("university" in lowered or "college" in lowered):
        is_university = 1
    return is_high_school, is_university


def process_bindings(con: sqlite3.Connection, sport: str, source_rows: list[dict[str, str]], data: dict, cache_path: Path) -> int:
    by_external = {(row["external_property"], row["external_id"]): row for row in source_rows}
    by_qid = {row["wikidata_qid"]: row for row in source_rows if row["wikidata_qid"]}
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        person_qid = qid_from_url(binding.get("person", {}).get("value"))
        if sport == "MLB":
            source = by_qid.get(person_qid or "")
            external_id = ""
        else:
            external_id = binding.get("pfr", {}).get("value", "") or binding.get("espn", {}).get("value", "")
            external_property = "P3561" if sport == "NFL" else "P3685"
            source = by_external.get((external_property, external_id))
        if not source or not person_qid:
            continue
        school_qid = qid_from_url(binding.get("school", {}).get("value"))
        lat, lon = parse_point(binding.get("coord", {}).get("value"))
        school_label = binding.get("schoolLabel", {}).get("value", "")
        is_high_school, is_university = classify_school_label(
            school_label,
            bool_value(binding, "isHighSchool"),
            bool_value(binding, "isUniversity"),
        )
        out.append(
            {
                "sport": source["sport"],
                "source_player_id": source["source_player_id"],
                "wikidata_qid": person_qid,
                "external_property": source["external_property"],
                "external_id": source["external_id"],
                "person_label": binding.get("personLabel", {}).get("value", ""),
                "school_qid": school_qid or "",
                "school_label": school_label,
                "located_in_label": binding.get("locatedInLabel", {}).get("value", ""),
                "is_high_school": is_high_school,
                "is_university": is_university,
                "latitude": lat,
                "longitude": lon,
                "raw_cache_path": repo_path(cache_path),
            }
        )
    con.executemany(
        """
        insert or replace into wikidata_education_events
        (sport, source_player_id, wikidata_qid, external_property, external_id, person_label,
         school_qid, school_label, located_in_label, is_high_school, is_university,
         latitude, longitude, raw_cache_path)
        values
        (:sport, :source_player_id, :wikidata_qid, :external_property, :external_id, :person_label,
         :school_qid, :school_label, :located_in_label, :is_high_school, :is_university,
         :latitude, :longitude, :raw_cache_path)
        """,
        out,
    )
    con.commit()
    return len(out)


def fetch_sport(con: sqlite3.Connection, sport: str, rows: list[dict[str, str]], chunk_size: int, max_chunks: int | None, sleep_seconds: float) -> None:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    grouped = chunks(rows, chunk_size)
    processed = 0
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and processed >= max_chunks:
            break
        cached = existing_chunk(con, sport, chunk_index)
        cache_path = RAW_WIKIDATA / f"{sport.lower()}_education_chunk_{chunk_index:05d}.json"
        if cached and cache_path.exists():
            continue
        if sport == "MLB":
            ids = [row["wikidata_qid"] for row in chunk_rows if row["wikidata_qid"]]
            query = mlb_query(ids)
        elif sport == "NFL":
            ids = [row["external_id"] for row in chunk_rows if row["external_id"]]
            query = nfl_query(ids)
        else:
            ids = [row["external_id"] for row in chunk_rows if row["external_id"]]
            query = nba_query(ids)
        if not ids:
            continue
        log(f"fetching {sport} chunk {chunk_index + 1}/{len(grouped)} ({len(ids)} ids)")
        try:
            data = sparql_request(query)
            cache_path.write_text(json.dumps(data))
            event_count = process_bindings(con, sport, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, ?, 'ok')
                """,
                (sport, chunk_index, repo_path(cache_path), event_count),
            )
            con.commit()
            log(f"  cached {event_count} education rows")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 0, ?)
                """,
                (sport, chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        processed += 1
        time.sleep(sleep_seconds)


def write_exports(con: sqlite3.Connection) -> dict:
    export_path = SCRATCH / "wikidata_education_events.csv"
    high_school_path = SCRATCH / "wikidata_high_school_events.csv"
    for query, path in [
        ("select * from wikidata_education_events order by sport, source_player_id, school_label", export_path),
        ("select * from wikidata_education_events where is_high_school=1 order by sport, source_player_id, school_label", high_school_path),
    ]:
        rows = con.execute(query).fetchall()
        cols = [d[0] for d in con.execute(query).description]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
    summary = {
        "sqlite_cache": repo_path(DB_PATH),
        "education_events": con.execute("select count(*) from wikidata_education_events").fetchone()[0],
        "high_school_events": con.execute("select count(*) from wikidata_education_events where is_high_school=1").fetchone()[0],
        "events_with_coordinates": con.execute("select count(*) from wikidata_education_events where latitude is not null and longitude is not null").fetchone()[0],
        "by_sport": dict(con.execute("select sport, count(*) from wikidata_education_events group by sport").fetchall()),
        "high_school_by_sport": dict(con.execute("select sport, count(*) from wikidata_education_events where is_high_school=1 group by sport").fetchall()),
        "exports": {
            "all_events": repo_path(export_path),
            "high_school_events": repo_path(high_school_path),
        },
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sports", default="MLB,NFL", help="Comma-separated sports: MLB,NFL,NBA")
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--max-chunks", type=int, default=10, help="Chunks per sport for this run; use 0 for all")
    parser.add_argument("--sleep-seconds", type=float, default=0.4)
    args = parser.parse_args()

    con = init_db()
    sports = {s.strip().upper() for s in args.sports.split(",") if s.strip()}
    max_chunks = None if args.max_chunks == 0 else args.max_chunks

    if "MLB" in sports:
        rows = load_mlb_wikidata_ids()
        insert_source_ids(con, rows)
        fetch_sport(con, "MLB", rows, args.chunk_size, max_chunks, args.sleep_seconds)
    if "NFL" in sports:
        rows = load_nfl_pfr_ids()
        insert_source_ids(con, rows)
        fetch_sport(con, "NFL", rows, args.chunk_size, max_chunks, args.sleep_seconds)
    if "NBA" in sports:
        rows = load_nba_espn_ids()
        insert_source_ids(con, rows)
        fetch_sport(con, "NBA", rows, args.chunk_size, max_chunks, args.sleep_seconds)

    summary = write_exports(con)
    con.close()
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
