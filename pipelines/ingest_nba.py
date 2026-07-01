#!/usr/bin/env python3
"""
Build the NBA geography enrichment cache.

The pipeline reads CC BY 4.0 hoopR NBA source snapshots, caches the raw parquet
files under data/raw/nba/, and writes a derived SQLite cache under scratch/.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
RAW_NBA = ROOT / "data/raw/nba/hoopr-nba-data"
DB_PATH = SCRATCH / "nba_enrichment.sqlite"
SUMMARY_PATH = SCRATCH / "nba_enrichment_summary.json"

HOOPR_REPO_API = "https://api.github.com/repos/sportsdataverse/hoopR-nba-data/contents"
HOOPR_RAW = "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/main"
HOOPR_LICENSE_URL = f"{HOOPR_RAW}/LICENSE.md"

GAZETTEER_ZIP_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip"
GAZETTEER_ZIP = ROOT / "data/raw/geography/census-gazetteer-2025/2025_Gaz_place_national.zip"

USER_AGENT = "HometownHeroesPipeline/0.1"


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
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


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def github_dir(path: str) -> list[dict[str, object]]:
    data = fetch_json(f"{HOOPR_REPO_API}/{path}?ref=main")
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected GitHub response for {path}: {data}")
    return [row for row in data if isinstance(row, dict)]


def download_manifest_and_license() -> None:
    download(HOOPR_LICENSE_URL, RAW_NBA / "LICENSE.md")
    for source_path, dest in (
        ("nba/rosters/nba_rosters_in_data_repo.csv", RAW_NBA / "manifests/nba_rosters_in_data_repo.csv"),
        (
            "nba/player_season_stats/nba_player_season_stats_in_data_repo.csv",
            RAW_NBA / "manifests/nba_player_season_stats_in_data_repo.csv",
        ),
    ):
        download(f"{HOOPR_RAW}/{source_path}", dest)


def download_parquet_dir(source_dir: str, local_dir: Path, pattern: str) -> list[Path]:
    regex = re.compile(pattern)
    cached = sorted(path for path in local_dir.glob("*.parquet") if regex.match(path.name))
    if cached:
        log(f"using cached parquet directory: {repo_path(local_dir)} ({len(cached)} files)")
        return cached

    rows = github_dir(source_dir)
    out: list[Path] = []
    for row in rows:
        name = str(row.get("name", ""))
        url = row.get("download_url")
        if row.get("type") != "file" or not url or not regex.match(name):
            continue
        dest = local_dir / name
        download(str(url), dest)
        out.append(dest)
    return sorted(out)


def download_sources() -> dict[str, list[Path]]:
    download_manifest_and_license()
    return {
        "player_season_stats": download_parquet_dir(
            "nba/player_season_stats/parquet",
            RAW_NBA / "player_season_stats/parquet",
            r"player_season_stats_20[0-9]{2}\.parquet$",
        ),
        "schedules": download_parquet_dir(
            "nba/schedules/parquet",
            RAW_NBA / "schedules/parquet",
            r"nba_schedule_20[0-9]{2}\.parquet$",
        ),
        "rosters": download_parquet_dir(
            "nba/rosters/parquet",
            RAW_NBA / "rosters/parquet",
            r"rosters_20[0-9]{2}\.parquet$",
        ),
    }


def clean_value(value: object) -> object:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def clean_records(df: pd.DataFrame) -> list[dict[str, object]]:
    cleaned = df.astype(object).where(pd.notna(df), None)
    return [{key: clean_value(value) for key, value in row.items()} for row in cleaned.to_dict("records")]


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


def read_player_team_seasons(paths: list[Path]) -> pd.DataFrame:
    frames = []
    columns = ["season", "athlete_id", "athlete_display_name", "team_id", "team_display_name", "stat_name", "value"]
    for path in paths:
        df = pd.read_parquet(path, columns=columns)
        frames.append(df[df["stat_name"] == "gamesPlayed"].copy())
    if not frames:
        return pd.DataFrame(columns=["season", "athlete_id", "display_name", "team_id", "team_display_name", "games_played"])
    stats = pd.concat(frames, ignore_index=True)
    stats["value"] = pd.to_numeric(stats["value"], errors="coerce")
    stats = stats[stats["value"].fillna(0) > 0]
    stats = stats.dropna(subset=["season", "athlete_id", "team_id"])
    grouped = (
        stats.groupby(["season", "athlete_id", "athlete_display_name", "team_id", "team_display_name"], dropna=False)["value"]
        .max()
        .reset_index()
    )
    grouped = grouped.rename(columns={"athlete_display_name": "display_name", "value": "games_played"})
    grouped["season"] = grouped["season"].astype(int)
    grouped["athlete_id"] = grouped["athlete_id"].astype(int).astype(str)
    grouped["team_id"] = grouped["team_id"].astype(int).astype(str)
    return grouped.sort_values(["season", "team_display_name", "display_name"])


def read_roster_players(paths: list[Path]) -> pd.DataFrame:
    frames = []
    columns = [
        "season",
        "athlete_id",
        "display_name",
        "date_of_birth",
        "birth_place_city",
        "birth_place_state",
        "birth_place_country",
    ]
    for path in paths:
        df = pd.read_parquet(path, columns=columns)
        frames.append(df.copy())
    if not frames:
        return pd.DataFrame(columns=columns)
    rosters = pd.concat(frames, ignore_index=True)
    rosters = rosters.dropna(subset=["athlete_id"])
    rosters["season"] = pd.to_numeric(rosters["season"], errors="coerce")
    rosters = rosters.sort_values(["athlete_id", "season"])
    rosters = rosters.drop_duplicates("athlete_id", keep="last")
    rosters["athlete_id"] = rosters["athlete_id"].astype(int).astype(str)
    return rosters


def build_players(player_team_seasons: pd.DataFrame, roster_players: pd.DataFrame) -> pd.DataFrame:
    latest_names = (
        player_team_seasons.sort_values(["athlete_id", "season"])
        .drop_duplicates("athlete_id", keep="last")[["athlete_id", "display_name"]]
    )
    season_bounds = (
        player_team_seasons.groupby("athlete_id", dropna=False)["season"].agg(first_season="min", last_season="max").reset_index()
    )
    season_players = pd.merge(season_bounds, latest_names, on="athlete_id", how="left")
    roster_subset = roster_players[
        [
            "athlete_id",
            "display_name",
            "date_of_birth",
            "birth_place_city",
            "birth_place_state",
            "birth_place_country",
        ]
    ].copy()
    players = pd.merge(season_players, roster_subset, on="athlete_id", how="outer", suffixes=("", "_roster"))
    players["display_name"] = players["display_name"].combine_first(players["display_name_roster"])
    players = players.drop(columns=["display_name_roster"])
    players["birth_year"] = players["date_of_birth"].astype(str).str.extract(r"^([0-9]{4})")[0]
    players["source"] = "hoopR NBA player season stats and roster snapshots"
    return players.sort_values(["display_name", "athlete_id"])


def read_team_home_venues(paths: list[Path]) -> pd.DataFrame:
    frames = []
    columns = [
        "season",
        "home_id",
        "home_display_name",
        "venue_id",
        "venue_full_name",
        "venue_address_city",
        "venue_address_state",
    ]
    for path in paths:
        df = pd.read_parquet(path, columns=columns)
        frames.append(df.copy())
    if not frames:
        return pd.DataFrame(columns=columns + ["home_games"])
    schedules = pd.concat(frames, ignore_index=True)
    schedules = schedules.dropna(subset=["season", "home_id", "venue_id"])
    schedules["season"] = schedules["season"].astype(int)
    schedules["home_id"] = schedules["home_id"].astype(int).astype(str)
    schedules["venue_id"] = schedules["venue_id"].astype(int).astype(str)
    counts = (
        schedules.groupby(
            [
                "season",
                "home_id",
                "home_display_name",
                "venue_id",
                "venue_full_name",
                "venue_address_city",
                "venue_address_state",
            ],
            dropna=False,
        )
        .size()
        .reset_index(name="home_games")
    )
    counts = counts.sort_values(["season", "home_id", "home_games"], ascending=[True, True, False])
    return counts.drop_duplicates(["season", "home_id"], keep="first")


def build_venue_geocode(team_home_venues: pd.DataFrame, gaz: dict[tuple[str, str], dict[str, str | float]]) -> pd.DataFrame:
    if team_home_venues.empty:
        return pd.DataFrame()
    venues = team_home_venues[
        ["venue_id", "venue_full_name", "venue_address_city", "venue_address_state"]
    ].drop_duplicates()
    rows = []
    for row in clean_records(venues):
        city = row.get("venue_address_city")
        state = row.get("venue_address_state")
        resolved = resolve_city(gaz, str(city) if city else None, str(state) if state else None, "USA")
        rows.append(
            {
                "venue_id": row.get("venue_id"),
                "venue_full_name": row.get("venue_full_name"),
                "city": city,
                "state": state,
                "country": "USA" if state else None,
                "geocode_status": resolved["status"],
                "latitude": resolved["latitude"],
                "longitude": resolved["longitude"],
                "source_label": resolved["source_label"],
            }
        )
    return pd.DataFrame(rows)


def build_birthplace_geocode(players: pd.DataFrame, gaz: dict[tuple[str, str], dict[str, str | float]]) -> pd.DataFrame:
    places = players[
        ["birth_place_city", "birth_place_state", "birth_place_country"]
    ].dropna(subset=["birth_place_city", "birth_place_state", "birth_place_country"])
    places = places.drop_duplicates()
    rows = []
    for row in clean_records(places):
        city = row.get("birth_place_city")
        state = row.get("birth_place_state")
        country = row.get("birth_place_country")
        resolved = resolve_city(gaz, str(city) if city else None, str(state) if state else None, str(country) if country else None)
        rows.append(
            {
                "birth_city": city,
                "birth_state": state,
                "birth_country": country,
                "geocode_status": resolved["status"],
                "latitude": resolved["latitude"],
                "longitude": resolved["longitude"],
                "source_label": resolved["source_label"],
            }
        )
    return pd.DataFrame(rows)


def build_pro_venue_events(player_team_seasons: pd.DataFrame, team_home_venues: pd.DataFrame) -> pd.DataFrame:
    if player_team_seasons.empty or team_home_venues.empty:
        return pd.DataFrame()
    joined = pd.merge(
        player_team_seasons,
        team_home_venues,
        left_on=["season", "team_id"],
        right_on=["season", "home_id"],
        how="inner",
    )
    grouped = (
        joined.groupby(
            ["athlete_id", "display_name", "team_id", "team_display_name", "venue_id", "venue_full_name"],
            dropna=False,
        )
        .agg(start_year=("season", "min"), end_year=("season", "max"), seasons=("season", "nunique"))
        .reset_index()
    )
    season_lists = (
        joined.groupby(["athlete_id", "team_id", "venue_id"], dropna=False)["season"]
        .apply(lambda s: ",".join(str(int(v)) for v in sorted(set(s.dropna()))))
        .reset_index(name="season_list")
    )
    grouped = pd.merge(grouped, season_lists, on=["athlete_id", "team_id", "venue_id"], how="left")
    grouped["source"] = "hoopR NBA player season stats + schedule home venue city"
    return grouped.sort_values(["display_name", "team_display_name", "start_year"])


def write_dataframe(con: sqlite3.Connection, df: pd.DataFrame, table: str) -> None:
    df.to_sql(table, con, if_exists="replace", index=False)


def create_indexes(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create index if not exists idx_nba_players_id on nba_players(athlete_id);
        create index if not exists idx_nba_team_seasons_player on nba_player_team_seasons(athlete_id);
        create index if not exists idx_nba_team_seasons_team on nba_player_team_seasons(team_id, season);
        create index if not exists idx_nba_pro_venue_player on nba_pro_venue_events(athlete_id);
        create index if not exists idx_nba_venue_geocode on nba_venue_geocode_cache(venue_id);
        """
    )


