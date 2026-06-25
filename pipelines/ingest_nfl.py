#!/usr/bin/env python3
"""
Build the NFL geography enrichment cache.

The pipeline reads nflverse and College Scorecard source snapshots, then
optionally uses CollegeFootballData when CFBD_API_KEY is available.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_EDU = ROOT / "data/raw/education/college-scorecard-most-recent-2026-06-10"
RAW_CFBD = ROOT / "data/raw/cfbd"
NFL_PLAYERS_CSV = ROOT / "data/raw/nfl/nflverse-players-2026-06-24/players.csv"
DB_PATH = SCRATCH / "nfl_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "nfl_enrichment_summary.json"

SCORECARD_ZIP_URL = "https://ed-public-download.scorecard.network/downloads/Most-Recent-Cohorts-Institution_06102026.zip"
SCORECARD_ZIP = RAW_EDU / "Most-Recent-Cohorts-Institution_06102026.zip"

CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_ROSTER_YEARS = range(2014, 2026)
CFBD_RECRUITING_YEARS = range(2014, 2026)
CFBD_RECRUITING_CLASSES = ("HighSchool", "JUCO", "PrepSchool")


STATE_WORDS = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}

COLLEGE_ALIASES = {
    "usc": "university of southern california",
    "ucla": "university of california los angeles",
    "lsu": "louisiana state university and agricultural mechanical college",
    "tcu": "texas christian university",
    "smu": "southern methodist university",
    "byu": "brigham young university",
    "ucf": "university of central florida",
    "utep": "the university of texas at el paso",
    "utsa": "the university of texas at san antonio",
    "unc": "university of north carolina at chapel hill",
    "north carolina": "university of north carolina at chapel hill",
    "ole miss": "university of mississippi",
    "miami": "university of miami",
    "pittsburgh": "university of pittsburgh pittsburgh campus",
    "california": "university of california berkeley",
    "texas": "the university of texas at austin",
    "georgia": "university of georgia",
    "florida": "university of florida",
    "washington": "university of washington seattle campus",
    "oregon": "university of oregon",
    "maryland": "university of maryland college park",
    "wisconsin": "university of wisconsin madison",
    "michigan": "university of michigan ann arbor",
    "virginia": "university of virginia main campus",
    "illinois": "university of illinois urbana champaign",
    "louisville": "university of louisville",
    "cincinnati": "university of cincinnati main campus",
    "houston": "university of houston",
    "memphis": "university of memphis",
    "toledo": "university of toledo",
    "tulane": "tulane university of louisiana",
    "buffalo": "university at buffalo",
    "nevada": "university of nevada reno",
    "unlv": "university of nevada las vegas",
    "penn state": "the pennsylvania state university",
    "nebraska": "university of nebraska lincoln",
    "oklahoma": "university of oklahoma norman campus",
    "tennessee": "the university of tennessee knoxville",
    "texas a m": "texas a and m university college station",
    "texas a and m": "texas a and m university college station",
    "arizona state": "arizona state university campus immersion",
    "virginia tech": "virginia polytechnic institute and state university",
    "south carolina": "university of south carolina columbia",
    "missouri": "university of missouri columbia",
    "georgia tech": "georgia institute of technology main campus",
    "minnesota": "university of minnesota twin cities",
    "rutgers": "rutgers university new brunswick",
    "indiana": "indiana university bloomington",
    "fresno state": "california state university fresno",
    "colorado state": "colorado state university fort collins",
    "hawaii": "university of hawaii at manoa",
    "miami ohio": "miami university oxford",
    "florida a m": "florida agricultural and mechanical university",
    "florida and m": "florida agricultural and mechanical university",
    "bowling green": "bowling green state university main campus",
    "kent state": "kent state university at kent",
    "uab": "university of alabama at birmingham",
    "umass amherst": "university of massachusetts amherst",
    "uc davis": "university of california davis",
    "sacramento state": "california state university sacramento",
    "air force": "united states air force academy",
    "navy": "united states naval academy",
    "army": "united states military academy",
    "n c state": "north carolina state university at raleigh",
    "nc state": "north carolina state university at raleigh",
    "southern": "southern university and a and m college",
    "northwestern state la": "northwestern state university of louisiana",
    "cal poly san luis obispo": "california polytechnic state university san luis obispo",
    "texas state san marcos": "texas state university",
    "grand valley": "grand valley state university",
    "penn": "university of pennsylvania",
    "texas a m commerce": "texas a and m university commerce",
    "texas a and m commerce": "texas a and m university commerce",
    "missouri state": "missouri state university springfield",
    "minnesota state": "minnesota state university mankato",
    "the citadel": "citadel military college of south carolina",
    "charlotte": "university of north carolina at charlotte",
}


def log(message: str) -> None:
    print(message, flush=True)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"using cached download: {dest}")
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "HometownHeroesPipeline/0.1"})
    with urllib.request.urlopen(req, timeout=120) as response, tmp.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.rename(dest)


def norm(value: str | None) -> str:
    value = (value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(the|at|main|campus|university|college|of)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def norm_for_alias(value: str | None) -> str:
    value = (value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def load_scorecard_institutions() -> list[dict[str, str]]:
    download(SCORECARD_ZIP_URL, SCORECARD_ZIP)
    with zipfile.ZipFile(SCORECARD_ZIP) as zf:
        csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("College Scorecard zip contained no CSV")
        name = csv_names[0]
        log(f"reading scorecard csv: {name}")
        with zf.open(name) as raw:
            text = (line.decode("utf-8", "replace") for line in raw)
            reader = csv.DictReader(text)
            rows = []
            for row in reader:
                if row.get("INSTNM") and row.get("LATITUDE") and row.get("LONGITUDE"):
                    rows.append(
                        {
                            "unitid": row.get("UNITID", ""),
                            "name": row.get("INSTNM", ""),
                            "city": row.get("CITY", ""),
                            "state": row.get("STABBR", ""),
                            "latitude": row.get("LATITUDE", ""),
                            "longitude": row.get("LONGITUDE", ""),
                            "preddeg": row.get("PREDDEG", ""),
                            "control": row.get("CONTROL", ""),
                            "institution_norm": norm(row.get("INSTNM", "")),
                            "institution_alias_norm": norm_for_alias(row.get("INSTNM", "")),
                        }
                    )
            return rows


def load_nfl_colleges() -> tuple[list[dict[str, str]], Counter[str]]:
    with NFL_PLAYERS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    counts = Counter(row.get("college_name", "").strip() for row in rows if row.get("college_name", "").strip())
    return rows, counts


def make_candidates(institutions: list[dict[str, str]]) -> dict[str, object]:
    by_token: dict[str, list[dict[str, str]]] = {}
    exact: dict[str, dict[str, str]] = {}
    stop_tokens = {"state", "st", "univ", "university", "college", "school", "community"}
    for inst in institutions:
        keys = {inst["institution_norm"], inst["institution_alias_norm"]}
        for key in keys:
            if not key:
                continue
            exact.setdefault(inst["institution_alias_norm"], inst)
            for token in key.split():
                if token not in stop_tokens and len(token) > 1:
                    by_token.setdefault(token, []).append(inst)
    return {"by_token": by_token, "exact": exact}


def match_college(college_name: str, institutions: list[dict[str, str]], candidates: dict[str, object]) -> dict[str, str | float | None]:
    alias = COLLEGE_ALIASES.get(norm_for_alias(college_name))
    query = alias or college_name
    qn = norm(query)
    qa = norm_for_alias(query)
    best = None
    best_score = -1.0

    exact = candidates["exact"]
    if qa in exact:
        best = exact[qa]
        best_score = 1.0
    else:
        by_token = candidates["by_token"]
        pool_by_unitid = {}
        query_tokens = [token for token in set(qn.split() + qa.split()) if len(token) > 1]
        for token in query_tokens:
            for inst in by_token.get(token, []):
                pool_by_unitid[inst["unitid"]] = inst
        pool = list(pool_by_unitid.values())

        for inst in pool:
            score = max(ratio(qn, inst["institution_norm"]), ratio(qa, inst["institution_alias_norm"]))
            if score > best_score:
                best_score = score
                best = inst

    matched = best_score >= 0.88
    return {
        "college_name": college_name,
        "query_used": query,
        "match_status": "matched" if matched else "unresolved",
        "match_score": round(best_score, 4) if best_score >= 0 else None,
        "unitid": best["unitid"] if matched and best else None,
        "institution_name": best["name"] if matched and best else None,
        "city": best["city"] if matched and best else None,
        "state": best["state"] if matched and best else None,
        "latitude": best["latitude"] if matched and best else None,
        "longitude": best["longitude"] if matched and best else None,
        "player_count": None,
    }


def create_sql_cache(institutions: list[dict[str, str]], nfl_rows: list[dict[str, str]], college_counts: Counter[str], matches: list[dict[str, str | float | None]]) -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("pragma journal_mode=wal")
    con.execute(
        """
        create table scorecard_institutions (
            unitid text,
            name text,
            city text,
            state text,
            latitude real,
            longitude real,
            preddeg text,
            control text,
            institution_norm text,
            institution_alias_norm text
        )
        """
    )
    con.executemany(
        "insert into scorecard_institutions values (:unitid,:name,:city,:state,:latitude,:longitude,:preddeg,:control,:institution_norm,:institution_alias_norm)",
        institutions,
    )
    con.execute(
        """
        create table nfl_players (
            gsis_id text,
            display_name text,
            pfr_id text,
            birth_date text,
            college_name text,
            rookie_season text,
            last_season text,
            latest_team text
        )
        """
    )
    con.executemany(
        """
        insert into nfl_players values (:gsis_id,:display_name,:pfr_id,:birth_date,:college_name,:rookie_season,:last_season,:latest_team)
        """,
        [
            {
                "gsis_id": row.get("gsis_id", ""),
                "display_name": row.get("display_name", ""),
                "pfr_id": row.get("pfr_id", ""),
                "birth_date": row.get("birth_date", ""),
                "college_name": row.get("college_name", ""),
                "rookie_season": row.get("rookie_season", ""),
                "last_season": row.get("last_season", ""),
                "latest_team": row.get("latest_team", ""),
            }
            for row in nfl_rows
        ],
    )
    con.execute(
        """
        create table nfl_college_geocode_cache (
            college_name text primary key,
            query_used text,
            match_status text,
            match_score real,
            unitid text,
            institution_name text,
            city text,
            state text,
            latitude real,
            longitude real,
            player_count integer
        )
        """
    )
    for row in matches:
        row["player_count"] = college_counts[row["college_name"]]
    con.executemany(
        """
        insert into nfl_college_geocode_cache values (
            :college_name,:query_used,:match_status,:match_score,:unitid,:institution_name,:city,:state,:latitude,:longitude,:player_count
        )
        """,
        matches,
    )
    con.execute(
        """
        create view nfl_players_with_college_location as
        select
            p.*,
            c.match_status,
            c.match_score,
            c.unitid,
            c.institution_name,
            c.city as college_city,
            c.state as college_state,
            c.latitude as college_latitude,
            c.longitude as college_longitude
        from nfl_players p
        left join nfl_college_geocode_cache c
          on p.college_name = c.college_name
        """
    )
    con.execute("create index idx_nfl_college_status on nfl_college_geocode_cache(match_status)")
    con.execute("create index idx_nfl_players_college on nfl_players(college_name)")
    con.commit()
    con.close()


def cfbd_request(path: str, params: dict[str, str], api_key: str) -> object:
    query = urllib.parse.urlencode(params)
    url = f"{CFBD_BASE}{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "HometownHeroesPipeline/0.1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def maybe_fetch_cfbd() -> dict[str, int | str]:
    api_key = os.environ.get("CFBD_API_KEY", "").strip()
    RAW_CFBD.mkdir(parents=True, exist_ok=True)
    if not api_key:
        return {"status": "skipped_missing_CFBD_API_KEY"}

    fetched_rosters = 0
    fetched_recruiting = 0
    for year in CFBD_ROSTER_YEARS:
        dest = RAW_CFBD / "rosters" / f"roster_{year}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            data = cfbd_request("/roster", {"year": str(year)}, api_key)
            dest.write_text(json.dumps(data))
            fetched_rosters += 1
            time.sleep(0.25)
    for year in CFBD_RECRUITING_YEARS:
        for classification in CFBD_RECRUITING_CLASSES:
            dest = RAW_CFBD / "recruiting" / f"recruiting_{classification}_{year}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                data = cfbd_request("/recruiting/players", {"year": str(year), "classification": classification}, api_key)
                dest.write_text(json.dumps(data))
                fetched_recruiting += 1
                time.sleep(0.25)
    return {
        "status": "ok",
        "fetched_roster_files": fetched_rosters,
        "fetched_recruiting_files": fetched_recruiting,
    }


def main() -> None:
    SCRATCH.mkdir(exist_ok=True)
    institutions = load_scorecard_institutions()
    nfl_rows, college_counts = load_nfl_colleges()
    candidates = make_candidates(institutions)
    matches = [match_college(name, institutions, candidates) for name in sorted(college_counts)]
    create_sql_cache(institutions, nfl_rows, college_counts, matches)
    cfbd_summary = maybe_fetch_cfbd()

    matched_colleges = [row for row in matches if row["match_status"] == "matched"]
    matched_player_count = sum(college_counts[row["college_name"]] for row in matched_colleges)
    summary = {
        "scorecard_source": SCORECARD_ZIP_URL,
        "scorecard_institutions_with_lat_lon": len(institutions),
        "nfl_players": len(nfl_rows),
        "nfl_unique_college_names": len(college_counts),
        "matched_unique_college_names": len(matched_colleges),
        "matched_unique_college_pct": round(100 * len(matched_colleges) / len(college_counts), 1),
        "matched_player_count": matched_player_count,
        "matched_player_pct": round(100 * matched_player_count / sum(college_counts.values()), 1),
        "sqlite_cache": str(DB_PATH),
        "cfbd": cfbd_summary,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
