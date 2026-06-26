#!/usr/bin/env python3
"""
Build a conservative player honors cache.

MLB All-Star and Hall of Fame data come from Lahman's downloadable tables.
NFL Hall of Fame status comes from Wikidata's Pro Football Hall of Fame ID
property (P6930), keyed through Pro-Football-Reference player IDs. NFL
Pro Bowl and All-Pro selections are parsed from cached Wikipedia infobox
career highlights when explicitly requested.
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
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_WIKIDATA = ROOT / "data/raw/wikidata/honors"
RAW_WIKIPEDIA = ROOT / "data/raw/wikipedia/nfl_honors"
LAHMAN_DATA = ROOT / "data/raw/mlb/lahman-cran-14.0-0/extracted/Lahman/data"
NFL_PLAYERS_CSV = ROOT / "data/raw/nfl/nflverse-players-2026-06-24/players.csv"
DB_PATH = SCRATCH / "player_honors.sqlite"
SUMMARY_PATH = SCRATCH / "player_honors_summary.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "HometownHeroesPipeline/0.1 (CC0 structured data)"


import pandas as pd
import rdata


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


def load_rdata(name: str) -> pd.DataFrame:
    obj = rdata.conversion.convert(rdata.parser.parse_file(LAHMAN_DATA / f"{name}.RData"))
    return obj[name]


def clean(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


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
                        "external_property": "P3561",
                        "external_id": variant,
                    }
                )
    rows.sort(key=lambda r: r["external_id"])
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
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 10.0 * attempt
            except ValueError:
                delay = 10.0 * attempt
            log(f"HTTP {exc.code}; backing off {delay:.1f}s")
            time.sleep(delay)
        except URLError as exc:
            last_exc = exc
            if attempt == 4:
                raise
            delay = 5.0 * attempt
            log(f"URL error; backing off {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(f"SPARQL request failed: {last_exc!r}")


def nfl_hof_query(external_ids: list[str]) -> str:
    values = " ".join(json.dumps(v) for v in external_ids)
    return f"""