def write_summary(
    source_paths: dict[str, list[Path]],
    players: pd.DataFrame,
    player_team_seasons: pd.DataFrame,
    team_home_venues: pd.DataFrame,
    venue_geocode: pd.DataFrame,
    birthplace_geocode: pd.DataFrame,
    pro_venue_events: pd.DataFrame,
) -> dict[str, object]:
    summary = {
        "sqlite_database": repo_path(DB_PATH),
        "raw_cache_root": repo_path(RAW_NBA),
        "source": "sportsdataverse/hoopR-nba-data",
        "license": "CC BY 4.0",
        "downloaded_files": {key: len(paths) for key, paths in source_paths.items()},
        "players": int(len(players)),
        "players_with_birthplace_fields": int(players["birth_place_city"].notna().sum()) if "birth_place_city" in players else 0,
        "player_team_seasons": int(len(player_team_seasons)),
        "team_home_venue_seasons": int(len(team_home_venues)),
        "venues": int(len(venue_geocode)),
        "venues_geocoded": int((venue_geocode["geocode_status"] == "matched").sum()) if not venue_geocode.empty else 0,
        "birthplaces": int(len(birthplace_geocode)),
        "birthplaces_geocoded": int((birthplace_geocode["geocode_status"] == "matched").sum()) if not birthplace_geocode.empty else 0,
        "pro_venue_events": int(len(pro_venue_events)),
        "seasons": sorted(int(v) for v in player_team_seasons["season"].dropna().unique()),
        "event_confidence_note": "NBA pro locations are player/team seasons joined to the team's schedule-derived home venue city centroid.",
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    SCRATCH.mkdir(exist_ok=True)
    source_paths = download_sources()
    gaz = load_census_places()

    player_team_seasons = read_player_team_seasons(source_paths["player_season_stats"])
    roster_players = read_roster_players(source_paths["rosters"])
    players = build_players(player_team_seasons, roster_players)
    team_home_venues = read_team_home_venues(source_paths["schedules"])
    venue_geocode = build_venue_geocode(team_home_venues, gaz)
    birthplace_geocode = build_birthplace_geocode(players, gaz)
    pro_venue_events = build_pro_venue_events(player_team_seasons, team_home_venues)

    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    write_dataframe(con, players, "nba_players")
    write_dataframe(con, player_team_seasons, "nba_player_team_seasons")
    write_dataframe(con, team_home_venues, "nba_team_home_venues")
    write_dataframe(con, venue_geocode, "nba_venue_geocode_cache")
    write_dataframe(con, birthplace_geocode, "nba_birthplace_geocode_cache")
    write_dataframe(con, pro_venue_events, "nba_pro_venue_events")
    create_indexes(con)
    con.commit()
    con.close()

    summary = write_summary(
        source_paths,
        players,
        player_team_seasons,
        team_home_venues,
        venue_geocode,
        birthplace_geocode,
        pro_venue_events,
    )
    log(json.dumps(summary, indent=2))

    top_teams = Counter(player_team_seasons["team_display_name"]).most_common(5)
    log("top player-team season rows: " + json.dumps(top_teams))


if __name__ == "__main__":
    main()
