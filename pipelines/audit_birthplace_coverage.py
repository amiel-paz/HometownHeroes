#!/usr/bin/env python3
"""
Audit missing or conflicting player birth dates and birthplaces.

The audit is read-only against the application database. It writes a scratch
SQLite database containing missing-data candidates, Wikidata structured-data
hits, and cached Wikipedia infobox birth fields that a separate fixer can use
after review.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from enrich_wikidata_birthplace import parse_point, pfr_variants, qid_from_url


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
APP_DB = SCRATCH / "HometownHeroes.sqlite"
OUT_DB = SCRATCH / "birthplace_coverage_audit.sqlite"
SUMMARY_PATH = SCRATCH / "birthplace_coverage_audit_summary.json"
REVIEW_CANDIDATES_PATH = SCRATCH / "birthplace_conflict_review_candidates.json"
RAW_AUDIT = ROOT / "data" / "raw" / "wikidata" / "birthplace_audit"
HONORS_DB = SCRATCH / "player_honors.sqlite"
NFL_WIKIPEDIA_PAGES = ROOT / "data" / "raw" / "wikipedia" / "nfl_honors"

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "HometownHeroesPipeline/0.1 (CC0 structured data; birthplace audit)"

US_STATE_NAMES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
    "dc": "district of columbia",
}


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def chunks(rows: list[str], size: int) -> list[list[str]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def bbr_variants(bbref_id: str) -> list[str]:
    bbref_id = (bbref_id or "").strip()
    if not bbref_id:
        return []
    if "/" in bbref_id:
        return [bbref_id]
    return sorted({bbref_id, f"{bbref_id[0]}/{bbref_id}"})


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


def wikidata_query(prop: str, external_ids: list[str]) -> str:
    values = " ".join(json.dumps(value) for value in external_ids)
    return f"""
