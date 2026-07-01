#!/usr/bin/env python3
"""
Build the MLB geography enrichment cache.

The pipeline reads Lahman, Chadwick Register, College Scorecard, and Census
Gazetteer source snapshots and writes derived SQLite/CSV outputs under scratch/.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import unicodedata
import urllib.request
import warnings
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
BASE = ROOT / "data/raw/mlb/lahman-cran-14.0-0/extracted/Lahman/data"
CHADWICK_DATA = ROOT / "data/raw/mlb/chadwick-register-master/extracted/register-master/data"
RAW_EDU = ROOT / "data/raw/education/college-scorecard-most-recent-2026-06-10"
DB_PATH = SCRATCH / "mlb_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "mlb_enrichment_summary.json"

SCORECARD_ZIP_URL = "https://ed-public-download.scorecard.network/downloads/Most-Recent-Cohorts-Institution_06102026.zip"
SCORECARD_ZIP = RAW_EDU / "Most-Recent-Cohorts-Institution_06102026.zip"
GAZETTEER_ZIP_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip"
GAZETTEER_ZIP = ROOT / "data/raw/geography/census-gazetteer-2025/2025_Gaz_place_national.zip"
GEONAMES_DIR = ROOT / "data/raw/geography/geonames"
GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities500.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
GEONAMES_COUNTRY_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
GEONAMES_CITIES_ZIP = GEONAMES_DIR / "cities500.zip"
GEONAMES_ADMIN1 = GEONAMES_DIR / "admin1CodesASCII.txt"
GEONAMES_COUNTRY = GEONAMES_DIR / "countryInfo.txt"


warnings.filterwarnings("ignore")
import pandas as pd
import rdata


SCORECARD_ALIASES = {
    "usc": "university of southern california",
    "ucla": "university of california los angeles",
    "lsu": "louisiana state university and agricultural mechanical college",
    "california": "university of california berkeley",
    "cal": "university of california berkeley",
    "texas": "the university of texas at austin",
    "stanford": "stanford university",
    "miamifl": "university of miami",
    "miami fl": "university of miami",
    "floridast": "florida state university",
    "florida state": "florida state university",
    "arizonast": "arizona state university campus immersion",
    "arizona state": "arizona state university campus immersion",
    "calstfull": "california state university fullerton",
    "cal state fullerton": "california state university fullerton",
    "unc": "university of north carolina at chapel hill",
    "north carolina": "university of north carolina at chapel hill",
    "notredame": "university of notre dame",
    "holy cross": "college of the holy cross",
    "holycross": "college of the holy cross",
    "okstate": "oklahoma state university main campus",
    "oklahoma state": "oklahoma state university main campus",
}

COUNTRY_ALIASES = {
    "can": "CA",
    "d r": "DO",
    "dr": "DO",
    "dominican republic": "DO",
    "mexico": "MX",
    "mexico": "MX",
    "méxico": "MX",
    "p r": "PR",
    "pr": "PR",
    "pri": "PR",
    "puerto rico": "PR",
    "usa": "US",
    "us": "US",
    "united states": "US",
    "u s a": "US",
    "u s": "US",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "west germany": "DE",
    "south korea": "KR",
    "korea": "KR",
    "curacao": "CW",
    "curaçao": "CW",
    "u s virgin islands": "VI",
    "us virgin islands": "VI",
    "virgin islands": "VI",
}

PUERTO_RICO_CENTROID = {
    "status": "matched",
    "latitude": 18.2208,
    "longitude": -66.5901,
    "source_label": "Puerto Rico",
    "source": "Puerto Rico island centroid fallback",
    "geonameid": None,
}

US_VIRGIN_ISLANDS_CENTROID = {
    "status": "matched",
    "latitude": 18.3358,
    "longitude": -64.8963,
    "source_label": "U.S. Virgin Islands",
    "source": "U.S. Virgin Islands territory centroid fallback",
    "geonameid": None,
}

ST_THOMAS_CENTROID = {
    "status": "matched",
    "latitude": 18.3381,
    "longitude": -64.8941,
    "source_label": "St. Thomas, U.S. Virgin Islands",
    "source": "U.S. Virgin Islands island centroid fallback",
    "geonameid": None,
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


def load_rdata(name: str) -> pd.DataFrame:
    obj = rdata.conversion.convert(rdata.parser.parse_file(BASE / f"{name}.RData"))
    return obj[name]


def norm(value: str | None) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(the|at|main|campus|university|college|of)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def alias_norm(value: str | None) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def clean(value: object) -> object:
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


def load_scorecard_institutions() -> list[dict[str, str]]:
    download(SCORECARD_ZIP_URL, SCORECARD_ZIP)
    with zipfile.ZipFile(SCORECARD_ZIP) as zf:
        csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        with zf.open(csv_names[0]) as raw:
            text = (line.decode("utf-8", "replace") for line in raw)
            reader = csv.DictReader(text)
            rows = []
            for row in reader:
                if row.get("INSTNM") and row.get("LATITUDE") and row.get("LONGITUDE"):
                    rows.append(
                        {
                            "unitid": row.get("UNITID", ""),
                            "institution_name": row.get("INSTNM", ""),
                            "city": row.get("CITY", ""),
                            "state": row.get("STABBR", ""),
                            "latitude": row.get("LATITUDE", ""),
                            "longitude": row.get("LONGITUDE", ""),
                            "institution_norm": norm(row.get("INSTNM", "")),
                            "institution_alias_norm": alias_norm(row.get("INSTNM", "")),
                        }
                    )
            return rows


def place_name_norm(name: str | None) -> str:
    name = " ".join((name or "").strip().split())
    for suffix in (" city", " town", " village", " borough", " municipality", " township", " CDP"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower()


def ascii_norm(value: str | None) -> str:
    try:
        if pd.isna(value):
            value = ""
    except TypeError:
        pass
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def load_census_places() -> dict[tuple[str, str], dict[str, str | float]]:
    download(GAZETTEER_ZIP_URL, GAZETTEER_ZIP)
    gaz: dict[tuple[str, str], dict[str, str | float]] = {}
    with zipfile.ZipFile(GAZETTEER_ZIP) as zf:
        with zf.open("2025_Gaz_place_national.txt") as raw:
            text = (line.decode("utf-8") for line in raw)
            reader = csv.DictReader(text, delimiter="|")
            for row in reader:
                state = row["USPS"].upper()
                key = (state, place_name_norm(row["NAME"]))
                gaz.setdefault(
                    key,
                    {
                        "city": place_name_norm(row["NAME"]),
                        "state": state,
                        "latitude": float(row["INTPTLAT"]),
                        "longitude": float(row["INTPTLONG"]),
                        "source_label": row["NAME"],
                    },
                )
    gaz[("CA", "san francisco")] = {
        "city": "san francisco",
        "state": "CA",
        "latitude": 37.7749,
        "longitude": -122.4194,
        "source_label": "San Francisco, CA",
    }
    return gaz


def load_geonames_countries() -> dict[str, dict[str, str]]:
    download(GEONAMES_COUNTRY_URL, GEONAMES_COUNTRY)
    countries: dict[str, dict[str, str]] = {}
    with GEONAMES_COUNTRY.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            iso, iso3, fips, country = parts[0], parts[1], parts[3], parts[4]
            countries[iso] = {"iso": iso, "iso3": iso3, "fips": fips, "country": country}
    return countries


def country_to_iso(country: str | None, countries: dict[str, dict[str, str]]) -> str | None:
    key = ascii_norm(country)
    if not key:
        return None
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    for iso, row in countries.items():
        if key in {ascii_norm(row["country"]), ascii_norm(row["iso"]), ascii_norm(row["iso3"]), ascii_norm(row["fips"])}:
            return iso
    return None


def is_puerto_rico(country: str | None, state: str | None = None) -> bool:
    aliases = {"p r", "pr", "pri", "puerto rico"}
    return ascii_norm(country) in aliases or ascii_norm(state) in aliases


def is_us_virgin_islands(country: str | None, state: str | None = None) -> bool:
    aliases = {"u s virgin islands", "us virgin islands", "united states virgin islands", "virgin islands", "vi"}
    return ascii_norm(country) in aliases or ascii_norm(state) in aliases


def us_virgin_islands_fallback(city: str | None, state: str | None, country: str | None) -> dict[str, object] | None:
    if not is_us_virgin_islands(country, state):
        return None
    place_key = " ".join(part for part in (ascii_norm(city), ascii_norm(state)) if part)
    if "st thomas" in place_key or "saint thomas" in place_key:
        return ST_THOMAS_CENTROID
    return US_VIRGIN_ISLANDS_CENTROID


def load_geonames_admin1() -> dict[tuple[str, str], set[str]]:
    download(GEONAMES_ADMIN1_URL, GEONAMES_ADMIN1)
    admin_names: dict[tuple[str, str], set[str]] = {}
    with GEONAMES_ADMIN1.open(encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3 or "." not in parts[0]:
                continue
            country, admin1 = parts[0].split(".", 1)
            names = {ascii_norm(parts[1]), ascii_norm(parts[2]), ascii_norm(admin1)}
            admin_names[(country, admin1)] = {name for name in names if name}
    return admin_names


def load_geonames_cities() -> tuple[dict[tuple[str, str], list[dict[str, object]]], dict[tuple[str, str], set[str]], dict[str, dict[str, str]]]:
    download(GEONAMES_CITIES_URL, GEONAMES_CITIES_ZIP)
    countries = load_geonames_countries()
    admin_names = load_geonames_admin1()
    index: dict[tuple[str, str], list[dict[str, object]]] = {}
    with zipfile.ZipFile(GEONAMES_CITIES_ZIP) as zf:
        with zf.open("cities500.txt") as raw:
            for raw_line in raw:
                parts = raw_line.decode("utf-8", "replace").rstrip("\n").split("\t")
                if len(parts) < 19:
                    continue
                country = parts[8]
                admin1 = parts[10]
                names = {ascii_norm(parts[1]), ascii_norm(parts[2])}
                names.update(ascii_norm(name) for name in parts[3].split(",") if name)
                names = {name for name in names if name}
                row = {
                    "geonameid": parts[0],
                    "name": parts[1],
                    "asciiname": parts[2],
                    "country": country,
                    "country_name": countries.get(country, {}).get("country", country),
                    "admin1": admin1,
                    "admin1_names": admin_names.get((country, admin1), set()),
                    "latitude": float(parts[4]),
                    "longitude": float(parts[5]),
                    "population": int(parts[14] or 0),
                }
                for name in names:
                    index.setdefault((country, name), []).append(row)
    for candidates in index.values():
        candidates.sort(key=lambda row: int(row["population"]), reverse=True)
    return index, admin_names, countries


def resolve_global_city(
    geonames: dict[tuple[str, str], list[dict[str, object]]],
    countries: dict[str, dict[str, str]],
    city: str | None,
    state: str | None,
    country: str | None,
) -> dict[str, object]:
    iso = country_to_iso(country, countries)
    city_key = ascii_norm(city)
    if not iso or not city_key:
        return {"status": "unresolved", "latitude": None, "longitude": None, "source_label": None, "source": None, "geonameid": None}
    candidates = geonames.get((iso, city_key), [])
    if not candidates:
        return {"status": "unresolved", "latitude": None, "longitude": None, "source_label": None, "source": None, "geonameid": None}

    state_key = ascii_norm(state)
    matched = None
    if state_key:
        for row in candidates:
            admin_names = row["admin1_names"]
            if state_key in admin_names or state_key == ascii_norm(row["admin1"]):
                matched = row
                break
    if matched is None:
        matched = candidates[0]

    label_parts = [str(matched["name"])]
    if matched["admin1_names"]:
        label_parts.append(sorted(matched["admin1_names"], key=len, reverse=True)[0].title())
    label_parts.append(str(matched["country_name"]))
    return {
        "status": "matched",
        "latitude": matched["latitude"],
        "longitude": matched["longitude"],
        "source_label": ", ".join(label_parts),
        "source": "GeoNames cities500 city centroid",
        "geonameid": matched["geonameid"],
    }


def make_institution_index(institutions: list[dict[str, str]]) -> dict[str, object]:
    exact: dict[str, dict[str, str]] = {}
    by_token: dict[str, list[dict[str, str]]] = {}
    stop = {"state", "st", "univ", "university", "college", "school", "community"}
    for inst in institutions:
        exact.setdefault(inst["institution_alias_norm"], inst)
        for key in (inst["institution_norm"], inst["institution_alias_norm"]):
            for token in key.split():
                if len(token) > 1 and token not in stop:
                    by_token.setdefault(token, []).append(inst)
    return {"exact": exact, "by_token": by_token}


def match_school_to_scorecard(school: dict[str, str], index: dict[str, object]) -> dict[str, object]:
    query = SCORECARD_ALIASES.get(alias_norm(school["schoolID"])) or SCORECARD_ALIASES.get(alias_norm(school["name_full"])) or school["name_full"]
    qn = norm(query)
    qa = alias_norm(query)
    exact = index["exact"]
    best = None
    best_score = -1.0
    if qa in exact:
        best = exact[qa]
        best_score = 1.0
    else:
        by_token = index["by_token"]
        pool = {}
        for token in set(qn.split() + qa.split()):
            for inst in by_token.get(token, []):
                if not school.get("state") or inst["state"] == school["state"]:
                    pool[inst["unitid"]] = inst
        for inst in pool.values():
            score = max(ratio(qn, inst["institution_norm"]), ratio(qa, inst["institution_alias_norm"]))
            if score > best_score:
                best_score = score
                best = inst
    matched = best is not None and best_score >= 0.88
    return {
        "schoolID": school["schoolID"],
        "lahman_name": school["name_full"],
        "lahman_city": school["city"],
        "lahman_state": school["state"],
        "lahman_country": school["country"],
        "query_used": query,
        "scorecard_match_status": "matched" if matched else "unresolved",
        "scorecard_match_score": round(best_score, 4) if best_score >= 0 else None,
        "unitid": best["unitid"] if matched else None,
        "institution_name": best["institution_name"] if matched else None,
        "scorecard_city": best["city"] if matched else None,
        "scorecard_state": best["state"] if matched else None,
        "scorecard_latitude": best["latitude"] if matched else None,
        "scorecard_longitude": best["longitude"] if matched else None,
    }


def resolve_city(gaz: dict[tuple[str, str], dict[str, str | float]], city: str | None, state: str | None, country: str | None) -> dict[str, object]:
    if not city or not state or (country or "").upper() not in {"USA", "US"}:
        return {"status": "unresolved", "latitude": None, "longitude": None, "source_label": None}
    row = gaz.get((state.upper(), place_name_norm(city)))
    if not row:
        return {"status": "unresolved", "latitude": None, "longitude": None, "source_label": None}
    return {
        "status": "matched",
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "source_label": row["source_label"],
    }


def read_chadwick_people() -> list[dict[str, str]]:
    rows = []
    for path in sorted(CHADWICK_DATA.glob("people-*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("key_bbref"):
                    rows.append(
                        {
                            "key_person": row.get("key_person", ""),
                            "key_uuid": row.get("key_uuid", ""),
                            "key_mlbam": row.get("key_mlbam", ""),
                            "key_retro": row.get("key_retro", ""),
                            "key_bbref": row.get("key_bbref", ""),
                            "key_bbref_minors": row.get("key_bbref_minors", ""),
                            "key_fangraphs": row.get("key_fangraphs", ""),
                            "key_wikidata": row.get("key_wikidata", ""),
                            "pro_played_first": row.get("pro_played_first", ""),
                            "pro_played_last": row.get("pro_played_last", ""),
                            "mlb_played_first": row.get("mlb_played_first", ""),
                            "mlb_played_last": row.get("mlb_played_last", ""),
                            "col_played_first": row.get("col_played_first", ""),
                            "col_played_last": row.get("col_played_last", ""),
                        }
                    )
    return rows


def main() -> None:
    SCRATCH.mkdir(exist_ok=True)
    log("loading Lahman tables")
    people = load_rdata("People")
    schools = load_rdata("Schools")
    college = load_rdata("CollegePlaying")
    parks = load_rdata("Parks")
    homegames = load_rdata("HomeGames")
    appearances = load_rdata("Appearances")
    teams = load_rdata("Teams")
    institutions = load_scorecard_institutions()
    census_places = load_census_places()
    geonames_cities, _geonames_admin1, geonames_countries = load_geonames_cities()
    school_counts = Counter(college["schoolID"].dropna().astype(str))

    index = make_institution_index(institutions)
    school_records = [dict(row) for _, row in schools.iterrows()]
    school_matches = [match_school_to_scorecard(row, index) for row in school_records]
    for row in school_matches:
        fallback = resolve_city(census_places, row["lahman_city"], row["lahman_state"], row["lahman_country"])
        row["city_geocode_status"] = fallback["status"]
        row["city_latitude"] = fallback["latitude"]
        row["city_longitude"] = fallback["longitude"]
        row["city_geocode_label"] = fallback["source_label"]
        if row["scorecard_match_status"] == "matched":
            row["resolved_latitude"] = row["scorecard_latitude"]
            row["resolved_longitude"] = row["scorecard_longitude"]
            row["resolved_source"] = "College Scorecard institution"
        elif fallback["status"] == "matched":
            row["resolved_latitude"] = fallback["latitude"]
            row["resolved_longitude"] = fallback["longitude"]
            row["resolved_source"] = "Census Gazetteer city centroid"
        else:
            row["resolved_latitude"] = None
            row["resolved_longitude"] = None
            row["resolved_source"] = None
        row["player_year_rows"] = school_counts[row["schoolID"]]

    birth_locations = []
    people_with_birth_city = people.dropna(subset=["birthCity"]).copy()
    puerto_rico_country_only = people[
        people["birthCity"].isna()
        & people.apply(lambda row: is_puerto_rico(row.get("birthCountry"), row.get("birthState")), axis=1)
    ].copy()
    puerto_rico_country_only["birthCity"] = ""
    people_with_birth_city = pd.concat([people_with_birth_city, puerto_rico_country_only], ignore_index=True)
    people_with_birth_city["birthState"] = people_with_birth_city["birthState"].fillna("")
    people_with_birth_city["birthCountry"] = people_with_birth_city["birthCountry"].fillna("")
    for (city, state, country), group in people_with_birth_city.groupby(["birthCity", "birthState", "birthCountry"], dropna=False):
        loc = resolve_city(census_places, city, state, country)
        source = "Census Gazetteer place centroid" if loc["status"] == "matched" else None
        geonameid = None
        if loc["status"] != "matched":
            global_loc = resolve_global_city(geonames_cities, geonames_countries, city, state, country)
            if global_loc["status"] == "matched":
                loc = global_loc
                source = global_loc["source"]
                geonameid = global_loc["geonameid"]
        if loc["status"] != "matched" and is_puerto_rico(country, state):
            loc = PUERTO_RICO_CENTROID
            source = loc["source"]
            geonameid = loc["geonameid"]
        usvi_fallback = us_virgin_islands_fallback(city, state, country)
        if loc["status"] != "matched" and usvi_fallback:
            loc = usvi_fallback
            source = loc["source"]
            geonameid = loc["geonameid"]
        birth_locations.append(
            {
                "birth_city": city,
                "birth_state": state,
                "birth_country": country,
                "player_count": len(group),
                "geocode_status": loc["status"],
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "geocode_label": loc["source_label"],
                "geocode_source": source,
                "geonameid": geonameid,
            }
        )

    park_locations = []
    parks2 = parks.rename(columns={"park.key": "park_key", "park.name": "park_name", "park.alias": "park_alias"})
    for _, row in parks2.iterrows():
        loc = resolve_city(census_places, row["city"], row["state"], row["country"])
        park_locations.append(
            {
                "park_key": row["park_key"],
                "park_name": row["park_name"],
                "park_alias": row["park_alias"],
                "city": row["city"],
                "state": row["state"],
                "country": row["country"],
                "geocode_status": loc["status"],
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "geocode_label": loc["source_label"],
            }
        )

    player_rows = []
    for _, row in people.iterrows():
        first = clean(row.get("nameFirst")) or ""
        last = clean(row.get("nameLast")) or ""
        player_rows.append(
            {
                "playerID": row["playerID"],
                "name": f"{first} {last}".strip(),
                "birthYear": clean(row.get("birthYear")),
                "birthCity": clean(row.get("birthCity")),
                "birthState": clean(row.get("birthState")),
                "birthCountry": clean(row.get("birthCountry")),
                "debut": clean(row.get("debut")),
                "finalGame": clean(row.get("finalGame")),
                "bbrefID": clean(row.get("bbrefID")),
                "retroID": clean(row.get("retroID")),
            }
        )

    college_events = (
        college.groupby(["playerID", "schoolID"], dropna=False)
        .agg(start_year=("yearID", "min"), end_year=("yearID", "max"), years=("yearID", "nunique"))
        .reset_index()
    )

    hg = homegames.rename(columns={"year.key": "yearID", "league.key": "lgID", "team.key": "teamID", "park.key": "park_key"})
    hg["yearID"] = hg["yearID"].astype(int)
    app = appearances[["playerID", "yearID", "teamID", "lgID", "G_all"]].copy()
    team_names = teams[["yearID", "lgID", "teamID", "name"]].rename(columns={"name": "team_name"}).copy()
    team_names["yearID"] = team_names["yearID"].astype(int)
    park_events = (
        app.merge(hg[["yearID", "teamID", "lgID", "park_key", "games"]], on=["yearID", "teamID", "lgID"], how="left")
        .merge(team_names, on=["yearID", "teamID", "lgID"], how="left")
        .dropna(subset=["park_key"])
        .groupby(["playerID", "teamID", "lgID", "team_name", "park_key"], dropna=False)
        .agg(start_year=("yearID", "min"), end_year=("yearID", "max"), seasons=("yearID", "nunique"), appearance_rows=("yearID", "count"))
        .reset_index()
    )

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    pd.DataFrame(player_rows).to_sql("mlb_players", con, index=False, if_exists="replace")
    schools.to_sql("lahman_schools", con, index=False, if_exists="replace")
    college.to_sql("lahman_college_playing", con, index=False, if_exists="replace")
    parks2.to_sql("lahman_parks", con, index=False, if_exists="replace")
    team_names.to_sql("lahman_teams", con, index=False, if_exists="replace")
    homegames.to_sql("lahman_homegames", con, index=False, if_exists="replace")
    appearances[["playerID", "yearID", "teamID", "lgID", "G_all"]].to_sql("lahman_appearances", con, index=False, if_exists="replace")
    pd.DataFrame(school_matches).to_sql("mlb_school_geocode_cache", con, index=False, if_exists="replace")
    pd.DataFrame(birth_locations).to_sql("mlb_birthplace_geocode_cache", con, index=False, if_exists="replace")
    pd.DataFrame(park_locations).to_sql("mlb_park_geocode_cache", con, index=False, if_exists="replace")
    college_events.to_sql("mlb_college_events", con, index=False, if_exists="replace")
    park_events.to_sql("mlb_pro_park_events", con, index=False, if_exists="replace")
    pd.DataFrame(read_chadwick_people()).to_sql("chadwick_people_ids", con, index=False, if_exists="replace")

    con.executescript(
        """
        create view mlb_players_with_birthplace_location as
        select p.*, b.latitude as birth_latitude, b.longitude as birth_longitude, b.geocode_status as birth_geocode_status
        from mlb_players p
        left join mlb_birthplace_geocode_cache b
          on p.birthCity = b.birth_city
         and coalesce(p.birthState, '') = coalesce(b.birth_state, '')
         and p.birthCountry = b.birth_country;

        create view mlb_college_events_with_location as
        select e.*, s.lahman_name, s.lahman_city, s.lahman_state,
               s.resolved_latitude, s.resolved_longitude, s.resolved_source
        from mlb_college_events e
        left join mlb_school_geocode_cache s using (schoolID);

        create view mlb_pro_park_events_with_location as
        select e.*, p.park_name, p.city, p.state, p.latitude, p.longitude, p.geocode_status
        from mlb_pro_park_events e
        left join mlb_park_geocode_cache p using (park_key);

        create view mlb_chadwick_college_hints as
        select p.playerID, p.name, c.col_played_first, c.col_played_last
        from mlb_players p
        join chadwick_people_ids c on p.playerID = c.key_bbref
        where c.col_played_first is not null and c.col_played_first != '';
        """
    )
    con.commit()

    summary = {
        "sqlite_cache": str(DB_PATH),
        "mlb_players": len(player_rows),
        "birthplace_unique_places": len(birth_locations),
        "birthplace_resolved_places": int(sum(1 for row in birth_locations if row["geocode_status"] == "matched")),
        "lahman_schools": len(school_matches),
        "lahman_schools_scorecard_matched": int(sum(1 for row in school_matches if row["scorecard_match_status"] == "matched")),
        "lahman_schools_city_geocoded": int(sum(1 for row in school_matches if row["city_geocode_status"] == "matched")),
        "lahman_schools_resolved": int(sum(1 for row in school_matches if row["resolved_latitude"] is not None)),
        "college_event_player_school_rows": int(len(college_events)),
        "college_players": int(college_events["playerID"].nunique()),
        "parks": len(park_locations),
        "parks_resolved": int(sum(1 for row in park_locations if row["geocode_status"] == "matched")),
        "pro_park_event_player_park_rows": int(len(park_events)),
        "chadwick_college_hint_players": int(con.execute("select count(*) from mlb_chadwick_college_hints").fetchone()[0]),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))

    for table, filename in [
        ("mlb_school_geocode_cache", "mlb_school_geocode_cache.csv"),
        ("mlb_birthplace_geocode_cache", "mlb_birthplace_geocode_cache.csv"),
        ("mlb_park_geocode_cache", "mlb_park_geocode_cache.csv"),
        ("mlb_chadwick_college_hints", "mlb_chadwick_college_hints.csv"),
    ]:
        pd.read_sql_query(f"select * from {table}", con).to_csv(SCRATCH / filename, index=False)

    con.close()
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
