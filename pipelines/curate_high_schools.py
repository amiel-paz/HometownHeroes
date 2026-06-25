#!/usr/bin/env python3
"""
Curate high school associations from conservative/public layers.

The pipeline writes derived review tables under scratch/. Wikipedia-derived
rows retain source URLs, revision IDs, and license metadata.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
DATA = ROOT / "data"

HOMETOWN_DB = SCRATCH / "hometown_heroes.sqlite"
WIKIDATA_DB = SCRATCH / "wikidata_education_enrichment.sqlite"
OUT_DB = SCRATCH / "high_school_curation.sqlite"
OUT_SUMMARY = SCRATCH / "high_school_curation_summary.json"
OUT_EVENTS_CSV = SCRATCH / "curated_high_school_events.csv"
OUT_CANDIDATES_CSV = SCRATCH / "wikipedia_high_school_candidates.csv"

NCES_DIR = DATA / "raw" / "education" / "nces-public-school-locations-2024-25"
NCES_ZIP = NCES_DIR / "EDGE_GEOCODE_PUBLICSCH_2425.zip"
NCES_URL = "https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PUBLICSCH_2425.zip"

WIKI_CACHE = SCRATCH / "wikipedia_high_school_cache"
USER_AGENT = "HometownHeroesPipeline/0.1"

STATE_TO_ABBR = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
ABBR_TO_STATE = {v: k.title() for k, v in STATE_TO_ABBR.items()}

SCHOOL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.\- ]{1,90}?(?:High School|Preparatory School|Prep|Academy|School))\b"
)
CITY_STATE_RE = re.compile(
    r"\bin\s+([A-Z][A-Za-z .'\-]{1,80}),\s+("
    + "|".join(re.escape(s.title()) for s in STATE_TO_ABBR)
    + r"|[A-Z]{2})\b"
)
ACTION_RE = re.compile(r"\b(attended|graduated from|went to|played (?:baseball|football|basketball|hockey)?\s*(?:at|for))\b", re.I)


@dataclass(frozen=True)
class Player:
    sport: str
    player_id: str
    display_name: str | None
    wikidata_qid: str


def log(message: str) -> None:
    print(message, flush=True)


def ensure_dirs() -> None:
    NCES_DIR.mkdir(parents=True, exist_ok=True)
    WIKI_CACHE.mkdir(parents=True, exist_ok=True)


def http_get(url: str, *, accept: str = "*/*", max_attempts: int = 4) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code != 429 or attempt == max_attempts:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 10.0 * attempt
            except ValueError:
                delay = 10.0 * attempt
            log(f"HTTP 429 from upstream; backing off for {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def download_nces() -> Path:
    ensure_dirs()
    if not NCES_ZIP.exists():
        log(f"Downloading NCES public school locations: {NCES_URL}")
        NCES_ZIP.write_bytes(http_get(NCES_URL))
    with zipfile.ZipFile(NCES_ZIP) as zf:
        names = zf.namelist()
        csv_names = [n for n in names if n.lower().endswith((".csv", ".txt"))]
        if not csv_names:
            raise RuntimeError(f"No CSV/TXT table found in {NCES_ZIP}")
        csv_name = csv_names[0]
        out_csv = NCES_DIR / Path(csv_name).name
        if not out_csv.exists():
            log(f"Extracting NCES CSV: {csv_name}")
            with zf.open(csv_name) as src, out_csv.open("wb") as dst:
                dst.write(src.read())
        return out_csv


def norm_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(jr|sr|senior|junior)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_state(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if len(value) == 2:
        return value.upper()
    return STATE_TO_ABBR.get(value.lower())


def reset_db(conn: sqlite3.Connection, *, reset: bool) -> None:
    if reset:
        conn.executescript(
            """
            drop table if exists source_metadata;
            drop table if exists nces_public_schools;
            drop table if exists players_to_check;
            drop table if exists player_wikipedia_pages;
            drop table if exists wikipedia_high_school_candidates;
            drop table if exists curated_high_school_events;
            """
        )
    conn.executescript(
        """
        create table if not exists source_metadata (
          source_key text primary key,
          source_url text,
          source_license text,
          snapshot_path text,
          notes text
        );

        create table if not exists nces_public_schools (
          ncessch text primary key,
          name text,
          city text,
          state text,
          zip text,
          latitude real,
          longitude real,
          norm_name text,
          norm_city text
        );
        create index if not exists idx_nces_norm on nces_public_schools(norm_name, state, norm_city);

        create table if not exists players_to_check (
          sport text not null,
          player_id text not null,
          display_name text,
          wikidata_qid text not null,
          already_has_high_school integer not null default 0,
          primary key (sport, player_id)
        );

        create table if not exists player_wikipedia_pages (
          wikidata_qid text primary key,
          title text,
          pageid integer,
          revid integer,
          fetched_at text default current_timestamp,
          status text not null,
          cache_path text,
          error text
        );

        create table if not exists wikipedia_high_school_candidates (
          candidate_id text primary key,
          sport text not null,
          player_id text not null,
          display_name text,
          wikidata_qid text not null,
          wikipedia_title text,
          wikipedia_revid integer,
          school_name text not null,
          city text,
          state text,
          sentence text,
          extraction_rule text not null,
          matched_ncessch text,
          matched_school_name text,
          matched_city text,
          matched_state text,
          latitude real,
          longitude real,
          confidence text not null,
          curation_status text not null,
          source_layer text not null default 'wikipedia_cc_by_sa',
          source_url text,
          source_license text not null default 'CC BY-SA 4.0'
        );
        create index if not exists idx_candidates_player on wikipedia_high_school_candidates(sport, player_id);

        create table if not exists curated_high_school_events (
          event_id text primary key,
          sport text not null,
          player_id text not null,
          display_name text,
          event_type text not null,
          school_name text not null,
          city text,
          state text,
          country text,
          latitude real,
          longitude real,
          source_layer text not null,
          source_url text,
          source_revision_or_snapshot text,
          source_license text,
          extraction_method text not null,
          confidence text not null,
          curation_status text not null,
          notes text
        );
        create index if not exists idx_curated_player on curated_high_school_events(sport, player_id);
        """
    )


def import_nces(conn: sqlite3.Connection, csv_path: Path) -> int:
    existing = conn.execute("select count(*) from nces_public_schools").fetchone()[0]
    if existing:
        return existing
    log(f"Importing NCES schools from {csv_path}")
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = "|" if "|" in sample else ("\t" if "\t" in sample else ",")
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        has_header = any(token in first_line.upper().split(delimiter) for token in ("NCESSCH", "NAME", "CITY"))
        fieldnames = None
        if not has_header:
            fieldnames = [
                "NCESSCH",
                "LEAID",
                "NAME",
                "OPSTFIPS",
                "STREET",
                "CITY",
                "STATE",
                "ZIP",
                "STFIP",
                "CNTY",
                "NMCNTY",
                "LOCALE",
                "LAT",
                "LON",
                "CBSA",
                "NMCBSA",
                "CBSATYPE",
                "CSA",
                "NMCSA",
                "CD",
                "SLDL",
                "SLDU",
                "SCHOOLYEAR",
            ]
        reader = csv.DictReader(f, delimiter=delimiter, fieldnames=fieldnames)
        rows = []
        for row in reader:
            name = row.get("NAME") or row.get("SCH_NAME")
            city = row.get("CITY")
            state = canonical_state(row.get("STATE"))
            ncessch = row.get("NCESSCH") or row.get("OBJECTID") or row.get("FID")
            if not (ncessch and name and city and state):
                continue
            lat = row.get("LAT") or row.get("Y")
            lon = row.get("LON") or row.get("X")
            rows.append(
                (
                    ncessch,
                    name,
                    city,
                    state,
                    row.get("ZIP"),
                    float(lat) if lat else None,
                    float(lon) if lon else None,
                    norm_text(name),
                    norm_text(city),
                )
            )
    conn.executemany(
        """
        insert or replace into nces_public_schools
        (ncessch, name, city, state, zip, latitude, longitude, norm_name, norm_city)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def attach_inputs(conn: sqlite3.Connection) -> None:
    conn.execute("attach database ? as hh", (str(HOMETOWN_DB),))
    conn.execute("attach database ? as wd", (str(WIKIDATA_DB),))