SELECT ?external ?person ?personLabel ?birthdate ?birthplace ?birthplaceLabel ?coord ?locatedInLabel ?countryLabel WHERE {{
  VALUES ?external {{ {values} }}
  ?person wdt:{prop} ?external .
  OPTIONAL {{ ?person wdt:P569 ?birthdate . }}
  OPTIONAL {{
    ?person wdt:P19 ?birthplace .
    OPTIONAL {{ ?birthplace wdt:P625 ?coord . }}
    OPTIONAL {{ ?birthplace wdt:P131 ?locatedIn . }}
    OPTIONAL {{ ?birthplace wdt:P17 ?country . }}
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def init_audit_db(path: Path, reset: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    if reset and path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table if not exists candidates (
            sport text not null,
            player_id text not null,
            display_name text,
            primary_external_id text,
            missing_birth_date integer not null,
            missing_birth_year integer not null,
            missing_geocoded_birth integer not null,
            external_property text not null,
            external_id text not null,
            primary key (sport, player_id, external_property, external_id)
        );

        create table if not exists app_birthplaces (
            sport text not null,
            player_id text not null,
            display_name text,
            birth_date text,
            birth_year integer,
            location_id text,
            label text,
            city text,
            state text,
            country text,
            latitude real,
            longitude real,
            source text,
            source_key text,
            primary key (sport, player_id, location_id)
        );

        create table if not exists wikidata_hits (
            sport text not null,
            player_id text not null,
            display_name text,
            external_property text not null,
            external_id text not null,
            wikidata_qid text,
            person_label text,
            birth_date text,
            birthplace_qid text,
            birthplace_label text,
            located_in_label text,
            country_label text,
            latitude real,
            longitude real,
            raw_cache_path text,
            primary key (sport, player_id, external_property, external_id, wikidata_qid, birthplace_qid)
        );

        create table if not exists wikipedia_infobox_birth (
            sport text not null,
            player_id text not null,
            article_title text not null,
            birth_date_raw text,
            birth_place_raw text,
            birth_date_clean text,
            birth_place_clean text,
            raw_cache_path text,
            primary key (sport, player_id, article_title)
        );

        create table if not exists fetch_log (
            external_property text not null,
            chunk_index integer not null,
            cache_path text,
            id_count integer not null,
            hit_count integer not null,
            status text not null,
            fetched_at text default current_timestamp,
            primary key (external_property, chunk_index)
        );

        create table if not exists source_birthplace_conflicts (
            sport text not null,
            player_id text not null,
            display_name text,
            source_name text not null,
            source_value text not null,
            source_key text,
            app_location_id text,
            app_label text,
            app_city text,
            app_state text,
            app_country text,
            app_source text,
            conflict_kind text not null,
            recommendation text not null,
            raw_cache_path text,
            primary key (sport, player_id, source_name, source_value, app_location_id)
        );
        """
    )
    return con


def collect_app_birthplaces(app_con: sqlite3.Connection) -> list[sqlite3.Row]:
    return app_con.execute(
        """
        select
            p.sport,
            p.player_id,
            p.display_name,
            p.birth_date,
            p.birth_year,
            l.location_id,
            l.label,
            l.city,
            l.state,
            l.country,
            l.latitude,
            l.longitude,
            e.source,
            e.source_key
        from players p
        join player_location_events e
          on e.sport = p.sport
         and e.player_id = p.player_id
         and e.event_type = 'born'
        join locations l on l.location_id = e.location_id
        """
    ).fetchall()


def insert_app_birthplaces(con: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    con.executemany(
        """
        insert or replace into app_birthplaces
        (sport, player_id, display_name, birth_date, birth_year, location_id, label, city, state,
         country, latitude, longitude, source, source_key)
        values
        (:sport, :player_id, :display_name, :birth_date, :birth_year, :location_id, :label, :city, :state,
         :country, :latitude, :longitude, :source, :source_key)
        """,
        [dict(row) for row in rows],
    )
    con.commit()


def collect_candidates(app_con: sqlite3.Connection, include_complete_birthplaces: bool = False) -> list[dict[str, object]]:
    rows = app_con.execute(
        """
        with born as (
            select
                e.sport,
                e.player_id,
                max(case when l.latitude is not null and l.longitude is not null then 1 else 0 end) as has_geocoded_birth
            from player_location_events e
            left join locations l using (location_id)
            where e.event_type = 'born'
            group by e.sport, e.player_id
        )
        select
            p.sport,
            p.player_id,
            p.display_name,
            p.birth_date,
            p.birth_year,
            p.primary_external_id,
            case when p.birth_date is null or p.birth_date = '' then 1 else 0 end as missing_birth_date,
            case when p.birth_year is null then 1 else 0 end as missing_birth_year,
            case when coalesce(b.has_geocoded_birth, 0) = 0 then 1 else 0 end as missing_geocoded_birth
        from players p
        left join born b on b.sport = p.sport and b.player_id = p.player_id
        where ? = 1
           or p.birth_date is null
           or p.birth_date = ''
           or p.birth_year is null
           or coalesce(b.has_geocoded_birth, 0) = 0
        """,
        (1 if include_complete_birthplaces else 0,),
    ).fetchall()
    candidates: list[dict[str, object]] = []
    for row in rows:
        sport = row["sport"]
        player_id = row["player_id"]
        external_id = (row["primary_external_id"] or "").strip()
        variants: list[str]
        if sport == "NFL":
            prop = "P3561"
            variants = pfr_variants(external_id)
        elif sport == "NBA" and str(player_id).startswith("bbr:"):
            prop = "P2685"
            variants = bbr_variants(external_id or str(player_id)[4:])
        elif sport == "NBA":
            prop = "P3685"
            variants = [external_id or player_id]
        elif sport == "MLB":
            prop = "P1825"
            variants = bbr_variants(external_id)
        else:
            continue
        for variant in variants:
            candidates.append(
                {
                    "sport": sport,
                    "player_id": player_id,
                    "display_name": row["display_name"],
                    "primary_external_id": external_id,
                    "missing_birth_date": row["missing_birth_date"],
                    "missing_birth_year": row["missing_birth_year"],
                    "missing_geocoded_birth": row["missing_geocoded_birth"],
                    "external_property": prop,
                    "external_id": variant,
                }
            )
    return candidates


def insert_candidates(con: sqlite3.Connection, candidates: list[dict[str, object]]) -> None:
    con.executemany(
        """
        insert or ignore into candidates
        (sport, player_id, display_name, primary_external_id, missing_birth_date,
         missing_birth_year, missing_geocoded_birth, external_property, external_id)
        values
        (:sport, :player_id, :display_name, :primary_external_id, :missing_birth_date,
         :missing_birth_year, :missing_geocoded_birth, :external_property, :external_id)
        """,
        candidates,
    )
    con.commit()


def fetch_wikidata(con: sqlite3.Connection, chunk_size: int, max_chunks: int | None, sleep_seconds: float) -> None:
    RAW_AUDIT.mkdir(parents=True, exist_ok=True)
    props = [row[0] for row in con.execute("select distinct external_property from candidates order by external_property")]
    for prop in props:
        ids = [
            row[0]
            for row in con.execute(
                "select distinct external_id from candidates where external_property = ? and external_id != '' order by external_id",
                (prop,),
            )
        ]
        log(f"{prop}: {len(ids)} external IDs")
        processed = 0
        for chunk_index, batch in enumerate(chunks(ids, chunk_size)):
            if max_chunks is not None and processed >= max_chunks:
                break
            cached = con.execute(
                "select cache_path from fetch_log where external_property = ? and chunk_index = ? and status = 'ok'",
                (prop, chunk_index),
            ).fetchone()
            cache_path = RAW_AUDIT / f"{prop.lower()}_birth_audit_chunk_{chunk_index:05d}.json"
            if cached and cache_path.exists():
                processed += 1
                continue
            if cache_path.exists():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                data = sparql_request(wikidata_query(prop, batch))
                cache_path.write_text(json.dumps(data), encoding="utf-8")
            hit_count = process_wikidata_bindings(con, prop, batch, data, cache_path)
            con.execute(
                """
                insert or replace into fetch_log
                (external_property, chunk_index, cache_path, id_count, hit_count, status)
                values (?, ?, ?, ?, ?, 'ok')
                """,
                (prop, chunk_index, repo_path(cache_path), len(batch), hit_count),
            )
            con.commit()
            log(f"  {prop} chunk {chunk_index}: {len(batch)} ids, {hit_count} hits")
            processed += 1
            time.sleep(sleep_seconds)


def process_wikidata_bindings(con: sqlite3.Connection, prop: str, ids: list[str], data: dict, cache_path: Path) -> int:
    candidates_by_id: dict[str, list[sqlite3.Row]] = {}
    for row in con.execute(
        f"""
        select *
        from candidates
        where external_property = ?
          and external_id in ({",".join("?" for _ in ids)})
        """,
        [prop, *ids],
    ):
        candidates_by_id.setdefault(row["external_id"], []).append(row)

    out = []
    for binding in data.get("results", {}).get("bindings", []):
        external_id = binding.get("external", {}).get("value", "")
        person_qid = qid_from_url(binding.get("person", {}).get("value"))
        birthplace_qid = qid_from_url(binding.get("birthplace", {}).get("value"))
        lat, lon = parse_point(binding.get("coord", {}).get("value"))
        for candidate in candidates_by_id.get(external_id, []):
            out.append(
                {
                    "sport": candidate["sport"],
                    "player_id": candidate["player_id"],
                    "display_name": candidate["display_name"],
                    "external_property": prop,
                    "external_id": external_id,
                    "wikidata_qid": person_qid or "",
                    "person_label": binding.get("personLabel", {}).get("value", ""),
                    "birth_date": binding.get("birthdate", {}).get("value", ""),
                    "birthplace_qid": birthplace_qid or "",
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
        insert or ignore into wikidata_hits
        (sport, player_id, display_name, external_property, external_id, wikidata_qid, person_label,
         birth_date, birthplace_qid, birthplace_label, located_in_label, country_label,
         latitude, longitude, raw_cache_path)
        values
        (:sport, :player_id, :display_name, :external_property, :external_id, :wikidata_qid, :person_label,
         :birth_date, :birthplace_qid, :birthplace_label, :located_in_label, :country_label,
         :latitude, :longitude, :raw_cache_path)
        """,
        out,
    )
    return len(out)


def field(content: str, name: str) -> str:
    match = re.search(
        r"^\|\s*" + re.escape(name) + r"\s*=\s*(.*?)(?=^\|\s*[A-Za-z_ ]+\s*=|\n\}\})",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )
    return " ".join(match.group(1).strip().split()) if match else ""


def clean_wiki_markup(value: str) -> str:
    value = re.sub(r"\{\{Birth date[^{}]*\}\}", "DATE_TEMPLATE", value, flags=re.IGNORECASE)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(value.split())


def normalize_place_text(value: object) -> str:
    text = str(value or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\bft\.?\b", " fort ", text)
    text = re.sub(r"\bst\.?\b", " saint ", text)
    text = re.sub(r"\b(u\.s\.a?|united states(?: of america)?|usa)\b", " united states ", text)
    for abbr, state_name in US_STATE_NAMES.items():
        text = re.sub(rf"(?<=,)\s*{re.escape(abbr.lower())}\.?\s*(?=,|$)", f" {state_name} ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def place_text_variants(value: object) -> set[str]:
    norm = normalize_place_text(value)
    variants = {norm} if norm else set()
    suffixes = (
        " city",
        " town",
        " township",
        " municipality",
        " metropolitan area",
        " metro area",
        " valley",
    )
    for suffix in suffixes:
        if norm.endswith(suffix):
            variants.add(norm[: -len(suffix)].strip())
    if " by the sea" in norm:
        variants.add(norm.replace(" by the sea", "").strip())
    return {variant for variant in variants if variant}


def first_place_part(value: object) -> str:
    return str(value or "").split(",", 1)[0].strip()


def place_texts_agree(source_value: str, app_row: sqlite3.Row) -> bool:
    source_norm = normalize_place_text(source_value)
    if not source_norm:
        return True
    source_variants = place_text_variants(first_place_part(source_value)) | place_text_variants(source_value)
    app_parts = [
        app_row["city"],
        first_place_part(app_row["label"]),
        app_row["label"],
    ]
    for part in app_parts:
        for part_norm in place_text_variants(part):
            if part_norm and (part_norm in source_norm or source_norm in part_norm):
                return True
            for source_variant in source_variants:
                if part_norm and source_variant and (part_norm in source_variant or source_variant in part_norm):
                    return True
    return False


def source_place_text(row: sqlite3.Row) -> str:
    parts = [row["birthplace_label"], row["located_in_label"], row["country_label"]]
    return ", ".join(part for part in parts if part)


def audit_birthplace_conflicts(con: sqlite3.Connection) -> None:
    app_by_player: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in con.execute("select * from app_birthplaces"):
        app_by_player.setdefault((row["sport"], row["player_id"]), []).append(row)

    conflicts: list[dict[str, object]] = []
    for wiki in con.execute("select * from wikipedia_infobox_birth where birth_place_clean != ''"):
        for app in app_by_player.get((wiki["sport"], wiki["player_id"]), []):
            if place_texts_agree(wiki["birth_place_clean"], app):
                continue
            conflicts.append(
                {
                    "sport": wiki["sport"],
                    "player_id": wiki["player_id"],
                    "display_name": app["display_name"],
                    "source_name": "Wikipedia infobox",
                    "source_value": wiki["birth_place_clean"],
                    "source_key": wiki["article_title"],
                    "app_location_id": app["location_id"],
                    "app_label": app["label"],
                    "app_city": app["city"],
                    "app_state": app["state"],
                    "app_country": app["country"],
                    "app_source": app["source"],
                    "conflict_kind": "birthplace_text_disagreement",
                    "recommendation": "review_wikipedia_precedence",
                    "raw_cache_path": wiki["raw_cache_path"],
                }
            )

    for wd in con.execute("select * from wikidata_hits where birthplace_label != ''"):
        wd_value = source_place_text(wd)
        for app in app_by_player.get((wd["sport"], wd["player_id"]), []):
            if place_texts_agree(wd_value, app):
                continue
            conflicts.append(
                {
                    "sport": wd["sport"],
                    "player_id": wd["player_id"],
                    "display_name": app["display_name"] or wd["display_name"],
                    "source_name": "Wikidata P19",
                    "source_value": wd_value,
                    "source_key": wd["wikidata_qid"],
                    "app_location_id": app["location_id"],
                    "app_label": app["label"],
                    "app_city": app["city"],
                    "app_state": app["state"],
                    "app_country": app["country"],
                    "app_source": app["source"],
                    "conflict_kind": "birthplace_text_disagreement",
                    "recommendation": "review_structured_source_conflict",
                    "raw_cache_path": wd["raw_cache_path"],
                }
            )

    con.executemany(
        """
        insert or replace into source_birthplace_conflicts
        (sport, player_id, display_name, source_name, source_value, source_key, app_location_id,
         app_label, app_city, app_state, app_country, app_source, conflict_kind, recommendation, raw_cache_path)
        values
        (:sport, :player_id, :display_name, :source_name, :source_value, :source_key, :app_location_id,
         :app_label, :app_city, :app_state, :app_country, :app_source, :conflict_kind, :recommendation, :raw_cache_path)
        """,
        conflicts,
    )
    con.commit()


def load_wikipedia_pages() -> dict[str, tuple[str, str]]:
    pages: dict[str, tuple[str, str]] = {}
    if not NFL_WIKIPEDIA_PAGES.exists():
        return pages
    for path in sorted(NFL_WIKIPEDIA_PAGES.glob("nfl_honors_pages_chunk_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_pages = data.get("query", {}).get("pages", [])
        if isinstance(raw_pages, dict):
            raw_pages = raw_pages.values()
        for page in raw_pages:
            title = page.get("title")
            revisions = page.get("revisions") or []
            if not title or not revisions:
                continue
            content = revisions[0].get("slots", {}).get("main", {}).get("content", "")
            if content:
                pages[title] = (content, repo_path(path))
    return pages


def audit_cached_wikipedia(con: sqlite3.Connection) -> None:
    if not HONORS_DB.exists():
        return
    pages = load_wikipedia_pages()
    if not pages:
        return
    honors = sqlite3.connect(HONORS_DB)
    honors.row_factory = sqlite3.Row
    rows = honors.execute(
        """
        select source_player_id, article_title
        from nfl_wikipedia_title_map
        where article_title is not null and article_title != ''
        """
    ).fetchall()
    out = []
    for row in rows:
        content_and_path = pages.get(row["article_title"])
        if not content_and_path:
            continue
        content, cache_path = content_and_path
        birth_date_raw = field(content, "birth_date")
        birth_place_raw = field(content, "birth_place")
        if not birth_date_raw and not birth_place_raw:
            continue
        out.append(
            {
                "sport": "NFL",
                "player_id": row["source_player_id"],
                "article_title": row["article_title"],
                "birth_date_raw": birth_date_raw,
                "birth_place_raw": birth_place_raw,
                "birth_date_clean": clean_wiki_markup(birth_date_raw),
                "birth_place_clean": clean_wiki_markup(birth_place_raw),
                "raw_cache_path": cache_path,
            }
        )
    honors.close()
    con.executemany(
        """
        insert or replace into wikipedia_infobox_birth
        (sport, player_id, article_title, birth_date_raw, birth_place_raw,
         birth_date_clean, birth_place_clean, raw_cache_path)
        values
        (:sport, :player_id, :article_title, :birth_date_raw, :birth_place_raw,
         :birth_date_clean, :birth_place_clean, :raw_cache_path)
        """,
        out,
    )
    con.commit()


def write_review_candidates(con: sqlite3.Connection, path: Path) -> list[dict[str, object]]:
    rows = [
        {
            "sport": row["sport"],
            "player_id": row["player_id"],
            "display_name": row["display_name"],
            "source_name": row["source_name"],
            "source_value": row["source_value"],
            "source_key": row["source_key"],
            "current_app_birthplace": row["app_label"],
            "current_app_source": row["app_source"],
            "recommendation": row["recommendation"],
            "raw_cache_path": row["raw_cache_path"],
            "needs_review": True,
        }
        for row in con.execute(
            """
            select *
            from source_birthplace_conflicts
            order by
                case source_name when 'Wikipedia infobox' then 0 else 1 end,
                sport,
                display_name,
                player_id
            """
        )
    ]
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return rows


def write_summary(
    con: sqlite3.Connection, path: Path, audit_db_path: Path, review_candidates_path: Path
) -> dict[str, object]:
    summary = {
        "audit_database": repo_path(audit_db_path),
        "review_candidates": repo_path(review_candidates_path),
        "candidates_by_sport": dict(
            con.execute("select sport, count(distinct player_id) from candidates group by sport order by sport").fetchall()
        ),
        "app_birthplaces_by_sport": dict(
            con.execute("select sport, count(distinct player_id) from app_birthplaces group by sport order by sport").fetchall()
        ),
        "birthplace_conflicts_by_source": {
            f"{row['source_name']}|{row['recommendation']}": row["players"]
            for row in con.execute(
                """
                select source_name, recommendation, count(distinct sport || ':' || player_id) as players
                from source_birthplace_conflicts
                group by source_name, recommendation
                order by source_name, recommendation
                """
            )
        },
        "wikidata_fill_potential": {},
        "wikipedia_nfl_cached_infobox_birthplaces": con.execute(
            """
            select count(distinct w.player_id)
            from wikipedia_infobox_birth w
            join candidates c on c.sport = w.sport and c.player_id = w.player_id
            where c.missing_geocoded_birth = 1
              and w.birth_place_clean is not null
              and w.birth_place_clean != ''
            """
        ).fetchone()[0],
    }
    for row in con.execute(
        """
        select
            c.sport,
            count(distinct case when c.missing_birth_date = 1 then c.player_id end) as missing_birth_date_candidates,
            count(distinct case when c.missing_birth_date = 1 and h.birth_date != '' then c.player_id end) as fillable_birth_date,
            count(distinct case when c.missing_geocoded_birth = 1 then c.player_id end) as missing_geocoded_birth_candidates,
            count(distinct case
                when c.missing_geocoded_birth = 1
                 and h.birthplace_qid != ''
                 and h.latitude is not null
                 and h.longitude is not null
                then c.player_id end
            ) as fillable_geocoded_birthplace
        from candidates c
        left join wikidata_hits h
          on h.sport = c.sport
         and h.player_id = c.player_id
         and h.external_property = c.external_property
         and h.external_id = c.external_id
        group by c.sport
        order by c.sport
        """
    ):
        summary["wikidata_fill_potential"][row["sport"]] = {
            "missing_birth_date_candidates": row["missing_birth_date_candidates"],
            "fillable_birth_date": row["fillable_birth_date"],
            "missing_geocoded_birth_candidates": row["missing_geocoded_birth_candidates"],
            "fillable_geocoded_birthplace": row["fillable_geocoded_birthplace"],
        }
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, default=APP_DB)
    parser.add_argument("--out-db", type=Path, default=OUT_DB)
    parser.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    parser.add_argument("--review-candidates", type=Path, default=REVIEW_CANDIDATES_PATH)
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--max-chunks", type=int, default=0, help="Chunks per Wikidata property; 0 means all")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--skip-wikidata", action="store_true")
    parser.add_argument(
        "--include-complete-birthplaces",
        action="store_true",
        help="Also fetch Wikidata for players that already have birthplaces so conflicts can be audited.",
    )
    args = parser.parse_args()

    if not args.app_db.exists():
        raise SystemExit(f"Missing app database: {args.app_db}")

    con = init_audit_db(args.out_db)
    con.row_factory = sqlite3.Row
    app_con = sqlite3.connect(args.app_db)
    app_con.row_factory = sqlite3.Row
    insert_app_birthplaces(con, collect_app_birthplaces(app_con))
    candidates = collect_candidates(app_con, include_complete_birthplaces=args.include_complete_birthplaces)
    app_con.close()
    insert_candidates(con, candidates)
    log(f"candidate external IDs: {len(candidates)}")

    if not args.skip_wikidata:
        fetch_wikidata(con, args.chunk_size, None if args.max_chunks == 0 else args.max_chunks, args.sleep_seconds)
    audit_cached_wikipedia(con)
    audit_birthplace_conflicts(con)
    review_candidates = write_review_candidates(con, args.review_candidates)
    log(f"birthplace conflict review candidates: {len(review_candidates)}")
    summary = write_summary(con, args.summary, args.out_db, args.review_candidates)
    con.close()
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
