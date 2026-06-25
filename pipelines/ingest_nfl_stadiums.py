#!/usr/bin/env python3
"""
Build NFL stadium and team-season home-location caches.

This pipeline uses nflverse/Lee Sharpe schedules for structured game-level
home stadiums, then resolves unique stadium coordinates through Wikidata.
The derived pro-player stadium events are roster-home associations, not
game-appearance records.
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
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_NFL = ROOT / "data/raw/nfl"
RAW_SCHEDULES = RAW_NFL / "nflverse-schedules"
RAW_ROSTERS = RAW_NFL / "nflverse-rosters"
RAW_WIKIDATA = ROOT / "data/raw/wikidata/stadiums"

SCHEDULES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
SCHEDULES_CSV = RAW_SCHEDULES / "games.csv"
WIKIDATA_CACHE = RAW_WIKIDATA / "nfl_stadium_geocode.json"
WIKIDATA_QID_CACHE = RAW_WIKIDATA / "nfl_stadium_geocode_qids.json"
DB_PATH = SCRATCH / "nfl_stadium_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "nfl_stadium_enrichment_summary.json"
UNRESOLVED_CSV = SCRATCH / "nfl_unresolved_stadiums.csv"

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "HometownHeroesPipeline/0.1"

TEAM_ALIASES = {
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "PHO": "ARI",
    "SD": "LAC",
    "SL": "LAR",
    "STL": "LAR",
}

STADIUM_QUERY_ALIASES = {
    "FedExField": "FedExField",
    "FirstEnergy Stadium": "FirstEnergy Stadium",
    "Invesco Field at Mile High": "Empower Field at Mile High",
    "Lambeau Field": "Lambeau Field",
    "Louisiana Superdome": "Caesars Superdome",
    "M&T Bank Stadium": "M&T Bank Stadium",
    "New Era Field": "Highmark Stadium",
    "Oakland-Alameda County Coliseum": "Oakland Coliseum",
    "Paul Brown Stadium": "Paycor Stadium",
    "Qualcomm Stadium": "San Diego Stadium",
    "Ralph Wilson Stadium": "Highmark Stadium",
    "Raymond James Stadium": "Raymond James Stadium",
    "Reliant Stadium": "NRG Stadium",
    "Sports Authority Field at Mile High": "Empower Field at Mile High",
    "Sun Life Stadium": "Hard Rock Stadium",
    "University of Phoenix Stadium": "State Farm Stadium",
}

STADIUM_QID_ALIASES = {
    "3Com Park": "Q1033076",
    "CenturyLink Field": "Q612736",
    "Dolphin Stadium": "Q864339",
    "Dome at America's Center": "Q1292739",
    "EverBank Stadium": "Q635117",
    "Mall of America Field": "Q1072186",
    "Memorial Stadium (Champaign)": "Q3305514",
    "Monster Park": "Q1033076",
    "New Meadowlands Stadium": "Q10862290",
    "NRG Stadium": "Q1058864",
    "Pro Player Stadium": "Q864339",
    "Ring Central Coliseum": "Q1147732",
    "Seahawks Stadium": "Q612736",
    "Seattle Kingdome": "Q990430",
    "Tiger Stadium (LSU)": "Q1594708",
    "TWA Dome": "Q1292739",
}


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def download(url: str, dest: Path, refresh: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        log(f"using cached download: {repo_path(dest)}")
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as response, tmp.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    tmp.rename(dest)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize_team(team: str | None) -> str:
    team = (team or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def stable_season(value: str | None) -> int | None:
    if value and re.fullmatch(r"\d{4}", value.strip()):
        return int(value)
    return None


def derive_team_season_stadiums(schedule_rows: list[dict[str, str]]) -> list[dict[str, object]]:
    counts: Counter[tuple[int, str, str, str]] = Counter()
    totals: Counter[tuple[int, str]] = Counter()
    for row in schedule_rows:
        season = stable_season(row.get("season"))
        home_team = normalize_team(row.get("home_team"))
        stadium_id = (row.get("stadium_id") or "").strip()
        stadium = (row.get("stadium") or "").strip()
        if season is None or not home_team or not stadium_id or not stadium:
            continue
        if (row.get("location") or "").strip().lower() != "home":
            continue
        counts[(season, home_team, stadium_id, stadium)] += 1
        totals[(season, home_team)] += 1

    grouped: dict[tuple[int, str], list[tuple[tuple[int, str, str, str], int]]] = defaultdict(list)
    for key, count in counts.items():
        season, team, _, _ = key
        grouped[(season, team)].append((key, count))

    rows = []
    for (season, team), options in sorted(grouped.items()):
        key, home_games = max(options, key=lambda item: (item[1], item[0][2], item[0][3]))
        _, _, stadium_id, stadium = key
        rows.append(
            {
                "season": season,
                "team": team,
                "stadium_id": stadium_id,
                "stadium_name": stadium,
                "home_games": home_games,
                "total_home_games": totals[(season, team)],
                "source": "nflverse schedules home games",
            }
        )
    return rows


def stadium_query_names(team_season_rows: list[dict[str, object]]) -> list[str]:
    names = sorted({str(row["stadium_name"]) for row in team_season_rows if row.get("stadium_name")})
    return [STADIUM_QUERY_ALIASES.get(name, name) for name in names]


def sparql_escape_label(label: str) -> str:
    return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"@en'


def fetch_wikidata_stadiums(labels: list[str], refresh: bool = False, sleep_seconds: float = 0.5) -> dict[str, object]:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    if WIKIDATA_CACHE.exists() and WIKIDATA_CACHE.stat().st_size > 0 and not refresh:
        return json.loads(WIKIDATA_CACHE.read_text())

    values = " ".join(sparql_escape_label(label) for label in sorted(set(labels)))
    query = f"""
