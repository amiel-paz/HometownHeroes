#!/usr/bin/env python3
"""
Build a conservative player media cache.

Images are discovered through Wikidata player entities first. If a player has
an English Wikipedia article but no Wikidata P18 image, the script can try the
article's lead image as a fallback. Image metadata is cached from Wikimedia APIs
and only reusable-looking files are marked usable.
"""

from __future__ import annotations

import argparse
import csv
import html
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
RAW_WIKIDATA = ROOT / "data/raw/wikidata/media"
RAW_WIKIPEDIA = ROOT / "data/raw/wikipedia/player_media"
RAW_COMMONS = ROOT / "data/raw/wikimedia_commons/player_media"

CHADWICK_DATA = ROOT / "data/raw/mlb/chadwick-register-master/extracted/register-master/data"
NFL_PLAYERS_CSV = ROOT / "data/raw/nfl/nflverse-players-2026-06-24/players.csv"
NBA_DB = SCRATCH / "nba_enrichment.sqlite"
NBA_ALLTIME_DB = SCRATCH / "nba_alltime_wikidata.sqlite"
NHL_DB = SCRATCH / "nhl_enrichment.sqlite"
DB_PATH = SCRATCH / "player_media.sqlite"
SUMMARY_PATH = SCRATCH / "player_media_summary.json"

SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "HometownHeroesPipeline/0.1 (Wikimedia media attribution cache)"

FREE_LICENSE_HINTS = (
    "cc-by",
    "cc by",
    "cc-by-sa",
    "cc by-sa",
    "cc0",
    "public domain",
    "pd-",
    "gfdl",
)
NON_FREE_HINTS = ("non-free", "fair use", "copyrighted free use", "unknown")


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