SELECT ?pfr ?person ?personLabel ?hofId WHERE {{
  VALUES ?pfr {{ {values} }}
  ?person wdt:P3561 ?pfr ;
          wdt:P6930 ?hofId .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def nfl_enwiki_title_query(external_ids: list[str]) -> str:
    values = " ".join(json.dumps(v) for v in external_ids)
    return f"""
SELECT ?pfr ?person ?personLabel ?article WHERE {{
  VALUES ?pfr {{ {values} }}
  ?person wdt:P3561 ?pfr .
  OPTIONAL {{
    ?article schema:about ?person ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def init_db(reset: bool) -> sqlite3.Connection:
    SCRATCH.mkdir(exist_ok=True)
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        create table if not exists player_honor_events (
            sport text not null,
            source_player_id text not null,
            honor_type text not null,
            honor_label text not null,
            honor_year integer,
            source text not null,
            source_key text not null,
            raw_cache_path text,
            wikidata_qid text,
            external_property text,
            external_id text,
            primary key (sport, source_player_id, honor_type, source_key)
        );

        create table if not exists player_honor_summary (
            sport text not null,
            source_player_id text not null,
            all_star_count integer not null default 0,
            all_pro_count integer not null default 0,
            hof_inducted integer not null default 0,
            hof_year integer,
            honor_sources text not null,
            primary key (sport, source_player_id)
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

        create table if not exists wikidata_enwiki_title_chunks (
            sport text,
            chunk_index integer,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (sport, chunk_index)
        );

        create table if not exists nfl_wikipedia_title_map (
            sport text not null,
            source_player_id text not null,
            wikidata_qid text,
            person_label text,
            external_property text not null,
            external_id text not null,
            article_title text,
            raw_cache_path text,
            primary key (sport, source_player_id, external_property, external_id)
        );

        create table if not exists wikipedia_fetch_chunks (
            sport text,
            chunk_index integer,
            cache_path text,
            page_count integer,
            honor_event_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (sport, chunk_index)
        );
        """
    )
    con.commit()
    return con


def load_mlb_honors(con: sqlite3.Connection) -> None:
    allstars = load_rdata("AllstarFull")
    hof = load_rdata("HallOfFame")

    allstar_rows = []
    for row_index, row in allstars.iterrows():
        player_id = clean(row.get("playerID"))
        year = clean(row.get("yearID"))
        game_id = clean(row.get("gameID")) or f"{year}-{clean(row.get('gameNum'))}"
        if not player_id or year is None:
            continue
        allstar_rows.append(
            {
                "sport": "MLB",
                "source_player_id": str(player_id),
                "honor_type": "all_star",
                "honor_label": "MLB All-Star",
                "honor_year": int(year),
                "source": "Lahman AllstarFull",
                "source_key": f"{year}:{clean(row.get('gameNum'))}:{game_id}:{clean(row.get('teamID'))}:{clean(row.get('lgID'))}:{row_index}",
                "raw_cache_path": repo_path(LAHMAN_DATA / "AllstarFull.RData"),
                "wikidata_qid": None,
                "external_property": None,
                "external_id": None,
            }
        )

    hof_rows = []
    inducted = hof[(hof["inducted"].astype(str) == "Y") & (hof["category"].astype(str) == "Player")]
    for _, row in inducted.iterrows():
        player_id = clean(row.get("playerID"))
        year = clean(row.get("yearID"))
        if not player_id or year is None:
            continue
        voted_by = clean(row.get("votedBy")) or ""
        category = clean(row.get("category")) or ""
        hof_rows.append(
            {
                "sport": "MLB",
                "source_player_id": str(player_id),
                "honor_type": "hall_of_fame",
                "honor_label": "National Baseball Hall of Fame",
                "honor_year": int(year),
                "source": "Lahman HallOfFame",
                "source_key": f"{year}:{voted_by}:{category}",
                "raw_cache_path": repo_path(LAHMAN_DATA / "HallOfFame.RData"),
                "wikidata_qid": None,
                "external_property": None,
                "external_id": None,
            }
        )

    con.executemany(
        """
        insert or replace into player_honor_events
        (sport, source_player_id, honor_type, honor_label, honor_year, source, source_key,
         raw_cache_path, wikidata_qid, external_property, external_id)
        values
        (:sport, :source_player_id, :honor_type, :honor_label, :honor_year, :source, :source_key,
         :raw_cache_path, :wikidata_qid, :external_property, :external_id)
        """,
        allstar_rows + hof_rows,
    )
    con.commit()
    log(f"cached {len(allstar_rows)} MLB All-Star rows and {len(hof_rows)} MLB HOF rows")


def existing_chunk(con: sqlite3.Connection, sport: str, chunk_index: int) -> str | None:
    row = con.execute(
        "select cache_path from wikidata_fetch_chunks where sport=? and chunk_index=? and status='ok'",
        (sport, chunk_index),
    ).fetchone()
    return row[0] if row else None


def process_nfl_hof_bindings(con: sqlite3.Connection, source_rows: list[dict[str, str]], data: dict, cache_path: Path) -> int:
    by_external = {row["external_id"]: row for row in source_rows}
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        external_id = binding.get("pfr", {}).get("value", "")
        source = by_external.get(external_id)
        if not source:
            continue
        person_qid = qid_from_url(binding.get("person", {}).get("value"))
        hof_id = binding.get("hofId", {}).get("value", "")
        out.append(
            {
                "sport": "NFL",
                "source_player_id": source["source_player_id"],
                "honor_type": "hall_of_fame",
                "honor_label": "Pro Football Hall of Fame",
                "honor_year": None,
                "source": "Wikidata P6930 Pro Football Hall of Fame ID",
                "source_key": hof_id,
                "raw_cache_path": repo_path(cache_path),
                "wikidata_qid": person_qid,
                "external_property": source["external_property"],
                "external_id": source["external_id"],
            }
        )
    con.executemany(
        """
        insert or replace into player_honor_events
        (sport, source_player_id, honor_type, honor_label, honor_year, source, source_key,
         raw_cache_path, wikidata_qid, external_property, external_id)
        values
        (:sport, :source_player_id, :honor_type, :honor_label, :honor_year, :source, :source_key,
         :raw_cache_path, :wikidata_qid, :external_property, :external_id)
        """,
        out,
    )
    con.commit()
    return len(out)


def fetch_nfl_hof(con: sqlite3.Connection, rows: list[dict[str, str]], chunk_size: int, max_chunks: int | None, sleep_seconds: float) -> None:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    grouped = chunks(rows, chunk_size)
    processed = 0
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and processed >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"nfl_hof_chunk_{chunk_index:05d}.json"
        cached = existing_chunk(con, "NFL", chunk_index)
        if cached and cache_path.exists():
            processed += 1
            continue
        ids = [row["external_id"] for row in chunk_rows if row["external_id"]]
        if not ids:
            processed += 1
            continue
        log(f"fetching NFL HOF chunk {chunk_index + 1}/{len(grouped)} ({len(ids)} ids)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = sparql_request(nfl_hof_query(ids))
                cache_path.write_text(json.dumps(data))
            row_count = process_nfl_hof_bindings(con, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, ?, 'ok')
                """,
                ("NFL", chunk_index, repo_path(cache_path), row_count),
            )
            con.commit()
            log(f"  cached {row_count} NFL HOF rows")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikidata_fetch_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 0, ?)
                """,
                ("NFL", chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        processed += 1
        time.sleep(sleep_seconds)


def existing_title_chunk(con: sqlite3.Connection, sport: str, chunk_index: int) -> str | None:
    row = con.execute(
        "select cache_path from wikidata_enwiki_title_chunks where sport=? and chunk_index=? and status='ok'",
        (sport, chunk_index),
    ).fetchone()
    return row[0] if row else None


def process_title_bindings(con: sqlite3.Connection, source_rows: list[dict[str, str]], data: dict, cache_path: Path) -> int:
    by_external = {row["external_id"]: row for row in source_rows}
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        external_id = binding.get("pfr", {}).get("value", "")
        source = by_external.get(external_id)
        if not source:
            continue
        article_url = binding.get("article", {}).get("value", "")
        article_title = urllib.parse.unquote(article_url.rsplit("/", 1)[-1]).replace("_", " ") if article_url else ""
        out.append(
            {
                "sport": "NFL",
                "source_player_id": source["source_player_id"],
                "wikidata_qid": qid_from_url(binding.get("person", {}).get("value")),
                "person_label": binding.get("personLabel", {}).get("value", ""),
                "external_property": source["external_property"],
                "external_id": source["external_id"],
                "article_title": article_title,
                "raw_cache_path": repo_path(cache_path),
            }
        )
    con.executemany(
        """
        insert or replace into nfl_wikipedia_title_map
        (sport, source_player_id, wikidata_qid, person_label, external_property, external_id, article_title, raw_cache_path)
        values
        (:sport, :source_player_id, :wikidata_qid, :person_label, :external_property, :external_id, :article_title, :raw_cache_path)
        """,
        out,
    )
    con.commit()
    return len(out)


def fetch_nfl_enwiki_titles(
    con: sqlite3.Connection,
    rows: list[dict[str, str]],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
) -> None:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    grouped = chunks(rows, chunk_size)
    processed = 0
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and processed >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"nfl_enwiki_titles_chunk_{chunk_index:05d}.json"
        cached = existing_title_chunk(con, "NFL", chunk_index)
        if cached and cache_path.exists():
            processed += 1
            continue
        ids = [row["external_id"] for row in chunk_rows if row["external_id"]]
        if not ids:
            processed += 1
            continue
        log(f"fetching NFL enwiki title chunk {chunk_index + 1}/{len(grouped)} ({len(ids)} ids)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = sparql_request(nfl_enwiki_title_query(ids))
                cache_path.write_text(json.dumps(data))
            row_count = process_title_bindings(con, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikidata_enwiki_title_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, ?, 'ok')
                """,
                ("NFL", chunk_index, repo_path(cache_path), row_count),
            )
            con.commit()
            log(f"  cached {row_count} title rows")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikidata_enwiki_title_chunks
                (sport, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 0, ?)
                """,
                ("NFL", chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        processed += 1
        time.sleep(sleep_seconds)


def wikipedia_request(titles: list[str]) -> dict:
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": "|".join(titles),
        "rvprop": "content",
        "rvsection": "0",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        WIKIPEDIA_API_URL,
        data=data,
        headers={
            "User-Agent": USER_AGENT + " (Wikipedia CC BY-SA enrichment)",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise
            delay = 10.0 * attempt
            log(f"Wikipedia HTTP {exc.code}; backing off {delay:.1f}s")
            time.sleep(delay)
        except URLError as exc:
            last_exc = exc
            if attempt == 4:
                raise
            delay = 5.0 * attempt
            log(f"Wikipedia URL error; backing off {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(f"Wikipedia request failed: {last_exc!r}")


def extract_template(wikitext: str, template_names: tuple[str, ...]) -> str:
    lowered = wikitext.lower()
    match = None
    for name in template_names:
        found = lowered.find("{{" + name.lower())
        if found >= 0 and (match is None or found < match):
            match = found
    if match is None:
        return ""

    depth = 0
    index = match
    while index < len(wikitext) - 1:
        pair = wikitext[index : index + 2]
        if pair == "{{":
            depth += 1
            index += 2
            continue
        if pair == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                return wikitext[match:index]
            continue
        index += 1
    return ""


def extract_highlights(wikitext: str) -> str:
    template = extract_template(
        wikitext,
        (
            "infobox nfl biography",
            "infobox nfl player",
            "infobox gridiron football biography",
            "infobox gridiron football person",
            "infobox canadian football biography",
        ),
    )
    if not template:
        return ""

    lines = template.splitlines()
    capture = False
    collected: list[str] = []
    for line in lines:
        if re.match(r"^\|\s*highlights\s*=", line, flags=re.IGNORECASE):
            capture = True
            collected.append(re.sub(r"^\|\s*highlights\s*=\s*", "", line, flags=re.IGNORECASE))
            continue
        if capture and re.match(r"^\|\s*(stat|pfr|nfl|cfl|module|espn|si|databasefootball|status|pastteams)\b", line, flags=re.IGNORECASE):
            break
        if capture:
            collected.append(line)
    return "\n".join(collected)


def display_wikitext(value: str) -> str:
    value = re.sub(r"<ref[^>/]*/>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\{\{\s*nfly\s*\|\s*(\d{4})\s*\}\}", r"\1", value, flags=re.IGNORECASE)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = value.replace("&nbsp;", " ")
    value = re.sub(r"'''?", "", value)
    return re.sub(r"\s+", " ", value).strip()


def expand_years(value: str) -> list[int]:
    years: list[int] = []
    consumed: list[tuple[int, int]] = []
    for match in re.finditer(r"\b((?:19|20)\d{2})\s*[–-]\s*((?:19|20)\d{2})\b", value):
        start = int(match.group(1))
        end = int(match.group(2))
        if start <= end and end - start <= 30:
            years.extend(range(start, end + 1))
            consumed.append(match.span())

    for match in re.finditer(r"\b((?:19|20)\d{2})\b", value):
        if any(start <= match.start() and match.end() <= end for start, end in consumed):
            continue
        year = int(match.group(1))
        if year not in years:
            years.append(year)
    return years


def line_count(value: str, years: list[int]) -> int:
    match = re.search(r"\b(\d{1,2})\s*[×x]\b", value)
    if match:
        return int(match.group(1))
    return max(1, len(years))


def parse_wikipedia_honor_events(source: dict[str, str], wikitext: str, cache_path: Path) -> list[dict[str, object]]:
    highlights = extract_highlights(wikitext)
    if not highlights:
        return []

    rows: list[dict[str, object]] = []
    article_title = source["article_title"]
    for raw_line in highlights.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("*"):
            continue
        line = display_wikitext(stripped.lstrip("*").strip())
        lower = line.lower()
        if "pro bowl" in lower:
            honor_type = "all_star"
            honor_label = "NFL Pro Bowl"
        elif "all-pro" in lower:
            honor_type = "all_pro"
            if "first-team" in lower:
                honor_label = "NFL First-team All-Pro"
            elif "second-team" in lower:
                honor_label = "NFL Second-team All-Pro"
            else:
                honor_label = "NFL All-Pro"
        else:
            continue

        years = expand_years(line)
        count = line_count(line, years)
        if years and len(years) > count:
            count = len(years)
        for index in range(count):
            year = years[index] if index < len(years) else None
            rows.append(
                {
                    "sport": "NFL",
                    "source_player_id": source["source_player_id"],
                    "honor_type": honor_type,
                    "honor_label": honor_label,
                    "honor_year": year,
                    "source": "Wikipedia infobox career highlights",
                    "source_key": f"{article_title}:{honor_type}:{honor_label}:{year or 'unknown'}:{index + 1}",
                    "raw_cache_path": repo_path(cache_path),
                    "wikidata_qid": source.get("wikidata_qid"),
                    "external_property": source["external_property"],
                    "external_id": source["external_id"],
                }
            )
    return rows


def existing_wikipedia_chunk(con: sqlite3.Connection, sport: str, chunk_index: int) -> str | None:
    row = con.execute(
        "select cache_path from wikipedia_fetch_chunks where sport=? and chunk_index=? and status='ok'",
        (sport, chunk_index),
    ).fetchone()
    return row[0] if row else None


def title_key(value: str) -> str:
    return value.replace("_", " ").strip().casefold()


def process_wikipedia_pages(con: sqlite3.Connection, source_rows: list[dict[str, str]], data: dict, cache_path: Path) -> tuple[int, int]:
    by_title: dict[str, list[dict[str, str]]] = {}
    for row in source_rows:
        by_title.setdefault(title_key(row["article_title"]), []).append(row)
    for redirect in data.get("query", {}).get("redirects", []):
        source_key = title_key(redirect.get("from", ""))
        target_key = title_key(redirect.get("to", ""))
        if source_key in by_title:
            by_title.setdefault(target_key, []).extend(by_title[source_key])

    out: list[dict[str, object]] = []
    page_count = 0
    for page in data.get("query", {}).get("pages", []):
        if page.get("missing"):
            continue
        revisions = page.get("revisions") or []
        if not revisions:
            continue
        content = revisions[0].get("slots", {}).get("main", {}).get("content", "")
        sources = by_title.get(title_key(page.get("title", "")), [])
        if not sources or not content:
            continue
        page_count += 1
        for source in sources:
            out.extend(parse_wikipedia_honor_events(source, content, cache_path))

    con.executemany(
        """
        insert or replace into player_honor_events
        (sport, source_player_id, honor_type, honor_label, honor_year, source, source_key,
         raw_cache_path, wikidata_qid, external_property, external_id)
        values
        (:sport, :source_player_id, :honor_type, :honor_label, :honor_year, :source, :source_key,
         :raw_cache_path, :wikidata_qid, :external_property, :external_id)
        """,
        out,
    )
    con.commit()
    return page_count, len(out)


def fetch_nfl_wikipedia_honors(
    con: sqlite3.Connection,
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
) -> None:
    RAW_WIKIPEDIA.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "source_player_id": row[0],
            "wikidata_qid": row[1],
            "external_property": row[2],
            "external_id": row[3],
            "article_title": row[4],
        }
        for row in con.execute(
            """
            select source_player_id, wikidata_qid, external_property, external_id, article_title
            from nfl_wikipedia_title_map
            where article_title is not null and article_title != ''
            order by article_title, source_player_id
            """
        ).fetchall()
    ]
    grouped = chunks(rows, chunk_size)
    processed = 0
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and processed >= max_chunks:
            break
        cache_path = RAW_WIKIPEDIA / f"nfl_honors_pages_chunk_{chunk_index:05d}.json"
        cached = existing_wikipedia_chunk(con, "NFL", chunk_index)
        if cached and cache_path.exists():
            processed += 1
            continue
        titles = sorted({row["article_title"] for row in chunk_rows})
        if not titles:
            processed += 1
            continue
        log(f"fetching NFL Wikipedia honors page chunk {chunk_index + 1}/{len(grouped)} ({len(titles)} titles)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = wikipedia_request(titles)
                cache_path.write_text(json.dumps(data))
            page_count, event_count = process_wikipedia_pages(con, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikipedia_fetch_chunks
                (sport, chunk_index, cache_path, page_count, honor_event_count, status)
                values (?, ?, ?, ?, ?, 'ok')
                """,
                ("NFL", chunk_index, repo_path(cache_path), page_count, event_count),
            )
            con.commit()
            log(f"  parsed {event_count} honor rows from {page_count} pages")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikipedia_fetch_chunks
                (sport, chunk_index, cache_path, page_count, honor_event_count, status)
                values (?, ?, ?, 0, 0, ?)
                """,
                ("NFL", chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        processed += 1
        time.sleep(sleep_seconds)


def rebuild_summary(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        delete from player_honor_summary;

        insert or replace into player_honor_summary
        (sport, source_player_id, all_star_count, all_pro_count, hof_inducted, hof_year, honor_sources)
        select
            sport,
            source_player_id,
            count(case when honor_type = 'all_star' then 1 end) as all_star_count,
            count(case when honor_type = 'all_pro' then 1 end) as all_pro_count,
            max(case when honor_type = 'hall_of_fame' then 1 else 0 end) as hof_inducted,
            min(case when honor_type = 'hall_of_fame' then honor_year end) as hof_year,
            group_concat(distinct source) as honor_sources
        from player_honor_events
        group by sport, source_player_id;
        """
    )
    con.commit()


def export_query(con: sqlite3.Connection, query: str, path: Path) -> None:
    cur = con.execute(query)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)


def write_exports(con: sqlite3.Connection) -> dict:
    events_path = SCRATCH / "player_honor_events.csv"
    summary_export_path = SCRATCH / "player_honor_summary.csv"
    export_query(
        con,
        "select * from player_honor_events order by sport, source_player_id, honor_type, honor_year",
        events_path,
    )
    export_query(
        con,
        "select * from player_honor_summary order by sport, source_player_id",
        summary_export_path,
    )
    summary = {
        "sqlite_cache": repo_path(DB_PATH),
        "honor_events": con.execute("select count(*) from player_honor_events").fetchone()[0],
        "summary_players": con.execute("select count(*) from player_honor_summary").fetchone()[0],
        "events_by_sport_type": {
            f"{sport}:{honor_type}": count
            for sport, honor_type, count in con.execute(
                """
                select sport, honor_type, count(*)
                from player_honor_events
                group by sport, honor_type
                order by sport, honor_type
                """
            ).fetchall()
        },
        "players_by_sport_type": {
            f"{sport}:{honor_type}": count
            for sport, honor_type, count in con.execute(
                """
                select sport, honor_type, count(distinct source_player_id)
                from player_honor_events
                group by sport, honor_type
                order by sport, honor_type
                """
            ).fetchall()
        },
        "exports": {
            "events": repo_path(events_path),
            "summary": repo_path(summary_export_path),
        },
        "notes": [
            "MLB all_star_count is Lahman AllstarFull row count.",
            "MLB hof_inducted is Lahman HallOfFame inducted='Y' and category='Player'.",
            "NFL hof_inducted is Wikidata P6930 presence, keyed by nflverse PFR ID.",
            "NFL all_star_count and all_pro_count are parsed from Wikipedia infobox career highlights when --include-wikipedia-nfl is used.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sports", default="MLB,NFL", help="Comma-separated sports: MLB,NFL")
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--max-chunks", type=int, default=0, help="NFL chunks to fetch; use 0 for all")
    parser.add_argument("--max-wikipedia-chunks", type=int, default=0, help="Wikipedia page chunks to fetch; use 0 for all")
    parser.add_argument("--wikipedia-chunk-size", type=int, default=50)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--include-wikipedia-nfl", action="store_true")
    parser.add_argument("--no-reset", action="store_true", help="Keep existing cache tables and append/replace rows")
    args = parser.parse_args()

    sports = {s.strip().upper() for s in args.sports.split(",") if s.strip()}
    max_chunks = None if args.max_chunks == 0 else args.max_chunks
    max_wikipedia_chunks = None if args.max_wikipedia_chunks == 0 else args.max_wikipedia_chunks

    con = init_db(reset=not args.no_reset)
    if "MLB" in sports:
        load_mlb_honors(con)
    if "NFL" in sports:
        nfl_rows = load_nfl_pfr_ids()
        fetch_nfl_hof(con, nfl_rows, args.chunk_size, max_chunks, args.sleep_seconds)
        if args.include_wikipedia_nfl:
            fetch_nfl_enwiki_titles(con, nfl_rows, args.chunk_size, max_chunks, args.sleep_seconds)
            fetch_nfl_wikipedia_honors(con, args.wikipedia_chunk_size, max_wikipedia_chunks, args.sleep_seconds)
    rebuild_summary(con)
    summary = write_exports(con)
    con.close()
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