SELECT ?queryLabel ?stadium ?stadiumLabel ?coord ?locatedInLabel ?countryLabel WHERE {{
  VALUES ?queryLabel {{ {values} }}
  {{
    ?stadium rdfs:label ?queryLabel.
  }}
  UNION
  {{
    ?stadium skos:altLabel ?queryLabel.
  }}
  ?stadium wdt:P625 ?coord.
  OPTIONAL {{ ?stadium wdt:P131 ?locatedIn. }}
  OPTIONAL {{ ?stadium wdt:P17 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?queryLabel ?stadiumLabel
"""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(
        f"{WIKIDATA_ENDPOINT}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    log("fetching Wikidata stadium coordinates")
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    WIKIDATA_CACHE.write_text(json.dumps(data, indent=2))
    time.sleep(sleep_seconds)
    return data


def fetch_wikidata_qid_stadiums(refresh: bool = False, sleep_seconds: float = 0.5) -> dict[str, object]:
    RAW_WIKIDATA.mkdir(parents=True, exist_ok=True)
    if WIKIDATA_QID_CACHE.exists() and WIKIDATA_QID_CACHE.stat().st_size > 0 and not refresh:
        return json.loads(WIKIDATA_QID_CACHE.read_text())

    values = " ".join(f"wd:{qid}" for qid in sorted(set(STADIUM_QID_ALIASES.values())))
    query = f"""
SELECT ?stadium ?stadiumLabel ?coord ?locatedInLabel ?countryLabel WHERE {{
  VALUES ?stadium {{ {values} }}
  ?stadium wdt:P625 ?coord.
  OPTIONAL {{ ?stadium wdt:P131 ?locatedIn. }}
  OPTIONAL {{ ?stadium wdt:P17 ?country. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?stadiumLabel
"""
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(
        f"{WIKIDATA_ENDPOINT}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    log("fetching Wikidata stadium coordinate QID fallbacks")
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    WIKIDATA_QID_CACHE.write_text(json.dumps(data, indent=2))
    time.sleep(sleep_seconds)
    return data


def parse_point(value: str | None) -> tuple[float | None, float | None]:
    if not value:
        return None, None
    match = re.fullmatch(r"Point\((-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\)", value)
    if not match:
        return None, None
    lon, lat = match.groups()
    return float(lat), float(lon)


def wikidata_matches(data: dict[str, object]) -> dict[str, dict[str, object]]:
    results = data.get("results", {}).get("bindings", [])
    matches: dict[str, dict[str, object]] = {}
    for binding in results:
        query_label = binding.get("queryLabel", {}).get("value")
        if not query_label or query_label in matches:
            continue
        lat, lon = parse_point(binding.get("coord", {}).get("value"))
        if lat is None or lon is None:
            continue
        stadium_uri = binding.get("stadium", {}).get("value", "")
        qid = stadium_uri.rsplit("/", 1)[-1] if stadium_uri else ""
        matches[query_label] = {
            "wikidata_qid": qid,
            "matched_label": binding.get("stadiumLabel", {}).get("value", ""),
            "city": binding.get("locatedInLabel", {}).get("value", ""),
            "country": binding.get("countryLabel", {}).get("value", ""),
            "latitude": lat,
            "longitude": lon,
            "geocode_source": "Wikidata P625 coordinate",
        }
    return matches


def wikidata_qid_matches(data: dict[str, object]) -> dict[str, dict[str, object]]:
    results = data.get("results", {}).get("bindings", [])
    by_qid: dict[str, dict[str, object]] = {}
    for binding in results:
        stadium_uri = binding.get("stadium", {}).get("value", "")
        qid = stadium_uri.rsplit("/", 1)[-1] if stadium_uri else ""
        lat, lon = parse_point(binding.get("coord", {}).get("value"))
        if not qid or lat is None or lon is None:
            continue
        by_qid[qid] = {
            "wikidata_qid": qid,
            "matched_label": binding.get("stadiumLabel", {}).get("value", ""),
            "city": binding.get("locatedInLabel", {}).get("value", ""),
            "country": binding.get("countryLabel", {}).get("value", ""),
            "latitude": lat,
            "longitude": lon,
            "geocode_source": "Wikidata P625 coordinate",
        }
    return by_qid


def build_stadium_geocode_rows(
    team_season_rows: list[dict[str, object]],
    matches: dict[str, dict[str, object]],
    qid_matches: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    by_stadium: dict[tuple[str, str], dict[str, object]] = {}
    for row in team_season_rows:
        stadium_id = str(row["stadium_id"])
        stadium_name = str(row["stadium_name"])
        by_stadium[(stadium_id, stadium_name)] = {"stadium_id": stadium_id, "stadium_name": stadium_name}

    geocode_rows = []
    for key in sorted(by_stadium):
        stadium_id, stadium_name = key
        query_name = STADIUM_QUERY_ALIASES.get(stadium_name, stadium_name)
        match = matches.get(query_name)
        if not match and query_name in STADIUM_QID_ALIASES:
            match = qid_matches.get(STADIUM_QID_ALIASES[query_name])
        if match:
            geocode_rows.append(
                {
                    "stadium_id": stadium_id,
                    "stadium_name": stadium_name,
                    "query_name": query_name,
                    "match_status": "matched",
                    **match,
                }
            )
        else:
            geocode_rows.append(
                {
                    "stadium_id": stadium_id,
                    "stadium_name": stadium_name,
                    "query_name": query_name,
                    "match_status": "unresolved",
                    "wikidata_qid": "",
                    "matched_label": "",
                    "city": "",
                    "country": "",
                    "latitude": None,
                    "longitude": None,
                    "geocode_source": "Wikidata P625 coordinate",
                }
            )
    return geocode_rows


def load_roster_seasons() -> list[dict[str, object]]:
    rows = []
    for path in sorted(RAW_ROSTERS.glob("roster_*.csv")):
        season_match = re.search(r"(\d{4})", path.name)
        if not season_match:
            continue
        file_season = int(season_match.group(1))
        for row in read_csv(path):
            season = stable_season(row.get("season")) or file_season
            team = normalize_team(row.get("team"))
            player_id = (row.get("gsis_id") or row.get("pfr_id") or "").strip()
            if not player_id or not team:
                continue
            rows.append(
                {
                    "season": season,
                    "team": team,
                    "player_id": player_id,
                    "gsis_id": (row.get("gsis_id") or "").strip(),
                    "pfr_id": (row.get("pfr_id") or "").strip(),
                    "full_name": (row.get("full_name") or "").strip(),
                    "status": (row.get("status") or "").strip(),
                }
            )
    return rows


def derive_player_stadium_events(
    roster_rows: list[dict[str, object]],
    team_season_rows: list[dict[str, object]],
    geocode_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    team_season = {(int(row["season"]), str(row["team"])): row for row in team_season_rows}
    geocoded = {
        (str(row["stadium_id"]), str(row["stadium_name"])): row
        for row in geocode_rows
        if row.get("match_status") == "matched" and row.get("latitude") is not None and row.get("longitude") is not None
    }
    years: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    names: dict[str, str] = {}
    for row in roster_rows:
        season = int(row["season"])
        team = str(row["team"])
        player_id = str(row["player_id"])
        stadium = team_season.get((season, team))
        if not stadium:
            continue
        stadium_key = (str(stadium["stadium_id"]), str(stadium["stadium_name"]))
        if stadium_key not in geocoded:
            continue
        key = (player_id, team, stadium_key[0], stadium_key[1])
        years[key].add(season)
        names.setdefault(player_id, str(row.get("full_name") or ""))

    events = []
    for (player_id, team, stadium_id, stadium_name), seasons in sorted(years.items()):
        sorted_years = sorted(seasons)
        events.append(
            {
                "player_id": player_id,
                "full_name": names.get(player_id, ""),
                "team": team,
                "stadium_id": stadium_id,
                "stadium_name": stadium_name,
                "start_year": min(sorted_years),
                "end_year": max(sorted_years),
                "seasons": len(sorted_years),
                "season_list": ",".join(str(year) for year in sorted_years),
                "source": "nflverse rosters + schedules home stadium",
            }
        )
    return events


def create_sql_cache(
    schedule_rows: list[dict[str, str]],
    team_season_rows: list[dict[str, object]],
    geocode_rows: list[dict[str, object]],
    roster_rows: list[dict[str, object]],
    player_events: list[dict[str, object]],
) -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("pragma journal_mode=wal")
    con.execute(
        """
        create table nfl_schedules (
            game_id text,
            season integer,
            game_type text,
            week integer,
            gameday text,
            away_team text,
            home_team text,
            location text,
            stadium_id text,
            stadium text
        )
        """
    )
    con.executemany(
        """
        insert into nfl_schedules values
        (:game_id,:season,:game_type,:week,:gameday,:away_team,:home_team,:location,:stadium_id,:stadium)
        """,
        [
            {
                "game_id": row.get("game_id", ""),
                "season": stable_season(row.get("season")),
                "game_type": row.get("game_type", ""),
                "week": int(row["week"]) if (row.get("week") or "").isdigit() else None,
                "gameday": row.get("gameday", ""),
                "away_team": normalize_team(row.get("away_team")),
                "home_team": normalize_team(row.get("home_team")),
                "location": row.get("location", ""),
                "stadium_id": row.get("stadium_id", ""),
                "stadium": row.get("stadium", ""),
            }
            for row in schedule_rows
        ],
    )
    con.execute(
        """
        create table nfl_team_season_stadiums (
            season integer,
            team text,
            stadium_id text,
            stadium_name text,
            home_games integer,
            total_home_games integer,
            source text,
            primary key (season, team)
        )
        """
    )
    con.executemany(
        """
        insert into nfl_team_season_stadiums values
        (:season,:team,:stadium_id,:stadium_name,:home_games,:total_home_games,:source)
        """,
        team_season_rows,
    )
    con.execute(
        """
        create table nfl_stadium_geocode_cache (
            stadium_id text,
            stadium_name text,
            query_name text,
            match_status text,
            wikidata_qid text,
            matched_label text,
            city text,
            country text,
            latitude real,
            longitude real,
            geocode_source text,
            primary key (stadium_id, stadium_name)
        )
        """
    )
    con.executemany(
        """
        insert into nfl_stadium_geocode_cache values
        (:stadium_id,:stadium_name,:query_name,:match_status,:wikidata_qid,:matched_label,:city,:country,:latitude,:longitude,:geocode_source)
        """,
        geocode_rows,
    )
    con.execute(
        """
        create table nfl_roster_seasons (
            season integer,
            team text,
            player_id text,
            gsis_id text,
            pfr_id text,
            full_name text,
            status text
        )
        """
    )
    con.executemany(
        """
        insert into nfl_roster_seasons values
        (:season,:team,:player_id,:gsis_id,:pfr_id,:full_name,:status)
        """,
        roster_rows,
    )
    con.execute(
        """
        create table nfl_pro_stadium_events (
            player_id text,
            full_name text,
            team text,
            stadium_id text,
            stadium_name text,
            start_year integer,
            end_year integer,
            seasons integer,
            season_list text,
            source text
        )
        """
    )
    con.executemany(
        """
        insert into nfl_pro_stadium_events values
        (:player_id,:full_name,:team,:stadium_id,:stadium_name,:start_year,:end_year,:seasons,:season_list,:source)
        """,
        player_events,
    )
    con.executescript(
        """
        create index idx_nfl_team_season_stadiums on nfl_team_season_stadiums(team, season);
        create index idx_nfl_roster_seasons_player on nfl_roster_seasons(player_id);
        create index idx_nfl_pro_stadium_events_player on nfl_pro_stadium_events(player_id);
        create index idx_nfl_stadium_geocode_status on nfl_stadium_geocode_cache(match_status);
        """
    )
    con.commit()
    con.close()


def write_unresolved(geocode_rows: list[dict[str, object]]) -> None:
    unresolved = [row for row in geocode_rows if row["match_status"] != "matched"]
    with UNRESOLVED_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["stadium_id", "stadium_name", "query_name", "match_status"],
        )
        writer.writeheader()
        for row in unresolved:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-downloads", action="store_true", help="Redownload nflverse schedules and Wikidata responses.")
    parser.add_argument("--sleep-seconds", type=float, default=0.5, help="Polite sleep after Wikidata request.")
    args = parser.parse_args()

    SCRATCH.mkdir(exist_ok=True)
    download(SCHEDULES_URL, SCHEDULES_CSV, refresh=args.refresh_downloads)
    schedule_rows = read_csv(SCHEDULES_CSV)
    team_season_rows = derive_team_season_stadiums(schedule_rows)
    wikidata_data = fetch_wikidata_stadiums(
        stadium_query_names(team_season_rows),
        refresh=args.refresh_downloads,
        sleep_seconds=args.sleep_seconds,
    )
    wikidata_qid_data = fetch_wikidata_qid_stadiums(refresh=args.refresh_downloads, sleep_seconds=args.sleep_seconds)
    matches = wikidata_matches(wikidata_data)
    qid_matches = wikidata_qid_matches(wikidata_qid_data)
    geocode_rows = build_stadium_geocode_rows(team_season_rows, matches, qid_matches)
    roster_rows = load_roster_seasons()
    player_events = derive_player_stadium_events(roster_rows, team_season_rows, geocode_rows)

    create_sql_cache(schedule_rows, team_season_rows, geocode_rows, roster_rows, player_events)
    write_unresolved(geocode_rows)

    matched_stadiums = [row for row in geocode_rows if row["match_status"] == "matched"]
    summary = {
        "nflverse_schedules_source": SCHEDULES_URL,
        "schedule_rows": len(schedule_rows),
        "schedule_min_year": min(int(row["season"]) for row in schedule_rows if stable_season(row.get("season"))),
        "schedule_max_year": max(int(row["season"]) for row in schedule_rows if stable_season(row.get("season"))),
        "team_season_stadium_rows": len(team_season_rows),
        "unique_stadiums": len(geocode_rows),
        "geocoded_stadiums": len(matched_stadiums),
        "unresolved_stadiums": len(geocode_rows) - len(matched_stadiums),
        "roster_season_rows": len(roster_rows),
        "derived_player_stadium_events": len(player_events),
        "sqlite_cache": repo_path(DB_PATH),
        "unresolved_csv": repo_path(UNRESOLVED_CSV),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