def file_title(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = urllib.parse.unquote(value.rsplit("/", 1)[-1]).replace("_", " ")
    if value.casefold().startswith("file:"):
        return value
    return f"File:{value}"


def article_title(article_url: str | None) -> str:
    if not article_url:
        return ""
    return urllib.parse.unquote(article_url.rsplit("/", 1)[-1]).replace("_", " ")


def clean_metadata_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value", "")
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def pfr_variants(pfr_id: str) -> list[str]:
    pfr_id = (pfr_id or "").strip()
    if not pfr_id:
        return []
    if "/" not in pfr_id:
        return [f"{pfr_id[0]}/{pfr_id}"]
    return [pfr_id]


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
    return rows


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
    return rows


def load_nba_espn_ids() -> list[dict[str, str]]:
    if not NBA_DB.exists():
        log(f"NBA cache missing: {repo_path(NBA_DB)}")
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


def load_nba_alltime_bbr_ids() -> list[dict[str, str]]:
    if not NBA_ALLTIME_DB.exists():
        return []
    con = sqlite3.connect(NBA_ALLTIME_DB)
    rows = [
        {
            "sport": "NBA",
            "source_player_id": "bbr:" + str(row[0]),
            "wikidata_qid": str(row[1] or ""),
            "external_property": "P2685",
            "external_id": str(row[0]),
        }
        for row in con.execute(
            """
            select bbr_id, wikidata_qid
            from nba_alltime_players
            where bbr_id is not null and bbr_id != ''
            order by bbr_id
            """
        ).fetchall()
    ]
    con.close()
    return rows


def load_nhl_ids() -> list[dict[str, str]]:
    if not NHL_DB.exists():
        log(f"NHL cache missing: {repo_path(NHL_DB)}")
        return []
    con = sqlite3.connect(NHL_DB)
    rows = [
        {
            "sport": "NHL",
            "source_player_id": str(row[0]),
            "wikidata_qid": "",
            "external_property": "P3522",
            "external_id": str(row[0]),
        }
        for row in con.execute(
            """
            select player_id
            from nhl_players
            where player_id is not null and player_id != ''
            order by player_id
            """
        ).fetchall()
    ]
    con.close()
    return rows


def load_source_rows(sports: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if "MLB" in sports:
        rows.extend(load_mlb_wikidata_ids())
    if "NFL" in sports:
        rows.extend(load_nfl_pfr_ids())
    if "NBA" in sports:
        rows.extend(load_nba_espn_ids())
        rows.extend(load_nba_alltime_bbr_ids())
    if "NHL" in sports:
        rows.extend(load_nhl_ids())
    deduped = {
        (row["sport"], row["source_player_id"], row["external_property"], row["external_id"], row["wikidata_qid"]): row
        for row in rows
        if row["source_player_id"] and (row["external_id"] or row["wikidata_qid"])
    }
    return sorted(deduped.values(), key=lambda row: (row["sport"], row["external_property"], row["external_id"], row["wikidata_qid"]))


def chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


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
    return max((retry_at.timestamp() - time.time()), fallback)


def request_json(
    url: str,
    params: dict[str, object] | None = None,
    *,
    post: bool = True,
    max_retries: int = 10,
    rate_limit_sleep: float = 90.0,
) -> dict:
    data = urllib.parse.urlencode(params or {}).encode("utf-8") if post else None
    full_url = url if post or not params else f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        full_url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                sleep_for = retry_after_seconds(exc, rate_limit_sleep)
            else:
                sleep_for = min(120.0, attempt * 5.0)
            log(f"  request retry {attempt}/{max_retries} after {type(exc).__name__} {getattr(exc, 'code', '')}; sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        except (URLError, TimeoutError) as exc:
            last_exc = exc
            sleep_for = min(120.0, attempt * 5.0)
            log(f"  request retry {attempt}/{max_retries} after {type(exc).__name__}; sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"request failed after retries: {last_exc}")


def sparql_request(query: str, max_retries: int, rate_limit_sleep: float) -> dict:
    return request_json(
        SPARQL_URL,
        {
            "query": query,
            "format": "json",
        },
        max_retries=max_retries,
        rate_limit_sleep=rate_limit_sleep,
    )


def wikidata_media_query(rows: list[dict[str, str]]) -> str:
    qids = sorted({row["wikidata_qid"] for row in rows if row["wikidata_qid"]})
    nfl_ids = sorted({row["external_id"] for row in rows if row["external_property"] == "P3561" and row["external_id"]})
    nba_ids = sorted({row["external_id"] for row in rows if row["external_property"] == "P3685" and row["external_id"]})
    nba_bbr_ids = sorted({row["external_id"] for row in rows if row["external_property"] == "P2685" and row["external_id"]})
    nhl_ids = sorted({row["external_id"] for row in rows if row["external_property"] == "P3522" and row["external_id"]})
    qid_values = " ".join(f"wd:{qid}" for qid in qids) or "wd:Q0"
    nfl_values = " ".join(json.dumps(v) for v in nfl_ids) or '""'
    nba_values = " ".join(json.dumps(v) for v in nba_ids) or '""'
    nba_bbr_values = " ".join(json.dumps(v) for v in nba_bbr_ids) or '""'
    nhl_values = " ".join(json.dumps(v) for v in nhl_ids) or '""'
    return f"""
SELECT ?person ?personLabel ?image ?article ?pfr ?espn ?bbr ?nhl WHERE {{
  {{
    VALUES ?person {{ {qid_values} }}
  }}
  UNION
  {{
    VALUES ?pfr {{ {nfl_values} }}
    ?person wdt:P3561 ?pfr .
  }}
  UNION
  {{
    VALUES ?espn {{ {nba_values} }}
    ?person wdt:P3685 ?espn .
  }}
  UNION
  {{
    VALUES ?bbr {{ {nba_bbr_values} }}
    ?person wdt:P2685 ?bbr .
  }}
  UNION
  {{
    VALUES ?nhl {{ {nhl_values} }}
    ?person wdt:P3522 ?nhl .
  }}
  OPTIONAL {{ ?person wdt:P18 ?image . }}
  OPTIONAL {{
    ?article schema:about ?person ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def init_db(reset: bool) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    if reset:
        con.executescript(
            """
            drop table if exists media_source_players;
            drop table if exists wikidata_media_chunks;
            drop table if exists media_candidates;
            drop table if exists wikipedia_pageimage_chunks;
            drop table if exists imageinfo_chunks;
            drop table if exists player_media;
            """
        )
    con.executescript(
        """
        create table if not exists media_source_players (
            sport text not null,
            source_player_id text not null,
            wikidata_qid text,
            external_property text not null,
            external_id text not null,
            primary key (sport, source_player_id, external_property, external_id)
        );

        create table if not exists wikidata_media_chunks (
            sport_group text not null,
            chunk_index integer not null,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (sport_group, chunk_index)
        );

        create table if not exists media_candidates (
            sport text not null,
            source_player_id text not null,
            wikidata_qid text,
            person_label text,
            article_title text,
            candidate_source text not null,
            file_title text,
            raw_cache_path text,
            primary key (sport, source_player_id, candidate_source)
        );

        create table if not exists wikipedia_pageimage_chunks (
            chunk_index integer primary key,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp
        );

        create table if not exists imageinfo_chunks (
            api_source text not null,
            chunk_index integer not null,
            cache_path text,
            row_count integer,
            status text,
            fetched_at text default current_timestamp,
            primary key (api_source, chunk_index)
        );

        create table if not exists player_media (
            sport text not null,
            source_player_id text not null,
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
            fetched_at text default current_timestamp,
            primary key (sport, source_player_id)
        );
        """
    )
    return con


def insert_source_rows(con: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    con.executemany(
        """
        insert or replace into media_source_players
        (sport, source_player_id, wikidata_qid, external_property, external_id)
        values (:sport, :source_player_id, :wikidata_qid, :external_property, :external_id)
        """,
        rows,
    )
    con.commit()


def import_nba_alltime_image_candidates(con: sqlite3.Connection) -> int:
    if not NBA_ALLTIME_DB.exists():
        return 0
    source = sqlite3.connect(NBA_ALLTIME_DB)
    source.row_factory = sqlite3.Row
    rows = [
        {
            "sport": "NBA",
            "source_player_id": "bbr:" + row["bbr_id"],
            "wikidata_qid": row["wikidata_qid"],
            "person_label": row["display_name"],
            "article_title": row["enwiki_title"],
            "candidate_source": "wikidata_p18",
            "file_title": row["image_file"],
            "raw_cache_path": row["raw_cache_path"],
        }
        for row in source.execute(
            """
            select bbr_id, wikidata_qid, display_name, enwiki_title, image_file, raw_cache_path
            from nba_alltime_players
            where bbr_id is not null and bbr_id != ''
              and image_file is not null and image_file != ''
            """
        ).fetchall()
    ]
    source.close()
    con.executemany(
        """
        insert or replace into media_candidates
        (sport, source_player_id, wikidata_qid, person_label, article_title, candidate_source, file_title, raw_cache_path)
        values
        (:sport, :source_player_id, :wikidata_qid, :person_label, :article_title, :candidate_source, :file_title, :raw_cache_path)
        """,
        rows,
    )
    con.commit()
    return len(rows)


def process_wikidata_bindings(con: sqlite3.Connection, source_rows: list[dict[str, str]], data: dict, cache_path: Path) -> int:
    by_qid = {row["wikidata_qid"]: row for row in source_rows if row["wikidata_qid"]}
    by_property = {(row["external_property"], row["external_id"]): row for row in source_rows if row["external_id"]}
    out = []
    for binding in data.get("results", {}).get("bindings", []):
        qid = qid_from_url(binding.get("person", {}).get("value"))
        source = by_qid.get(qid or "")
        pfr = binding.get("pfr", {}).get("value", "")
        espn = binding.get("espn", {}).get("value", "")
        bbr = binding.get("bbr", {}).get("value", "")
        nhl = binding.get("nhl", {}).get("value", "")
        if not source and pfr:
            source = by_property.get(("P3561", pfr))
        if not source and espn:
            source = by_property.get(("P3685", espn))
        if not source and bbr:
            source = by_property.get(("P2685", bbr))
        if not source and nhl:
            source = by_property.get(("P3522", nhl))
        if not source:
            continue
        image = file_title(binding.get("image", {}).get("value"))
        out.append(
            {
                "sport": source["sport"],
                "source_player_id": source["source_player_id"],
                "wikidata_qid": qid,
                "person_label": binding.get("personLabel", {}).get("value", ""),
                "article_title": article_title(binding.get("article", {}).get("value")),
                "candidate_source": "wikidata_p18",
                "file_title": image,
                "raw_cache_path": repo_path(cache_path),
            }
        )
    con.executemany(
        """
        insert or replace into media_candidates
        (sport, source_player_id, wikidata_qid, person_label, article_title, candidate_source, file_title, raw_cache_path)
        values
        (:sport, :source_player_id, :wikidata_qid, :person_label, :article_title, :candidate_source, :file_title, :raw_cache_path)
        """,
        out,
    )
    con.commit()
    return len(out)


def fetch_wikidata_media(
    con: sqlite3.Connection,
    rows: list[dict[str, str]],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    grouped = chunks(rows, chunk_size)
    sport_group = "_".join(sorted({row["sport"] for row in rows})) if rows else "ALL"
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = RAW_WIKIDATA / f"wikidata_player_media_{sport_group.lower()}_chunk_{chunk_index:05d}.json"
        cached = con.execute(
            "select status from wikidata_media_chunks where sport_group=? and chunk_index=? and status='ok'",
            (sport_group, chunk_index),
        ).fetchone()
        if cached and cache_path.exists():
            continue
        log(f"fetching Wikidata media chunk {chunk_index + 1}/{len(grouped)} ({len(chunk_rows)} players)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = sparql_request(wikidata_media_query(chunk_rows), max_retries, rate_limit_sleep)
                cache_path.write_text(json.dumps(data))
            row_count = process_wikidata_bindings(con, chunk_rows, data, cache_path)
            con.execute(
                """
                insert or replace into wikidata_media_chunks
                (sport_group, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, ?, 'ok')
                """,
                (sport_group, chunk_index, repo_path(cache_path), row_count),
            )
            con.commit()
            log(f"  cached {row_count} media candidates")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikidata_media_chunks
                (sport_group, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 0, ?)
                """,
                (sport_group, chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        time.sleep(sleep_seconds)


def wikipedia_pageimages_request(titles: list[str], max_retries: int, rate_limit_sleep: float) -> dict:
    return request_json(
        WIKIPEDIA_API_URL,
        {
            "action": "query",
            "prop": "pageimages",
            "titles": "|".join(titles),
            "piprop": "name",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
        max_retries=max_retries,
        rate_limit_sleep=rate_limit_sleep,
    )


def fetch_wikipedia_pageimages(
    con: sqlite3.Connection,
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    RAW_WIKIPEDIA.mkdir(parents=True, exist_ok=True)
    rows = [
        dict(row)
        for row in con.execute(
            """
            select sport, source_player_id, wikidata_qid, person_label, article_title
            from media_candidates
            where coalesce(file_title, '') = ''
              and coalesce(article_title, '') != ''
            order by sport, source_player_id
            """
        ).fetchall()
    ]
    grouped = chunks(rows, chunk_size)
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = RAW_WIKIPEDIA / f"wikipedia_pageimages_chunk_{chunk_index:05d}.json"
        cached = con.execute(
            "select status from wikipedia_pageimage_chunks where chunk_index=? and status='ok'",
            (chunk_index,),
        ).fetchone()
        if cached and cache_path.exists():
            continue
        titles = [row["article_title"] for row in chunk_rows if row["article_title"]]
        if not titles:
            continue
        log(f"fetching Wikipedia pageimage chunk {chunk_index + 1}/{len(grouped)} ({len(titles)} pages)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = wikipedia_pageimages_request(titles, max_retries, rate_limit_sleep)
                cache_path.write_text(json.dumps(data))
            by_title = {row["article_title"]: row for row in chunk_rows}
            out = []
            for page in data.get("query", {}).get("pages", []):
                source = by_title.get(page.get("title", ""))
                if not source:
                    continue
                title = file_title(page.get("pageimage", ""))
                if not title:
                    continue
                out.append(
                    {
                        **source,
                        "candidate_source": "wikipedia_pageimage",
                        "file_title": title,
                        "raw_cache_path": repo_path(cache_path),
                    }
                )
            con.executemany(
                """
                insert or replace into media_candidates
                (sport, source_player_id, wikidata_qid, person_label, article_title, candidate_source, file_title, raw_cache_path)
                values
                (:sport, :source_player_id, :wikidata_qid, :person_label, :article_title, :candidate_source, :file_title, :raw_cache_path)
                """,
                out,
            )
            con.execute(
                """
                insert or replace into wikipedia_pageimage_chunks
                (chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 'ok')
                """,
                (chunk_index, repo_path(cache_path), len(out)),
            )
            con.commit()
            log(f"  cached {len(out)} Wikipedia pageimage candidates")
        except Exception as exc:
            con.execute(
                """
                insert or replace into wikipedia_pageimage_chunks
                (chunk_index, cache_path, row_count, status)
                values (?, ?, 0, ?)
                """,
                (chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        time.sleep(sleep_seconds)


def imageinfo_request(api_url: str, titles: list[str], max_retries: int, rate_limit_sleep: float) -> dict:
    return request_json(
        api_url,
        {
            "action": "query",
            "prop": "imageinfo",
            "titles": "|".join(titles),
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": "180",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
        max_retries=max_retries,
        rate_limit_sleep=rate_limit_sleep,
    )


def is_usable_license(api_source: str, license_name: str, usage_terms: str, restrictions: str) -> tuple[int, str]:
    combined = " ".join([license_name, usage_terms, restrictions]).casefold()
    if any(hint in combined for hint in NON_FREE_HINTS):
        return 0, "non-free-or-unknown-license"
    if api_source == "commons":
        return 1, ""
    if any(hint in combined for hint in FREE_LICENSE_HINTS):
        return 1, ""
    return 0, "license-not-recognized-as-free"


def media_row_from_page(candidate: dict[str, str], page: dict, api_source: str, cache_path: Path) -> dict[str, object]:
    infos = page.get("imageinfo") or []
    if not infos:
        return {
            **candidate,
            "media_source": candidate["candidate_source"],
            "thumbnail_url": "",
            "original_url": "",
            "source_page_url": "",
            "author": "",
            "credit": "",
            "license": "",
            "license_url": "",
            "attribution_text": "",
            "attribution_required": 1,
            "usable": 0,
            "rejection_reason": "imageinfo-missing",
            "raw_cache_path": repo_path(cache_path),
        }
    info = infos[0]
    metadata = info.get("extmetadata") or {}
    author = clean_metadata_text(metadata.get("Artist"))
    credit = clean_metadata_text(metadata.get("Credit"))
    license_name = clean_metadata_text(metadata.get("LicenseShortName") or metadata.get("UsageTerms"))
    usage_terms = clean_metadata_text(metadata.get("UsageTerms"))
    restrictions = clean_metadata_text(metadata.get("Restrictions"))
    license_url = clean_metadata_text(metadata.get("LicenseUrl"))
    usable, reason = is_usable_license(api_source, license_name, usage_terms, restrictions)
    attribution_bits = [bit for bit in (author, license_name) if bit]
    attribution = " / ".join(attribution_bits)
    return {
        **candidate,
        "media_source": candidate["candidate_source"],
        "thumbnail_url": info.get("thumburl") or info.get("url") or "",
        "original_url": info.get("url") or "",
        "source_page_url": info.get("descriptionurl") or "",
        "author": author,
        "credit": credit,
        "license": license_name,
        "license_url": license_url,
        "attribution_text": attribution,
        "attribution_required": 1,
        "usable": usable,
        "rejection_reason": reason,
        "raw_cache_path": repo_path(cache_path),
    }


def fetch_imageinfo_for_source(
    con: sqlite3.Connection,
    *,
    api_source: str,
    sports: set[str],
    chunk_size: int,
    max_chunks: int | None,
    sleep_seconds: float,
    max_retries: int,
    rate_limit_sleep: float,
) -> None:
    is_commons = api_source == "commons"
    raw_root = RAW_COMMONS if is_commons else RAW_WIKIPEDIA
    raw_root.mkdir(parents=True, exist_ok=True)
    candidate_source = "wikidata_p18" if is_commons else "wikipedia_pageimage"
    sport_group = "_".join(sorted(sports)) if sports else "ALL"
    api_source_key = f"{api_source}:{sport_group}"
    sport_filter = sorted(sports) if sports else ["MLB", "NBA", "NFL", "NHL"]
    sport_placeholders = ",".join("?" for _ in sport_filter)
    rows = [
        dict(row)
        for row in con.execute(
            f"""
            select sport, source_player_id, wikidata_qid, person_label, article_title, candidate_source, file_title
            from media_candidates
            where candidate_source = ?
              and sport in ({sport_placeholders})
              and coalesce(file_title, '') != ''
            order by sport, source_player_id
            """,
            (candidate_source, *sport_filter),
        ).fetchall()
    ]
    grouped = chunks(rows, chunk_size)
    api_url = COMMONS_API_URL if is_commons else WIKIPEDIA_API_URL
    for chunk_index, chunk_rows in enumerate(grouped):
        if max_chunks is not None and chunk_index >= max_chunks:
            break
        cache_path = raw_root / f"{api_source}_{sport_group.lower()}_imageinfo_chunk_{chunk_index:05d}.json"
        cached = con.execute(
            "select status from imageinfo_chunks where api_source=? and chunk_index=? and status='ok'",
            (api_source_key, chunk_index),
        ).fetchone()
        if cached and cache_path.exists():
            continue
        titles = [row["file_title"] for row in chunk_rows if row["file_title"]]
        log(f"fetching {api_source} imageinfo chunk {chunk_index + 1}/{len(grouped)} ({len(titles)} files)")
        try:
            if cache_path.exists():
                data = json.loads(cache_path.read_text())
            else:
                data = imageinfo_request(api_url, titles, max_retries, rate_limit_sleep)
                cache_path.write_text(json.dumps(data))
            by_title = {row["file_title"].casefold(): row for row in chunk_rows}
            out = []
            for page in data.get("query", {}).get("pages", []):
                candidate = by_title.get(page.get("title", "").casefold())
                if not candidate:
                    continue
                out.append(media_row_from_page(candidate, page, api_source, cache_path))
            con.executemany(
                """
                insert or replace into player_media
                (sport, source_player_id, wikidata_qid, person_label, article_title, media_source, file_title,
                 thumbnail_url, original_url, source_page_url, author, credit, license, license_url,
                 attribution_text, attribution_required, usable, rejection_reason, raw_cache_path)
                values
                (:sport, :source_player_id, :wikidata_qid, :person_label, :article_title, :media_source, :file_title,
                 :thumbnail_url, :original_url, :source_page_url, :author, :credit, :license, :license_url,
                 :attribution_text, :attribution_required, :usable, :rejection_reason, :raw_cache_path)
                """,
                out,
            )
            con.execute(
                """
                insert or replace into imageinfo_chunks
                (api_source, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, ?, 'ok')
                """,
                (api_source_key, chunk_index, repo_path(cache_path), len(out)),
            )
            con.commit()
            log(f"  cached {len(out)} {api_source} media rows")
        except Exception as exc:
            con.execute(
                """
                insert or replace into imageinfo_chunks
                (api_source, chunk_index, cache_path, row_count, status)
                values (?, ?, ?, 0, ?)
                """,
                (api_source_key, chunk_index, repo_path(cache_path), f"error:{type(exc).__name__}:{exc}"),
            )
            con.commit()
            log(f"  error: {type(exc).__name__}: {exc}")
        time.sleep(sleep_seconds)


def write_summary(con: sqlite3.Connection) -> None:
    summary = {
        "sqlite_cache": repo_path(DB_PATH),
        "source_players": con.execute("select count(*) from media_source_players").fetchone()[0],
        "wikidata_candidates": con.execute("select count(*) from media_candidates where candidate_source='wikidata_p18'").fetchone()[0],
        "wikipedia_candidates": con.execute("select count(*) from media_candidates where candidate_source='wikipedia_pageimage'").fetchone()[0],
        "usable_media": con.execute("select count(*) from player_media where usable=1").fetchone()[0],
        "all_media_rows": con.execute("select count(*) from player_media").fetchone()[0],
        "by_sport": [
            dict(row)
            for row in con.execute(
                """
                select sport, count(*) as rows, sum(case when usable=1 then 1 else 0 end) as usable
                from player_media
                group by sport
                order by sport
                """
            )
        ],
        "notes": [
            "Sports Reference-style IDs are used for identity matching only; player photos are not downloaded from Sports Reference.",
            "Wikidata P18 images are treated as Commons candidates and retain Commons license metadata.",
            "Wikipedia pageimages are fallback candidates and are marked usable only when metadata looks freely reusable.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    log(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cached player profile media metadata.")
    parser.add_argument("--sports", default="MLB,NFL,NBA", help="Comma-separated sports to process.")
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--imageinfo-chunk-size", type=int, default=50)
    parser.add_argument("--max-chunks", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--max-imageinfo-chunks", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--media-sports", default="", help="Optional comma-separated sports for image metadata fetching.")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=10)
    parser.add_argument("--rate-limit-sleep", type=float, default=90.0)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--skip-wikipedia-fallback", action="store_true")
    args = parser.parse_args()

    sports = {sport.strip().upper() for sport in args.sports.split(",") if sport.strip()}
    media_sports = {sport.strip().upper() for sport in args.media_sports.split(",") if sport.strip()} or sports
    max_chunks = None if args.max_chunks == 0 else args.max_chunks
    max_imageinfo_chunks = None if args.max_imageinfo_chunks == 0 else args.max_imageinfo_chunks

    con = init_db(args.reset)
    con.row_factory = sqlite3.Row
    rows = load_source_rows(sports)
    insert_source_rows(con, rows)
    if "NBA" in sports:
        imported = import_nba_alltime_image_candidates(con)
        if imported:
            log(f"imported {imported} NBA all-time image candidates from {repo_path(NBA_ALLTIME_DB)}")
    fetch_wikidata_media(
        con,
        rows,
        args.chunk_size,
        max_chunks,
        args.sleep_seconds,
        args.max_retries,
        args.rate_limit_sleep,
    )
    if not args.skip_wikipedia_fallback:
        fetch_wikipedia_pageimages(
            con,
            args.imageinfo_chunk_size,
            max_imageinfo_chunks,
            args.sleep_seconds,
            args.max_retries,
            args.rate_limit_sleep,
        )
    fetch_imageinfo_for_source(
        con,
        api_source="commons",
        sports=media_sports,
        chunk_size=args.imageinfo_chunk_size,
        max_chunks=max_imageinfo_chunks,
        sleep_seconds=args.sleep_seconds,
        max_retries=args.max_retries,
        rate_limit_sleep=args.rate_limit_sleep,
    )
    if not args.skip_wikipedia_fallback:
        fetch_imageinfo_for_source(
            con,
            api_source="wikipedia",
            sports=media_sports,
            chunk_size=args.imageinfo_chunk_size,
            max_chunks=max_imageinfo_chunks,
            sleep_seconds=args.sleep_seconds,
            max_retries=args.max_retries,
            rate_limit_sleep=args.rate_limit_sleep,
        )
    write_summary(con)
    con.close()


if __name__ == "__main__":
    main()
