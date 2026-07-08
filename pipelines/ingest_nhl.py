#!/usr/bin/env python3
"""
Build the NHL geography enrichment cache.

The pipeline uses public NHL records/stats endpoints, caches raw JSON pages
under data/raw/nhl/, and writes a derived SQLite cache under scratch/.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_NHL = ROOT / "data/raw/nhl"
RAW_RECORDS = RAW_NHL / "records"
RAW_STATS = RAW_NHL / "stats"
DB_PATH = SCRATCH / "nhl_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "nhl_enrichment_summary.json"

RECORDS_API = "https://records.nhl.com/site/api"
STATS_API = "https://api.nhle.com/stats/rest/en"
USER_AGENT = "HometownHeroesPipeline/0.1"

GAZETTEER_ZIP_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip"
GAZETTEER_ZIP = ROOT / "data/raw/geography/census-gazetteer-2025/2025_Gaz_place_national.zip"
GEONAMES_DIR = ROOT / "data/raw/geography/geonames"
GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities500.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
GEONAMES_COUNTRY_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
GEONAMES_CITIES_ZIP = GEONAMES_DIR / "cities500.zip"
GEONAMES_ADMIN1 = GEONAMES_DIR / "admin1CodesASCII.txt"
GEONAMES_COUNTRY = GEONAMES_DIR / "countryInfo.txt"

COUNTRY_ALIASES = {
    "can": "CA",
    "canada": "CA",
    "cdn": "CA",
    "czech republic": "CZ",
    "czechoslovakia": "CZ",
    "deu": "DE",
    "england": "GB",
    "fin": "FI",
    "frg": "DE",
    "ger": "DE",
    "germany": "DE",
    "great britain": "GB",
    "lat": "LV",
    "russia": "RU",
    "slovakia": "SK",
    "soviet union": "RU",
    "sui": "CH",
    "sweden": "SE",
    "ukraine": "UA",
    "united states": "US",
    "usa": "US",
    "us": "US",
    "u s a": "US",
    "yugoslavia": "RS",
}

TEAM_PLACE_OVERRIDES = {
    "Anaheim Ducks": ("Anaheim", "CA", "USA"),
    "Arizona Coyotes": ("Glendale", "AZ", "USA"),
    "Atlanta Flames": ("Atlanta", "GA", "USA"),
    "Atlanta Thrashers": ("Atlanta", "GA", "USA"),
    "Boston Bruins": ("Boston", "MA", "USA"),
    "Buffalo Sabres": ("Buffalo", "NY", "USA"),
    "Calgary Flames": ("Calgary", "AB", "Canada"),
    "California Golden Seals": ("Oakland", "CA", "USA"),
    "Carolina Hurricanes": ("Raleigh", "NC", "USA"),
    "Chicago Blackhawks": ("Chicago", "IL", "USA"),
    "Cleveland Barons": ("Cleveland", "OH", "USA"),
    "Colorado Avalanche": ("Denver", "CO", "USA"),
    "Colorado Rockies": ("Denver", "CO", "USA"),
    "Columbus Blue Jackets": ("Columbus", "OH", "USA"),
    "Dallas Stars": ("Dallas", "TX", "USA"),
    "Detroit Red Wings": ("Detroit", "MI", "USA"),
    "Edmonton Oilers": ("Edmonton", "AB", "Canada"),
    "Florida Panthers": ("Sunrise", "FL", "USA"),
    "Hartford Whalers": ("Hartford", "CT", "USA"),
    "Kansas City Scouts": ("Kansas City", "MO", "USA"),
    "Los Angeles Kings": ("Los Angeles", "CA", "USA"),
    "Minnesota North Stars": ("Bloomington", "MN", "USA"),
    "Minnesota Wild": ("Saint Paul", "MN", "USA"),
    "Montréal Canadiens": ("Montreal", "QC", "Canada"),
    "Nashville Predators": ("Nashville", "TN", "USA"),
    "New Jersey Devils": ("Newark", "NJ", "USA"),
    "New York Islanders": ("Elmont", "NY", "USA"),
    "New York Rangers": ("New York", "NY", "USA"),
    "Oakland Seals": ("Oakland", "CA", "USA"),
    "Ottawa Senators": ("Ottawa", "ON", "Canada"),
    "Philadelphia Flyers": ("Philadelphia", "PA", "USA"),
    "Phoenix Coyotes": ("Glendale", "AZ", "USA"),
    "Pittsburgh Penguins": ("Pittsburgh", "PA", "USA"),
    "Quebec Nordiques": ("Quebec City", "QC", "Canada"),
    "San Jose Sharks": ("San Jose", "CA", "USA"),
    "Seattle Kraken": ("Seattle", "WA", "USA"),
    "St. Louis Blues": ("St. Louis", "MO", "USA"),
    "Tampa Bay Lightning": ("Tampa", "FL", "USA"),
    "Toronto Maple Leafs": ("Toronto", "ON", "Canada"),
    "Utah Hockey Club": ("Salt Lake City", "UT", "USA"),
    "Utah Mammoth": ("Salt Lake City", "UT", "USA"),
    "Vancouver Canucks": ("Vancouver", "BC", "Canada"),
    "Vegas Golden Knights": ("Las Vegas", "NV", "USA"),
    "Washington Capitals": ("Washington", "DC", "USA"),
    "Winnipeg Jets": ("Winnipeg", "MB", "Canada"),
    "Winnipeg Jets (1979)": ("Winnipeg", "MB", "Canada"),
}


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response, tmp.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.rename(dest)


def fetch_json(url: str, dest: Path, sleep_seconds: float = 0.05) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return json.loads(dest.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    time.sleep(sleep_seconds)
    return data


def api_page(base: str, resource: str, params: dict[str, object], cache_dir: Path, cache_name: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    url = f"{base}/{resource}"
    if query:
        url = f"{url}?{query}"
    return fetch_json(url, cache_dir / f"{cache_name}.json")


def paged_records(resource: str, cache_name: str, limit: int = 1000, extra: dict[str, object] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    total = None
    while total is None or start < total:
        params = {"limit": limit, "start": start}
        if extra:
            params.update(extra)
        data = api_page(RECORDS_API, resource, params, RAW_RECORDS / cache_name, f"{start:06d}")
        page = data.get("data") or []
        total = int(data.get("total") or len(page))
        rows.extend(page)
        if not page:
            break
        start += len(page)
        log(f"{resource}: {min(start, total)}/{total}")
    return rows


def paged_stats(resource: str, cache_name: str, limit: int = 1000, extra: dict[str, object] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    total = None
    while total is None or start < total:
        params = {"limit": limit, "start": start}
        if extra:
            params.update(extra)
        data = api_page(STATS_API, resource, params, RAW_STATS / cache_name, f"{start:06d}")
        page = data.get("data") or []
        total = int(data.get("total") or len(page))
        rows.extend(page)
        if not page:
            break
        start += len(page)
    return rows


def ascii_norm(value: object) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def place_name_norm(name: str | None) -> str:
    name = " ".join((name or "").strip().split())
    for suffix in (" city", " town", " village", " borough", " municipality", " township", " CDP"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower()


def load_census_places() -> dict[tuple[str, str], dict[str, str | float]]:
    download(GAZETTEER_ZIP_URL, GAZETTEER_ZIP)
    gaz: dict[tuple[str, str], dict[str, str | float]] = {}
    with zipfile.ZipFile(GAZETTEER_ZIP) as zf:
        with zf.open("2025_Gaz_place_national.txt") as raw:
            text = (line.decode("utf-8") for line in raw)
            reader = csv.DictReader(text, delimiter="|")
            for row in reader:
                state = row["USPS"].upper()
                gaz.setdefault(
                    (state, place_name_norm(row["NAME"])),
                    {
                        "city": place_name_norm(row["NAME"]),
                        "state": state,
                        "latitude": float(row["INTPTLAT"]),
                        "longitude": float(row["INTPTLONG"]),
                        "source_label": row["NAME"],
                    },
                )
    gaz[("DC", "washington")] = {
        "city": "washington",
        "state": "DC",
        "latitude": 38.9072,
        "longitude": -77.0369,
        "source_label": "Washington, DC",
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
            countries[parts[0]] = {"iso": parts[0], "iso3": parts[1], "fips": parts[3], "country": parts[4]}
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


def load_geonames_cities() -> tuple[dict[tuple[str, str], list[dict[str, object]]], dict[str, dict[str, str]]]:
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
                    if name:
                        index.setdefault((country, name), []).append(row)
    for candidates in index.values():
        candidates.sort(key=lambda row: int(row["population"]), reverse=True)
    return index, countries


def resolve_us_city(gaz: dict[tuple[str, str], dict[str, str | float]], city: str | None, state: str | None) -> dict[str, object] | None:
    if not city or not state:
        return None
    row = gaz.get((state.upper(), place_name_norm(city)))
    if not row:
        return None
    return {
        "status": "matched",
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "source_label": f"{row['source_label']}, {state.upper()}",
        "source": "Census Gazetteer place centroid",
        "geonameid": None,
    }


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
            if state_key in row["admin1_names"] or state_key == ascii_norm(row["admin1"]):
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


def resolve_place(gaz: dict[tuple[str, str], dict[str, str | float]], geonames, countries, city: str | None, state: str | None, country: str | None) -> dict[str, object]:
    iso = country_to_iso(country, countries)
    if iso == "US":
        us = resolve_us_city(gaz, city, state)
        if us:
            return us
    return resolve_global_city(geonames, countries, city, state, country)


def season_year(season_id: object) -> int | None:
    if season_id is None:
        return None
    text = str(season_id)
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def clean_records(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    out = []
    for row in rows:
        cleaned = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                cleaned[key] = json.dumps(value)
            else:
                cleaned[key] = value
        out.append(cleaned)
    return out


def split_team_abbrevs(value: object) -> list[str]:
    parts = re.split(r"[,/]", str(value or ""))
    return [part.strip() for part in parts if part.strip()]


def build_team_name_map(franchise_rows: list[dict[str, Any]]) -> dict[str, str]:
    out = {}
    for row in franchise_rows:
        tri = str(row.get("triCode") or "").strip()
        name = str(row.get("teamName") or "").strip()
        if tri and name:
            out[tri] = name
    return out


def build_player_team_seasons(skater_rows: list[dict[str, Any]], goalie_rows: list[dict[str, Any]], team_names: dict[str, str]) -> pd.DataFrame:
    rows = []
    for row in skater_rows:
        if int(row.get("gamesPlayed") or 0) <= 0:
            continue
        for team_abbrev in split_team_abbrevs(row.get("teamAbbrevs")):
            rows.append(
                {
                    "player_id": str(row["playerId"]),
                    "display_name": row.get("skaterFullName") or f"{row.get('firstName', '')} {row.get('lastName', '')}".strip(),
                    "season": season_year(row.get("seasonId")),
                    "season_id": row.get("seasonId"),
                    "team_abbrev": team_abbrev,
                    "team_name": team_names.get(team_abbrev, team_abbrev),
                    "games_played": row.get("gamesPlayed"),
                    "position": row.get("positionCode"),
                    "source": "NHL Stats REST skater summary",
                }
            )
    for row in goalie_rows:
        if int(row.get("gamesPlayed") or 0) <= 0:
            continue
        for team_abbrev in split_team_abbrevs(row.get("teamAbbrevs")):
            rows.append(
                {
                    "player_id": str(row["playerId"]),
                    "display_name": row.get("goalieFullName") or f"{row.get('firstName', '')} {row.get('lastName', '')}".strip(),
                    "season": season_year(row.get("seasonId")),
                    "season_id": row.get("seasonId"),
                    "team_abbrev": team_abbrev,
                    "team_name": team_names.get(team_abbrev, team_abbrev),
                    "games_played": row.get("gamesPlayed"),
                    "position": "G",
                    "source": "NHL Stats REST goalie summary",
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["player_id", "display_name", "season", "season_id", "team_abbrev", "team_name", "games_played", "position", "source"])
    df = df.dropna(subset=["player_id", "season", "team_name"])
    return df.sort_values(["season", "team_name", "display_name"])


def team_place(team_name: str | None) -> tuple[str | None, str | None, str | None]:
    if not team_name:
        return None, None, None
    if team_name in TEAM_PLACE_OVERRIDES:
        return TEAM_PLACE_OVERRIDES[team_name]
    parts = str(team_name).split()
    if len(parts) <= 1:
        return team_name, None, None
    place = " ".join(parts[:-1])
    return place, None, None


def build_team_geocode(
    player_team_seasons: pd.DataFrame,
    gaz: dict[tuple[str, str], dict[str, str | float]],
    geonames: dict[tuple[str, str], list[dict[str, object]]],
    countries: dict[str, dict[str, str]],
) -> pd.DataFrame:
    teams = (
        player_team_seasons[["team_abbrev", "team_name"]]
        .dropna()
        .drop_duplicates()
        .sort_values(["team_name", "team_abbrev"])
    )
    rows = []
    for record in teams.to_dict("records"):
        city, state, country = team_place(record["team_name"])
        resolved = resolve_place(gaz, geonames, countries, city, state, country)
        rows.append(
            {
                "team_abbrev": record["team_abbrev"],
                "team_name": record["team_name"],
                "city": city,
                "state": state,
                "country": country,
                "latitude": resolved["latitude"],
                "longitude": resolved["longitude"],
                "geocode_status": resolved["status"],
                "geocode_source": resolved["source"],
                "source_label": resolved["source_label"],
            }
        )
    return pd.DataFrame(rows)


def build_birthplace_geocode(players: list[dict[str, Any]], gaz, geonames, countries) -> pd.DataFrame:
    unique = {}
    for player in players:
        city = player.get("birthCity")
        country = player.get("birthCountry")
        state = player.get("birthStateProvince")
        if not city or not country:
            continue
        unique[(city, state, country)] = None
    rows = []
    for city, state, country in sorted(unique, key=lambda parts: tuple("" if part is None else str(part) for part in parts)):
        resolved = resolve_place(gaz, geonames, countries, city, state, country)
        rows.append(
            {
                "birth_city": city,
                "birth_state": state,
                "birth_country": country,
                "latitude": resolved["latitude"],
                "longitude": resolved["longitude"],
                "geocode_status": resolved["status"],
                "geocode_source": resolved["source"],
                "source_label": resolved["source_label"],
                "geonameid": resolved["geonameid"],
            }
        )
    return pd.DataFrame(rows)


def build_players(players: list[dict[str, Any]], player_team_seasons: pd.DataFrame) -> pd.DataFrame:
    p = pd.DataFrame(clean_records(players))
    p["player_id"] = p["id"].astype(str)
    if not player_team_seasons.empty:
        bounds = player_team_seasons.groupby("player_id")["season"].agg(first_season="min", last_season="max").reset_index()
        p = pd.merge(p, bounds, on="player_id", how="left")
    else:
        p["first_season"] = None
        p["last_season"] = None
    columns = [
        "player_id",
        "fullName",
        "birthDate",
        "birthCity",
        "birthStateProvince",
        "birthCountry",
        "first_season",
        "last_season",
        "inHockeyHof",
        "hofInductionYear",
        "yearsPro",
        "nhlExperience",
        "position",
        "nationality",
    ]
    for col in columns:
        if col not in p.columns:
            p[col] = None
    return p[columns].rename(
        columns={
            "fullName": "display_name",
            "birthDate": "birth_date",
            "birthCity": "birth_city",
            "birthStateProvince": "birth_state",
            "birthCountry": "birth_country",
            "inHockeyHof": "in_hockey_hof",
            "hofInductionYear": "hof_induction_year",
            "yearsPro": "years_pro",
            "nhlExperience": "nhl_experience",
        }
    )


def write_dataframe(con: sqlite3.Connection, df: pd.DataFrame, table: str) -> None:
    df.to_sql(table, con, index=False, if_exists="replace")


def write_summary(players: pd.DataFrame, player_team_seasons: pd.DataFrame, birth_geocode: pd.DataFrame, team_geocode: pd.DataFrame) -> dict[str, object]:
    summary = {
        "nhl_players": int(len(players)),
        "players_with_birthplace": int(players["birth_city"].notna().sum()) if not players.empty else 0,
        "players_with_career_seasons": int(players["first_season"].notna().sum()) if not players.empty else 0,
        "player_team_seasons": int(len(player_team_seasons)),
        "birthplaces": int(len(birth_geocode)),
        "birthplaces_geocoded": int((birth_geocode["geocode_status"] == "matched").sum()) if not birth_geocode.empty else 0,
        "teams": int(len(team_geocode)),
        "teams_geocoded": int((team_geocode["geocode_status"] == "matched").sum()) if not team_geocode.empty else 0,
        "sqlite_cache": repo_path(DB_PATH),
        "notes": [
            "NHL player/team seasons come from NHL Stats REST regular-season skater and goalie summaries.",
            "NHL pro locations currently use team city centroids, not exact venue coordinates.",
            "NHL birthplaces use Census Gazetteer for US places and GeoNames cities500 for non-US places.",
        ],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    RAW_NHL.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(exist_ok=True)
    gaz = load_census_places()
    geonames, countries = load_geonames_cities()

    players_raw = paged_records("player", "players")
    franchise_team_totals = paged_records("franchise-team-totals", "franchise_team_totals")
    team_names = build_team_name_map(franchise_team_totals)
    seasons = paged_stats("season", "seasons")
    season_ids = sorted({row["id"] for row in seasons if row.get("id") and int(row["id"]) >= 19591960})
    skater_rows: list[dict[str, Any]] = []
    goalie_rows: list[dict[str, Any]] = []
    for season_id in season_ids:
        exp = f"seasonId={season_id} and gameTypeId=2"
        skaters = paged_stats("skater/summary", f"skater_summary/{season_id}", extra={"cayenneExp": exp})
        goalies = paged_stats("goalie/summary", f"goalie_summary/{season_id}", extra={"cayenneExp": exp})
        skater_rows.extend(skaters)
        goalie_rows.extend(goalies)
        log(f"NHL season {season_id}: skaters {len(skaters)}, goalies {len(goalies)}")

    player_team_seasons = build_player_team_seasons(skater_rows, goalie_rows, team_names)
    players = build_players(players_raw, player_team_seasons)
    birth_geocode = build_birthplace_geocode(players_raw, gaz, geonames, countries)
    team_geocode = build_team_geocode(player_team_seasons, gaz, geonames, countries)

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    try:
        write_dataframe(con, players, "nhl_players")
        write_dataframe(con, player_team_seasons, "nhl_player_team_seasons")
        write_dataframe(con, birth_geocode, "nhl_birthplace_geocode_cache")
        write_dataframe(con, team_geocode, "nhl_team_geocode_cache")
        write_dataframe(con, pd.DataFrame(clean_records(players_raw)), "nhl_records_players_raw")
        write_dataframe(con, pd.DataFrame(clean_records(franchise_team_totals)), "nhl_franchise_team_totals_raw")
        write_dataframe(con, pd.DataFrame(clean_records(seasons)), "nhl_seasons_raw")
        con.executescript(
            """
            create index idx_nhl_players_id on nhl_players(player_id);
            create index idx_nhl_player_team_player on nhl_player_team_seasons(player_id);
            create index idx_nhl_player_team_team on nhl_player_team_seasons(team_name);
            create index idx_nhl_birthplace on nhl_birthplace_geocode_cache(birth_city, birth_state, birth_country);
            create index idx_nhl_team_geocode on nhl_team_geocode_cache(team_name);
            """
        )
    finally:
        con.close()
    print(json.dumps(write_summary(players, player_team_seasons, birth_geocode, team_geocode), indent=2))


if __name__ == "__main__":
    main()