def import_wikidata_high_schools(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        insert or replace into curated_high_school_events
        (
          event_id, sport, player_id, display_name, event_type, school_name,
          city, state, country, latitude, longitude, source_layer, source_url,
          source_revision_or_snapshot, source_license, extraction_method,
          confidence, curation_status, notes
        )
        select
          'wikidata:' || e.sport || ':' || e.source_player_id || ':' || e.school_qid,
          e.sport,
          e.source_player_id,
          coalesce(p.display_name, e.person_label),
          'attended_high_school',
          e.school_label,
          e.located_in_label,
          null,
          null,
          e.latitude,
          e.longitude,
          'wikidata_cc0',
          'https://www.wikidata.org/wiki/' || e.wikidata_qid,
          e.school_qid,
          'CC0',
          'wikidata_p69',
          'source_reported',
          'accepted',
          'Imported from Wikidata P69 education rows classified as high school.'
        from wd.wikidata_education_events e
        left join hh.players p
          on p.sport = e.sport and p.player_id = e.source_player_id
        where e.is_high_school = 1
        """
    )
    conn.commit()
    return conn.execute(
        "select count(*) from curated_high_school_events where extraction_method = 'wikidata_p69'"
    ).fetchone()[0]


def load_players_to_check(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        insert or ignore into players_to_check (sport, player_id, display_name, wikidata_qid, already_has_high_school)
        select p.sport, p.player_id, p.display_name, s.wikidata_qid,
               case when c.player_id is null then 0 else 1 end
        from hh.players p
        join wd.wikidata_source_ids s
          on s.sport = p.sport and s.source_player_id = p.player_id
        left join (
          select sport, player_id
          from curated_high_school_events
          where event_type = 'attended_high_school' and curation_status = 'accepted'
          group by sport, player_id
        ) c on c.sport = p.sport and c.player_id = p.player_id
        where s.wikidata_qid is not null and s.wikidata_qid <> ''
        """
    )
    conn.execute(
        """
        insert or ignore into players_to_check (sport, player_id, display_name, wikidata_qid, already_has_high_school)
        select p.sport, p.player_id, p.display_name, e.wikidata_qid,
               case when c.player_id is null then 0 else 1 end
        from hh.players p
        join (
          select sport, source_player_id, wikidata_qid
          from wd.wikidata_education_events
          where wikidata_qid is not null and wikidata_qid <> ''
          group by sport, source_player_id, wikidata_qid
        ) e on e.sport = p.sport and e.source_player_id = p.player_id
        left join (
          select sport, player_id
          from curated_high_school_events
          where event_type = 'attended_high_school' and curation_status = 'accepted'
          group by sport, player_id
        ) c on c.sport = p.sport and c.player_id = p.player_id
        """
    )
    conn.commit()
    return conn.execute("select count(*) from players_to_check").fetchone()[0]


def cache_json_path(kind: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    return WIKI_CACHE / f"{kind}_{safe}.json"


def fetch_json_cached(kind: str, key: str, url: str, *, sleep_seconds: float) -> dict:
    path = cache_json_path(kind, key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    time.sleep(sleep_seconds)
    data = json.loads(http_get(url).decode("utf-8"))
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def get_enwiki_title(qid: str, *, sleep_seconds: float) -> tuple[str | None, str | None]:
    params = urllib.parse.urlencode(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "sitelinks",
            "sitefilter": "enwiki",
            "format": "json",
        }
    )
    data = fetch_json_cached("wikidata_entity", qid, f"https://www.wikidata.org/w/api.php?{params}", sleep_seconds=sleep_seconds)
    entity = data.get("entities", {}).get(qid, {})
    title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
    if not title:
        return None, "no_enwiki_sitelink"
    return title, None


def get_wikipedia_extract(title: str, *, sleep_seconds: float) -> dict:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "extracts|revisions|info",
            "explaintext": "1",
            "rvprop": "ids|timestamp",
            "inprop": "url",
            "redirects": "1",
            "titles": title,
            "format": "json",
            "formatversion": "2",
        }
    )
    return fetch_json_cached("enwiki_page", title, f"https://en.wikipedia.org/w/api.php?{params}", sleep_seconds=sleep_seconds)


def page_cache_exists(title: str | None) -> bool:
    return bool(title) and cache_json_path("enwiki_page", title).exists()


def prefetch_wikipedia_extracts(titles: list[str], *, sleep_seconds: float, batch_size: int = 50) -> None:
    unique_titles = []
    seen = set()
    for title in titles:
        if not title or title in seen or page_cache_exists(title):
            continue
        seen.add(title)
        unique_titles.append(title)
    if not unique_titles:
        return
    for i in range(0, len(unique_titles), batch_size):
        chunk = unique_titles[i : i + batch_size]
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "extracts|revisions|info",
                "explaintext": "1",
                "rvprop": "ids|timestamp",
                "inprop": "url",
                "redirects": "1",
                "titles": "|".join(chunk),
                "format": "json",
                "formatversion": "2",
            }
        )
        time.sleep(sleep_seconds)
        data = json.loads(http_get(f"https://en.wikipedia.org/w/api.php?{params}").decode("utf-8"))
        pages = data.get("query", {}).get("pages", [])
        by_requested = {}
        redirects = data.get("query", {}).get("redirects", [])
        for redir in redirects:
            if redir.get("from") and redir.get("to"):
                by_requested[redir["from"]] = redir["to"]
        pages_by_title = {page.get("title"): page for page in pages if page.get("title")}
        for requested in chunk:
            resolved = by_requested.get(requested, requested)
            page = pages_by_title.get(resolved) or pages_by_title.get(requested)
            if page is None:
                page = {"title": requested, "missing": True}
            cache_json_path("enwiki_page", requested).write_text(
                json.dumps({"query": {"pages": [page]}}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        log(f"Prefetched Wikipedia extracts {min(i + batch_size, len(unique_titles))}/{len(unique_titles)}")


def split_sentences(text: str) -> Iterable[str]:
    text = text.replace("\r", "\n")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    for chunk in chunks:
        sentence = re.sub(r"\s+", " ", chunk).strip()
        if sentence:
            yield sentence


def extract_candidates(text: str) -> list[dict]:
    candidates = []
    for sentence in split_sentences(text):
        if "high school" not in sentence.lower():
            continue
        if not ACTION_RE.search(sentence):
            continue
        schools = SCHOOL_RE.findall(sentence)
        if not schools:
            continue
        city = None
        state = None
        city_state = CITY_STATE_RE.search(sentence)
        if city_state:
            city = city_state.group(1).strip()
            state = canonical_state(city_state.group(2))
        for school in schools:
            school = re.sub(r"^(the|The)\s+", "", school).strip(" ,.;")
            school = re.sub(
                r"^.*\b(?:attended|graduated from|went to|played (?:baseball|football|basketball|hockey)?\s*(?:at|for))\s+",
                "",
                school,
                flags=re.I,
            ).strip(" ,.;")
            school = re.sub(r"^high school\s+(?:at|for)\s+", "", school, flags=re.I).strip(" ,.;")
            school = re.sub(r"^(the|The)\s+", "", school).strip(" ,.;")
            if len(school) < 6:
                continue
            candidates.append(
                {
                    "school_name": school,
                    "city": city,
                    "state": state,
                    "sentence": sentence,
                    "extraction_rule": "sentence_high_school_action_v1",
                }
            )
    return candidates


def find_nces_match(conn: sqlite3.Connection, school_name: str, city: str | None, state: str | None) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    norm_name = norm_text(school_name)
    norm_names = [norm_name]
    if norm_name.endswith(" high school"):
        norm_names.append(norm_name[: -len(" school")])
    if norm_name.endswith(" preparatory school"):
        norm_names.append(norm_name[: -len(" school")])
    if norm_name.endswith(" school"):
        norm_names.append(norm_name[: -len(" school")])
    norm_names = list(dict.fromkeys(n for n in norm_names if n))
    norm_city = norm_text(city)
    state = canonical_state(state)
    if not state and not norm_city:
        return None

    for name_variant in norm_names:
        params: list[object] = [name_variant]
        where = ["norm_name = ?"]
        if state:
            where.append("state = ?")
            params.append(state)
        if norm_city:
            where.append("norm_city = ?")
            params.append(norm_city)
        row = conn.execute(
            f"select * from nces_public_schools where {' and '.join(where)} limit 1",
            params,
        ).fetchone()
        if row:
            return row

    # Slightly looser school-name matching for common suffix variations.
    for name_variant in norm_names:
        params = [f"%{name_variant}%"]
        where = ["norm_name like ?"]
        if state:
            where.append("state = ?")
            params.append(state)
        if norm_city:
            where.append("norm_city = ?")
            params.append(norm_city)
        row = conn.execute(
            f"select * from nces_public_schools where {' and '.join(where)} order by length(norm_name) limit 1",
            params,
        ).fetchone()
        if row:
            return row
    return None


def candidate_id(parts: Iterable[object]) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def insert_candidate(conn: sqlite3.Connection, player: Player, page: dict, cand: dict, match: sqlite3.Row | None) -> None:
    title = page["title"]
    revid = page["revid"]
    page_url = page["fullurl"]
    school_name = cand["school_name"]
    city = cand["city"]
    state = cand["state"]
    confidence = "medium"
    status = "candidate"
    if match and city and state:
        confidence = "high"
        status = "accepted"
    elif match:
        confidence = "medium_high"
    cid = candidate_id([player.sport, player.player_id, title, revid, school_name, city, state])
    conn.execute(
        """
        insert or replace into wikipedia_high_school_candidates
        (
          candidate_id, sport, player_id, display_name, wikidata_qid, wikipedia_title,
          wikipedia_revid, school_name, city, state, sentence, extraction_rule,
          matched_ncessch, matched_school_name, matched_city, matched_state,
          latitude, longitude, confidence, curation_status, source_url
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            player.sport,
            player.player_id,
            player.display_name,
            player.wikidata_qid,
            title,
            revid,
            school_name,
            city,
            state,
            cand["sentence"],
            cand["extraction_rule"],
            match["ncessch"] if match else None,
            match["name"] if match else None,
            match["city"] if match else None,
            match["state"] if match else None,
            match["latitude"] if match else None,
            match["longitude"] if match else None,
            confidence,
            status,
            page_url,
        ),
    )
    if status == "accepted":
        event_id = f"wikipedia:{cid}"
        conn.execute(
            """
            insert or replace into curated_high_school_events
            (
              event_id, sport, player_id, display_name, event_type, school_name,
              city, state, country, latitude, longitude, source_layer, source_url,
              source_revision_or_snapshot, source_license, extraction_method,
              confidence, curation_status, notes
            )
            values (?, ?, ?, ?, 'attended_high_school', ?, ?, ?, 'US', ?, ?, 'wikipedia_cc_by_sa', ?, ?, 'CC BY-SA 4.0',
                    'wikipedia_rule_plus_nces', ?, 'accepted', ?)
            """,
            (
                event_id,
                player.sport,
                player.player_id,
                player.display_name,
                match["name"],
                match["city"],
                match["state"],
                match["latitude"],
                match["longitude"],
                page_url,
                str(revid),
                confidence,
                f"Extracted from Wikipedia sentence and matched to NCES {match['ncessch']}.",
            ),
        )


def select_players(conn: sqlite3.Connection, *, player: str | None, sport: str | None, max_pages: int) -> list[Player]:
    where = "already_has_high_school = 0"
    params: list[object] = []
    if player:
        if ":" in player:
            sport, player_id = player.split(":", 1)
            where += " and sport = ? and player_id = ?"
            params.extend([sport.upper(), player_id])
        else:
            where += " and player_id = ?"
            params.append(player)
    elif sport:
        where += " and sport = ?"
        params.append(sport.upper())
    sql = f"""
      select sport, player_id, display_name, wikidata_qid
      from players_to_check
      where {where}
        and wikidata_qid not in (select wikidata_qid from player_wikipedia_pages where status = 'processed')
      order by sport, player_id
      limit ?
    """
    params.append(max_pages)
    rows = conn.execute(sql, params).fetchall()
    return [Player(*row) for row in rows]


def process_players(conn: sqlite3.Connection, players: list[Player], *, sleep_seconds: float, title_fallback: bool) -> dict:
    processed = 0
    with_candidates = 0
    accepted = 0
    stopped_due_to_rate_limit = False
    try:
        prefetch_wikipedia_extracts(
            [p.display_name for p in players if p.display_name],
            sleep_seconds=sleep_seconds,
        )
    except HTTPError as exc:
        if exc.code == 429:
            log("Stopping before row processing after upstream rate limit during batch prefetch; rerun later to resume.")
            return {
                "processed_pages": 0,
                "players_with_candidates": 0,
                "accepted_wikipedia_events": 0,
                "stopped_due_to_rate_limit": True,
            }
        raise
    for player in players:
        try:
            page_data = None
            title = player.display_name
            title_error = None
            if title:
                page_data = get_wikipedia_extract(title, sleep_seconds=sleep_seconds)
                pages = page_data.get("query", {}).get("pages", [])
                if not pages or pages[0].get("missing"):
                    page_data = None
            if page_data is None and title_fallback:
                title, title_error = get_enwiki_title(player.wikidata_qid, sleep_seconds=sleep_seconds)
            if page_data is None and (title_error or not title):
                conn.execute(
                    "insert or replace into player_wikipedia_pages (wikidata_qid, status, error) values (?, 'skipped', ?)",
                    (player.wikidata_qid, title_error),
                )
                conn.commit()
                processed += 1
                continue
            if page_data is None:
                page_data = get_wikipedia_extract(title, sleep_seconds=sleep_seconds)
            pages = page_data.get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing"):
                conn.execute(
                    "insert or replace into player_wikipedia_pages (wikidata_qid, title, status, error) values (?, ?, 'skipped', ?)",
                    (player.wikidata_qid, title, "missing_enwiki_page"),
                )
                conn.commit()
                processed += 1
                continue
            page = pages[0]
            extract = page.get("extract") or ""
            revs = page.get("revisions") or [{}]
            revid = revs[0].get("revid")
            fullurl = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            page_record = {
                "title": page.get("title") or title,
                "pageid": page.get("pageid"),
                "revid": revid,
                "fullurl": fullurl,
            }
            cache_path = cache_json_path("enwiki_page", title)
            conn.execute(
                """
                insert or replace into player_wikipedia_pages
                (wikidata_qid, title, pageid, revid, status, cache_path)
                values (?, ?, ?, ?, 'processed', ?)
                """,
                (player.wikidata_qid, page_record["title"], page_record["pageid"], revid, str(cache_path)),
            )

            candidates = extract_candidates(extract)
            if candidates:
                with_candidates += 1
            for cand in candidates:
                match = find_nces_match(conn, cand["school_name"], cand["city"], cand["state"])
                before = conn.execute(
                    "select count(*) from curated_high_school_events where source_layer = 'wikipedia_cc_by_sa'"
                ).fetchone()[0]
                insert_candidate(conn, player, page_record, cand, match)
                after = conn.execute(
                    "select count(*) from curated_high_school_events where source_layer = 'wikipedia_cc_by_sa'"
                ).fetchone()[0]
                accepted += max(0, after - before)
            conn.commit()
            processed += 1
            if processed % 25 == 0:
                log(f"Processed {processed}/{len(players)} Wikipedia pages...")
        except HTTPError as exc:
            if exc.code == 429:
                stopped_due_to_rate_limit = True
                conn.execute(
                    "insert or replace into player_wikipedia_pages (wikidata_qid, status, error) values (?, 'rate_limited', ?)",
                    (player.wikidata_qid, repr(exc)),
                )
                conn.commit()
                log("Stopping batch after upstream rate limit; rerun later to resume.")
                break
            raise
        except Exception as exc:  # keep the runner resumable across bad pages
            conn.execute(
                "insert or replace into player_wikipedia_pages (wikidata_qid, status, error) values (?, 'error', ?)",
                (player.wikidata_qid, repr(exc)),
            )
            conn.commit()
            log(f"Error for {player.sport}:{player.player_id} {player.wikidata_qid}: {exc}")
            processed += 1
    return {
        "processed_pages": processed,
        "players_with_candidates": with_candidates,
        "accepted_wikipedia_events": accepted,
        "stopped_due_to_rate_limit": stopped_due_to_rate_limit,
    }


def write_metadata(conn: sqlite3.Connection, nces_csv: Path) -> None:
    rows = [
        (
            "nces_public_school_locations_2024_25",
            NCES_URL,
            "Public domain / U.S. government data as described by Data.gov/NCES metadata",
            str(nces_csv),
            "Used for public high school canonicalization and coordinates.",
        ),
        (
            "wikidata_p69",
            "https://www.wikidata.org/",
            "CC0",
            str(WIKIDATA_DB),
            "Existing local Wikidata education enrichment cache.",
        ),
        (
            "wikipedia_extracts",
            "https://en.wikipedia.org/w/api.php",
            "CC BY-SA 4.0",
            str(WIKI_CACHE),
            "Used for candidate extraction only; rows retain source URL and revision ID.",
        ),
    ]
    conn.executemany(
        "insert or replace into source_metadata (source_key, source_url, source_license, snapshot_path, notes) values (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def write_summary(conn: sqlite3.Connection, run_stats: dict) -> dict:
    summary = {
        **run_stats,
        "nces_school_rows": conn.execute("select count(*) from nces_public_schools").fetchone()[0],
        "players_to_check": conn.execute("select count(*) from players_to_check").fetchone()[0],
        "players_missing_high_school_with_qid": conn.execute(
            "select count(*) from players_to_check where already_has_high_school = 0"
        ).fetchone()[0],
        "wikidata_high_school_events": conn.execute(
            "select count(*) from curated_high_school_events where source_layer = 'wikidata_cc0'"
        ).fetchone()[0],
        "wikipedia_candidates": conn.execute("select count(*) from wikipedia_high_school_candidates").fetchone()[0],
        "wikipedia_accepted_events": conn.execute(
            "select count(*) from curated_high_school_events where source_layer = 'wikipedia_cc_by_sa'"
        ).fetchone()[0],
        "curated_events_total": conn.execute("select count(*) from curated_high_school_events").fetchone()[0],
    }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def export_table(conn: sqlite3.Connection, table: str, path: Path) -> None:
    rows = conn.execute(f"select * from {table}").fetchall()
    cols = [desc[0] for desc in conn.execute(f"select * from {table} limit 0").description]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)


def export_outputs(conn: sqlite3.Connection) -> None:
    export_table(conn, "curated_high_school_events", OUT_EVENTS_CSV)
    export_table(conn, "wikipedia_high_school_candidates", OUT_CANDIDATES_CSV)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Rebuild output tables.")
    parser.add_argument("--max-pages", type=int, default=100, help="Maximum Wikipedia pages to fetch/process this run.")
    parser.add_argument("--player", help="Optional player filter, e.g. MLB:kwanst01.")
    parser.add_argument("--sport", choices=["MLB", "NFL"], help="Optional sport filter for broader resumable runs.")
    parser.add_argument("--no-title-fallback", action="store_true", help="Skip Wikidata sitelink fallback after exact-name page misses.")
    parser.add_argument("--sleep-seconds", type=float, default=1.0, help="Delay before uncached HTTP requests.")
    args = parser.parse_args()

    if not HOMETOWN_DB.exists():
        raise SystemExit(f"Missing {HOMETOWN_DB}")
    if not WIKIDATA_DB.exists():
        raise SystemExit(f"Missing {WIKIDATA_DB}")

    ensure_dirs()
    nces_csv = download_nces()
    conn = sqlite3.connect(OUT_DB)
    try:
        reset_db(conn, reset=args.reset)
        attach_inputs(conn)
        nces_rows = import_nces(conn, nces_csv)
        write_metadata(conn, nces_csv)
        wd_rows = import_wikidata_high_schools(conn)
        player_count = load_players_to_check(conn)
        log(f"NCES schools: {nces_rows:,}")
        log(f"Accepted Wikidata high school events: {wd_rows:,}")
        log(f"Players with Wikidata QIDs to evaluate: {player_count:,}")
        players = select_players(conn, player=args.player, sport=args.sport, max_pages=args.max_pages)
        log(f"Wikipedia pages selected this run: {len(players):,}")
        run_stats = process_players(conn, players, sleep_seconds=args.sleep_seconds, title_fallback=not args.no_title_fallback)
        summary = write_summary(conn, run_stats)
        export_outputs(conn)
        log(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
