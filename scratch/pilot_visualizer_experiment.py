#!/usr/bin/env python3
"""
EXPERIMENT: Pilot HometownHeroes visualizer.

This is an explicitly tracked, agent-coded experiment for working out
map/search/list interaction kinks before promoting any durable app
architecture. Treat this as a playable prototype, not the final frontend.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("HH_DB_PATH", ROOT / "scratch" / "HometownHeroes.sqlite"))
GEOCODE_CACHE = ROOT / "scratch" / "place_geocode_cache.sqlite"
ATTRIBUTIONS_PATH = ROOT / "ATTRIBUTIONS.md"
HONORS_DB = ROOT / "scratch" / "player_honors.sqlite"
PRO_VENUE_STINTS_DB = ROOT / "scratch" / "pro_venue_stints.sqlite"
NFL_WIKIPEDIA_PAGES = ROOT / "data" / "raw" / "wikipedia" / "nfl_honors"

EVENT_TYPE_GROUPS = {
    "birthplace": ["born"],
    "high_school": ["attended_high_school"],
    "college": ["attended_college", "played_college"],
    "pro_sports": ["played_pro"],
}

EVENT_LAYER_BY_TYPE = {
    "born": "born",
    "attended_high_school": "attended_high_school",
    "attended_college": "college",
    "played_college": "college",
    "played_pro": "played_pro",
}

EVENT_LABELS = {
    "born": "Birthplace",
    "attended_high_school": "High School",
    "college": "College",
    "played_pro": "Pro Sports",
}

EVENT_COLORS = {
    "born": "#2563eb",
    "attended_high_school": "#16a34a",
    "college": "#7c3aed",
    "played_pro": "#dc2626",
}

TIMELINE_SECTION_LABELS = {
    "birthplace": "Birthplace",
    "high_school": "High School",
    "college": "College",
    "pro": "Pro Teams / Venues",
}

TIMELINE_SECTION_ORDER = ["birthplace", "high_school", "college", "pro"]

NFL_PASTTEAMS_BY_TITLE: dict[str, list[dict[str, str]]] | None = None
PRO_VENUE_STINTS_BY_SPORT: dict[str, list[dict[str, Any]]] | None = None

NFL_TEAM_NAMES_BY_ABBR = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "OAK": "Oakland Raiders",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SD": "San Diego Chargers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "STL": "St. Louis Rams",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
}


def nfl_team_name_for_season(team: str | None, season: Any = None) -> str:
    team = str(team or "")
    try:
        year = int(season)
    except (TypeError, ValueError):
        year = None
    if team == "LV" and year is not None and year <= 2019:
        return "Oakland Raiders"
    if team == "LAR" and year is not None and 1995 <= year <= 2015:
        return "St. Louis Rams"
    if team == "LAC" and year is not None and year <= 2016:
        return "San Diego Chargers"
    if team == "WAS" and year is not None:
        if year <= 2019:
            return "Washington Redskins"
        if year <= 2021:
            return "Washington Football Team"
    return NFL_TEAM_NAMES_BY_ABBR.get(team, team)


DEFAULT_QUERY = {
    "place": "San Jose, CA",
    "lat": 37.3382,
    "lon": -121.8863,
    "radius_mi": 50,
    "pro_start_year": 1970,
    "pro_end_year": 2026,
    "birth_start_year": 1800,
    "birth_end_year": 2026,
    "sports": ["MLB", "NFL", "NBA", "NHL"],
    "groups": [
        {"clauses": [{"kind": "birthplace"}]},
        {"clauses": [{"kind": "high_school"}]},
    ],
    "sort": "nearest",
    "hof_only": False,
    "all_star_only": False,
    "all_pro_only": False,
}


def repo_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_geocode_cache() -> None:
    GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(GEOCODE_CACHE) as con:
        con.execute(
            """
            create table if not exists place_geocode_cache (
                query text primary key,
                label text,
                latitude real,
                longitude real,
                raw_json text,
                source text
            )
            """
        )


def geocode_place(query: str) -> dict[str, Any]:
    init_geocode_cache()
    normalized = query.strip()
    if not normalized:
        raise ValueError("Place query is empty.")

    with sqlite3.connect(GEOCODE_CACHE) as con:
        row = con.execute(
            "select label, latitude, longitude, raw_json, source from place_geocode_cache where query = ?",
            (normalized,),
        ).fetchone()
        if row:
            return {
                "query": normalized,
                "label": row[0],
                "lat": row[1],
                "lon": row[2],
                "source": row[4],
                "cached": True,
            }

    params = urllib.parse.urlencode(
        {
            "q": normalized,
            "format": "jsonv2",
            "limit": 1,
        }
    )
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "HometownHeroesPilotVisualizer/0.1"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        results = json.loads(response.read().decode("utf-8"))
    if not results:
        raise ValueError(f"Could not geocode {normalized!r}.")

    result = results[0]
    label = result.get("display_name", normalized)
    lat = float(result["lat"])
    lon = float(result["lon"])
    with sqlite3.connect(GEOCODE_CACHE) as con:
        con.execute(
            """
            insert or replace into place_geocode_cache
            (query, label, latitude, longitude, raw_json, source)
            values (?, ?, ?, ?, ?, ?)
            """,
            (normalized, label, lat, lon, json.dumps(result), "Nominatim"),
        )
    return {"query": normalized, "label": label, "lat": lat, "lon": lon, "source": "Nominatim", "cached": False}


def suggest_places(query: str, limit: int = 12) -> dict[str, Any]:
    term = " ".join((query or "").strip().split())
    if len(term) < 2:
        return {"query": term, "suggestions": []}

    like = f"%{term.lower()}%"
    prefix = f"{term.lower()}%"
    con = connect()
    try:
        rows = con.execute(
            """
            with location_hits as (
                select
                    l.location_id,
                    l.label,
                    l.city,
                    l.state,
                    l.country,
                    l.latitude,
                    l.longitude,
                    l.location_kind,
                    count(e.event_id) as event_count,
                    count(distinct e.sport || ':' || e.player_id) as player_count,
                    min(case
                        when lower(l.label) = lower(?) then 0
                        when lower(l.label) like ? then 1
                        when lower(coalesce(l.city, '')) like ? then 2
                        else 3
                    end) as rank
                from locations l
                left join player_location_events e on e.location_id = l.location_id
                where l.latitude is not null
                  and l.longitude is not null
                  and (
                    lower(l.label) like ?
                    or lower(coalesce(l.city, '')) like ?
                    or lower(coalesce(l.state, '')) like ?
                    or lower(coalesce(l.country, '')) like ?
                  )
                group by
                    l.location_id, l.label, l.city, l.state, l.country,
                    l.latitude, l.longitude, l.location_kind
            )
            select *
            from location_hits
            order by rank, player_count desc, event_count desc, label
            limit ?
            """,
            (term, prefix, prefix, like, like, like, like, limit),
        ).fetchall()
    finally:
        con.close()

    suggestions = []
    seen: set[str] = set()
    for row in rows:
        key = str(row["label"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        detail_parts = [
            row["location_kind"].replace("_", " ").title(),
            f"{row['player_count']} players",
        ]
        if row["city"] and row["city"] != row["label"]:
            detail_parts.append(str(row["city"]))
        suggestions.append(
            {
                "location_id": row["location_id"],
                "label": row["label"],
                "detail": " · ".join(detail_parts),
                "lat": row["latitude"],
                "lon": row["longitude"],
                "player_count": row["player_count"],
                "event_count": row["event_count"],
                "location_kind": row["location_kind"],
            }
        )
    return {"query": term, "suggestions": suggestions}


def placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def bounding_box(lat: float, lon: float, radius_mi: float) -> dict[str, float]:
    lat_delta = radius_mi / 69.0
    lon_delta = radius_mi / (69.0 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "min_lat": lat - lat_delta,
        "max_lat": lat + lat_delta,
        "min_lon": lon - lon_delta,
        "max_lon": lon + lon_delta,
    }


def normalize_query(payload: dict[str, Any]) -> dict[str, Any]:
    query = dict(DEFAULT_QUERY)
    query.update(payload or {})
    query["lat"] = float(query["lat"])
    query["lon"] = float(query["lon"])
    query["radius_mi"] = max(1.0, min(float(query["radius_mi"]), 500.0))
    if "pro_start_year" not in query and "start_year" in query:
        query["pro_start_year"] = query["start_year"]
    if "pro_end_year" not in query and "end_year" in query:
        query["pro_end_year"] = query["end_year"]
    query["pro_start_year"] = int(query["pro_start_year"])
    query["pro_end_year"] = int(query["pro_end_year"])
    query["birth_start_year"] = int(query["birth_start_year"])
    query["birth_end_year"] = int(query["birth_end_year"])
    if query["pro_start_year"] > query["pro_end_year"]:
        query["pro_start_year"], query["pro_end_year"] = query["pro_end_year"], query["pro_start_year"]
    if query["birth_start_year"] > query["birth_end_year"]:
        query["birth_start_year"], query["birth_end_year"] = query["birth_end_year"], query["birth_start_year"]
    query["sports"] = [sport for sport in query.get("sports", []) if sport in {"MLB", "NFL", "NBA", "NHL"}] or [
        "MLB",
        "NFL",
        "NBA",
        "NHL",
    ]
    query["sort"] = str(query.get("sort", "nearest"))
    query["hof_only"] = bool(query.get("hof_only", False))
    query["all_star_only"] = bool(query.get("all_star_only", False))
    query["all_pro_only"] = bool(query.get("all_pro_only", False))

    groups = []
    source_groups = query.get("groups")
    if not source_groups and query.get("blocks"):
        source_groups = [{"clauses": [block]} for block in query["blocks"]]

    for group in source_groups or []:
        clauses = []
        for clause in group.get("clauses", []) if isinstance(group, dict) else []:
            kind = clause.get("kind") if isinstance(clause, dict) else clause
            if kind in EVENT_TYPE_GROUPS:
                clauses.append({"kind": kind})
        if clauses:
            groups.append({"clauses": clauses})
    query["groups"] = groups or [{"clauses": [{"kind": "birthplace"}]}]
    query.pop("blocks", None)
    query.pop("logic", None)
    return query


def clause_kinds_for_query(query: dict[str, Any]) -> list[str]:
    return sorted({clause["kind"] for group in query["groups"] for clause in group["clauses"]})


def event_types_for_query(query: dict[str, Any]) -> list[str]:
    return sorted({event_type for kind in clause_kinds_for_query(query) for event_type in EVENT_TYPE_GROUPS[kind]})


def fetch_candidate_events(query: dict[str, Any]) -> list[dict[str, Any]]:
    event_types = event_types_for_query(query)
    bbox = bounding_box(query["lat"], query["lon"], query["radius_mi"])
    params: list[Any] = [
        query["lat"],
        query["lat"],
        query["lon"],
        *query["sports"],
        *event_types,
        bbox["min_lat"],
        bbox["max_lat"],
        bbox["min_lon"],
        bbox["max_lon"],
        query["pro_end_year"],
        query["pro_start_year"],
        query["birth_start_year"],
        query["birth_end_year"],
        query["pro_end_year"],
        query["pro_start_year"],
        query["radius_mi"],
    ]
    sql = f"""
        with candidate_events as (
            select
                v.*,
                p.birth_date,
                p.birth_year,
                p.debut_year,
                p.final_year,
                pcs.pro_start_year,
                pcs.pro_end_year,
                pcs.pro_location_seasons,
                coalesce(phs.all_star_count, 0) as all_star_count,
                coalesce(phs.all_pro_count, 0) as all_pro_count,
                coalesce(phs.hof_inducted, 0) as hof_inducted,
                phs.hof_year,
                phs.honor_sources,
                pm.thumbnail_url,
                pm.source_page_url as media_source_page_url,
                pm.license as media_license,
                pm.license_url as media_license_url,
                pm.attribution_text as media_attribution,
                pm.usable as media_usable,
                3958.7613 * 2 * asin(
                    min(1.0, sqrt(
                        pow(sin(((v.latitude - ?) * 0.017453292519943295) / 2), 2) +
                        cos(? * 0.017453292519943295) *
                        cos(v.latitude * 0.017453292519943295) *
                        pow(sin(((v.longitude - ?) * 0.017453292519943295) / 2), 2)
                    ))
                ) as distance_mi
            from geocoded_player_location_events v
            left join players p
              on p.sport = v.sport and p.player_id = v.player_id
            left join pro_career_summary pcs
              on pcs.sport = v.sport and pcs.player_id = v.player_id
            left join player_honor_summary phs
              on phs.sport = v.sport and phs.player_id = v.player_id
            left join player_media pm
              on pm.sport = v.sport and pm.player_id = v.player_id and pm.usable = 1
            where v.sport in ({placeholders(query["sports"])})
              and v.event_type in ({placeholders(event_types)})
              and v.latitude between ? and ?
              and v.longitude between ? and ?
              and coalesce(p.debut_year, pcs.pro_start_year) is not null
              and coalesce(p.final_year, pcs.pro_end_year) is not null
              and coalesce(p.debut_year, pcs.pro_start_year) <= ?
              and coalesce(p.final_year, pcs.pro_end_year) >= ?
              and coalesce(p.birth_year, cast(substr(p.birth_date, 1, 4) as integer)) is not null
              and coalesce(p.birth_year, cast(substr(p.birth_date, 1, 4) as integer)) between ? and ?
              and (
                v.event_type <> 'played_pro'
                or v.start_year is null
                or v.end_year is null
                or (v.start_year <= ? and v.end_year >= ?)
              )
        )
        select *
        from candidate_events
        where distance_mi <= ?
        order by distance_mi, sport, display_name, event_type
    """
    con = connect()
    try:
        return [dict(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def clause_matches_for_event(event_type: str) -> list[str]:
    return [kind for kind, types in EVENT_TYPE_GROUPS.items() if event_type in types]


def qualify_players(rows: list[dict[str, Any]], query: dict[str, Any]) -> tuple[set[tuple[str, str]], dict[tuple[str, str], set[str]], dict[tuple[str, str], set[int]]]:
    by_player: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (row["sport"], row["player_id"])
        by_player.setdefault(key, set()).update(clause_matches_for_event(row["event_type"]))

    qualified = set()
    matched_groups: dict[tuple[str, str], set[int]] = {}
    for key, matches in by_player.items():
        for index, group in enumerate(query["groups"], start=1):
            required = {clause["kind"] for clause in group["clauses"]}
            if required.issubset(matches):
                qualified.add(key)
                matched_groups.setdefault(key, set()).add(index)
    return qualified, by_player, matched_groups


def clean_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    return value


def career_length_from_years(start_year: Any, end_year: Any, fallback: Any = None) -> int | None:
    try:
        start = int(start_year)
        end = int(end_year)
    except (TypeError, ValueError):
        return fallback
    if start <= 0 or end < start:
        return fallback
    return end - start + 1


def timeline_section_for_event(event_type: str) -> str | None:
    if event_type == "born":
        return "birthplace"
    if event_type == "attended_high_school":
        return "high_school"
    if event_type in {"attended_college", "played_college"}:
        return "college"
    if event_type == "played_pro":
        return "pro"
    return None


def year_span_label(start_year: Any, end_year: Any) -> str:
    if start_year is None and end_year is None:
        return "Years NA"
    if start_year is None:
        return f"through {end_year}"
    if end_year is None or end_year == start_year:
        return str(start_year)
    return f"{start_year}-{end_year}"


def timeline_year_sort(value: str) -> int:
    match = re.search(r"\b(?:19|20)\d{2}\b", value or "")
    return int(match.group(0)) if match else 9999


def timeline_item_label(row: sqlite3.Row) -> str:
    label = row["location_label"] or "Location NA"
    if row["event_type"] != "played_pro":
        return label

    source_key = row["source_key"] or ""
    parts = source_key.split("|")
    if row["sport"] == "NBA" and row["source"].startswith("Wikidata P54"):
        return label
    if row["sport"] == "NHL" and row["source"].startswith("NHL Stats REST"):
        return label
    if parts and parts[0]:
        team_name = parts[0].strip()
        if row["sport"] == "NFL":
            team_name = nfl_team_name_for_season(team_name, row["start_year"])
        elif row["sport"] == "NBA" and team_name.isdigit():
            team_name = f"NBA team {team_name}"
        return f"{team_name} / {label}" if label else team_name
    return label


def normalize_timeline_label(label: str) -> str:
    normalized = label.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"\bla\b", "los angeles", normalized)
    normalized = re.sub(r"[-_/]+", " ", normalized)
    normalized = re.sub(r"\bmain campus\b", "", normalized)
    normalized = re.sub(r"\bcampus\b", "", normalized)
    normalized = re.sub(r"\bthe\b", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def timeline_years(value: str) -> set[int]:
    if not value or value == "Years NA":
        return set()
    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", value)]
    if not years:
        return set()
    if "-" in value and len(years) >= 2:
        start, end = years[0], years[-1]
        if start <= end and end - start <= 100:
            return set(range(start, end + 1))
    return set(years)


def load_pro_venue_stints() -> dict[str, list[dict[str, Any]]]:
    global PRO_VENUE_STINTS_BY_SPORT
    if PRO_VENUE_STINTS_BY_SPORT is not None:
        return PRO_VENUE_STINTS_BY_SPORT

    by_sport: dict[str, list[dict[str, Any]]] = {}
    if not PRO_VENUE_STINTS_DB.exists():
        PRO_VENUE_STINTS_BY_SPORT = by_sport
        return by_sport

    con = sqlite3.connect(PRO_VENUE_STINTS_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select sport, team_name, venue_name, start_year, end_year
            from pro_venue_stints
            where team_name is not null and team_name != ''
              and venue_name is not null and venue_name != ''
            order by sport, team_name, start_year, end_year
            """
        ).fetchall()
    finally:
        con.close()

    for row in rows:
        by_sport.setdefault(row["sport"], []).append(
            {
                "team_key": normalize_timeline_label(row["team_name"]),
                "venue_name": row["venue_name"],
                "start_year": row["start_year"],
                "end_year": row["end_year"],
            }
        )
    PRO_VENUE_STINTS_BY_SPORT = by_sport
    return by_sport


def canonical_venue_for_team_years(sport: str, team_label: str, years_label: str) -> str | None:
    years = timeline_years(years_label)
    if not years:
        return None

    team_key = normalize_timeline_label(team_label)
    venues = {
        stint["venue_name"]
        for stint in load_pro_venue_stints().get(sport, [])
        if stint["team_key"] == team_key
        and any(int(stint["start_year"]) <= year <= int(stint["end_year"]) for year in years)
    }
    if len(venues) == 1:
        return next(iter(venues))
    return None


def timeline_year_bounds(item: dict[str, Any]) -> tuple[int | None, int | None]:
    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", item.get("years") or "")]
    if not years:
        return None, None
    start = years[0]
    if "-" in (item.get("years") or "") and len(years) >= 2:
        return start, years[-1]
    if (item.get("source") or "").startswith("Wikidata P54"):
        return start, 9999
    return start, start


def timeline_ranges_overlap_or_continue(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start, left_end = timeline_year_bounds(left)
    right_start, right_end = timeline_year_bounds(right)
    if left_start is None or left_end is None or right_start is None or right_end is None:
        return False
    return left_start <= right_end and right_start <= left_end


def timeline_item_rank(item: dict[str, Any]) -> tuple[int, int]:
    label = item["label"]
    source = item.get("source") or ""
    penalty = 0
    if " / " in label:
        penalty += 6
    if label.endswith("-Main Campus") or " Main Campus" in label:
        penalty += 4
    if "stadium" in label.lower() or "field" in label.lower():
        penalty += 2
    if source == "Wikipedia infobox pastteams":
        penalty -= 4
    return (penalty, len(label))


def team_prefix_for_timeline_item(item: dict[str, Any]) -> str | None:
    label = item["label"]
    if " / " not in label:
        return None
    return label.split(" / ", 1)[0].strip()


def sanitize_timeline_items(section: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(item["label"] != "Location NA" for item in items):
        items = [item for item in items if item["label"] != "Location NA"]

    if section == "pro":
        venue_items = [item for item in items if team_prefix_for_timeline_item(item)]
        cleaned = []
        for item in items:
            team_prefix = team_prefix_for_timeline_item(item)
            years = timeline_years(item["years"])
            if not team_prefix:
                team_key = normalize_timeline_label(item["label"])
                has_venue_duplicate = any(
                    normalize_timeline_label(team_prefix_for_timeline_item(venue)) == team_key
                    and (not years or not timeline_years(venue["years"]) or timeline_ranges_overlap_or_continue(venue, item))
                    for venue in venue_items
                )
                if has_venue_duplicate:
                    continue
            cleaned.append(item)
        items = cleaned

    best_by_label_and_years: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        dedupe_key = (normalize_timeline_label(item["label"]), item["years"])
        previous = best_by_label_and_years.get(dedupe_key)
        if previous is None or timeline_item_rank(item) < timeline_item_rank(previous):
            best_by_label_and_years[dedupe_key] = item
    return list(best_by_label_and_years.values())


def strip_wiki_markup(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value)
    value = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("[[", "").replace("]]", "")
    return " ".join(value.replace("*", "").strip().split())


def nfl_pastteam_label_from_line(line: str) -> str:
    label = strip_wiki_markup(line)
    label = re.sub(r"\s*\([^()]*(?:19|20)\d{2}[^()]*\)\s*$", "", label)
    label = re.sub(r"\s*\([–—\-\s]*\)\s*$", "", label)
    label = re.sub(r"\s*\((?:present|current|active)\)\s*$", "", label, flags=re.IGNORECASE)
    return label.strip()


def extract_nfl_years(value: str) -> str:
    years = []
    for match in re.finditer(r"\{\{\s*NFL Year\s*\|\s*(\d{4})(?:\s*\|\s*(\d{4}|present))?", value, flags=re.IGNORECASE):
        start = match.group(1)
        end = match.group(2)
        if end and end != start:
            years.append(f"{start}-{end}")
        else:
            years.append(start)
    if years:
        if len(years) == 2 and all("-" not in year for year in years) and re.search(r"[–—-]", value):
            return f"{years[0]}-{years[1]}"
        return ", ".join(years)
    year_matches = re.findall(r"\b(?:19|20)\d{2}\b", value)
    if len(year_matches) >= 2:
        return f"{year_matches[0]}-{year_matches[-1]}"
    if year_matches:
        return year_matches[0]
    return "Years NA"


def parse_nfl_pastteams(content: str) -> list[dict[str, str]]:
    match = re.search(r"^\|\s*pastteams\s*=(.*?)(?=^\|\s*\w+\s*=|\n\}\})", content, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return []
    teams = []
    for line in match.group(1).splitlines():
        if "*" not in line:
            continue
        label = nfl_pastteam_label_from_line(line)
        if not label:
            continue
        teams.append({"label": label, "years": extract_nfl_years(line), "source": "Wikipedia infobox pastteams"})
    return teams


def load_nfl_pastteams_by_title() -> dict[str, list[dict[str, str]]]:
    global NFL_PASTTEAMS_BY_TITLE
    if NFL_PASTTEAMS_BY_TITLE is not None:
        return NFL_PASTTEAMS_BY_TITLE

    teams_by_title: dict[str, list[dict[str, str]]] = {}
    if not NFL_WIKIPEDIA_PAGES.exists():
        NFL_PASTTEAMS_BY_TITLE = teams_by_title
        return teams_by_title

    for path in sorted(NFL_WIKIPEDIA_PAGES.glob("nfl_honors_pages_chunk_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):
            pages = pages.values()
        for page in pages:
            title = page.get("title")
            revisions = page.get("revisions") or []
            if not title or not revisions:
                continue
            content = revisions[0].get("slots", {}).get("main", {}).get("content", "")
            teams = parse_nfl_pastteams(content)
            if teams:
                teams_by_title[title] = teams

    NFL_PASTTEAMS_BY_TITLE = teams_by_title
    return teams_by_title


def nfl_pastteams_for_players(player_keys: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    nfl_ids = [player_id for sport, player_id in player_keys if sport == "NFL"]
    if not nfl_ids or not HONORS_DB.exists():
        return {}

    by_title = load_nfl_pastteams_by_title()
    if not by_title:
        return {}

    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    con = sqlite3.connect(HONORS_DB)
    con.row_factory = sqlite3.Row
    try:
        for start in range(0, len(nfl_ids), 700):
            chunk = nfl_ids[start : start + 700]
            rows = con.execute(
                f"""
                select source_player_id, article_title
                from nfl_wikipedia_title_map
                where source_player_id in ({placeholders(chunk)})
                  and article_title is not null
                  and article_title != ''
                """,
                chunk,
            ).fetchall()
            for row in rows:
                teams = by_title.get(row["article_title"])
                if teams:
                    out[("NFL", row["source_player_id"])] = teams
    finally:
        con.close()
    return out


def attach_player_timelines(player_rows: list[dict[str, Any]]) -> None:
    if not player_rows:
        return

    player_keys = [(row["sport"], row["player_id"]) for row in player_rows]
    nfl_pastteams = nfl_pastteams_for_players(player_keys)
    timelines: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {
        key: {section: [] for section in TIMELINE_SECTION_ORDER} for key in player_keys
    }
    seen: dict[tuple[str, str], set[tuple[str, str, str, str]]] = {key: set() for key in player_keys}

    con = connect()
    try:
        for start in range(0, len(player_keys), 350):
            chunk = player_keys[start : start + 350]
            key_filter = ", ".join("(?, ?)" for _ in chunk)
            params = [value for key in chunk for value in key]
            rows = con.execute(
                f"""
                select
                    e.sport,
                    e.player_id,
                    e.event_type,
                    e.start_year,
                    e.end_year,
                    e.duration_years,
                    e.source,
                    e.source_key,
                    e.notes,
                    l.label as location_label,
                    l.location_kind,
                    l.city,
                    l.state,
                    l.country
                from player_location_events e
                left join locations l using (location_id)
                where (e.sport, e.player_id) in ({key_filter})
                order by
                    e.sport,
                    e.player_id,
                    case e.event_type
                        when 'born' then 1
                        when 'attended_high_school' then 2
                        when 'attended_college' then 3
                        when 'played_college' then 4
                        when 'attended_school' then 5
                        when 'played_pro' then 6
                        else 7
                    end,
                    e.start_year is null,
                    e.start_year,
                    e.end_year,
                    l.label
                """,
                params,
                ).fetchall()
            for row in rows:
                key = (row["sport"], row["player_id"])
                section = timeline_section_for_event(row["event_type"])
                if section is None:
                    continue
                item = {
                    "label": timeline_item_label(row),
                    "years": year_span_label(row["start_year"], row["end_year"]),
                    "event_label": EVENT_LABELS.get(EVENT_LAYER_BY_TYPE.get(row["event_type"], row["event_type"]), row["event_type"]),
                    "source": row["source"],
                    "notes": row["notes"],
                }
                if section == "pro" and " / " not in item["label"]:
                    venue = canonical_venue_for_team_years(row["sport"], item["label"], item["years"])
                    if venue:
                        item["label"] = f"{item['label']} / {venue}"
                dedupe = (section, item["label"], item["years"], item["source"])
                if dedupe in seen[key]:
                    continue
                seen[key].add(dedupe)
                timelines[key][section].append(item)
    finally:
        con.close()

    for key, teams in nfl_pastteams.items():
        if key not in timelines:
            continue
        for team in teams:
            item = {
                "label": team["label"],
                "years": team["years"],
                "event_label": "Pro Sports",
                "source": team["source"],
                "notes": "Cached from Wikipedia infobox pastteams; may include off-roster/practice-squad markers from the source article.",
            }
            venue = canonical_venue_for_team_years(key[0], item["label"], item["years"])
            if venue:
                item["label"] = f"{item['label']} / {venue}"
            dedupe = ("pro", item["label"], item["years"], item["source"])
            if dedupe in seen[key]:
                continue
            seen[key].add(dedupe)
            timelines[key]["pro"].append(item)

    for row in player_rows:
        key = (row["sport"], row["player_id"])
        sections = []
        for section in TIMELINE_SECTION_ORDER:
            items = sanitize_timeline_items(section, timelines[key][section])
            if items:
                items.sort(key=lambda item: (timeline_year_sort(item["years"]), item["label"]))
                sections.append({"key": section, "label": TIMELINE_SECTION_LABELS[section], "items": items[:60]})
        row["timeline"] = sections


def render_inline_markdown(value: str) -> str:
    parts = re.split(r"(`[^`]*`)", value)
    rendered: list[str] = []
    for part in parts:
        if part.startswith("`") and part.endswith("`"):
            rendered.append(f"<code>{html_lib.escape(part[1:-1])}</code>")
            continue
        escaped = html_lib.escape(part)
        escaped = re.sub(
            r"(https?://[^\s<]+)",
            lambda match: (
                f'<a href="{html_lib.escape(match.group(1), quote=True)}" '
                f'target="_blank" rel="noreferrer">{html_lib.escape(match.group(1))}</a>'
            ),
            escaped,
        )
        rendered.append(escaped)
    return "".join(rendered)


def render_markdown_fragment(text: str) -> str:
    blocks: list[str] = []
    list_items: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{render_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{render_inline_markdown(line[3:].strip())}</h2>")
            continue
        if line.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h1>{render_inline_markdown(line[2:].strip())}</h1>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            list_items.append(render_inline_markdown(line[2:].strip()))
            continue
        flush_list()
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def render_text_page(title: str, text: str) -> bytes:
    escaped_title = html_lib.escape(title)
    rendered_body = render_markdown_fragment(text)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    body {{
      margin: 0;
      padding: 32px;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #172033;
      background: #f7f9fc;
    }}
    main {{
      max-width: 880px;
      margin: 0 auto;
      background: #fff;
      border: 1px solid #e3e7ee;
      border-radius: 8px;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 24px;
      font-size: 28px;
      line-height: 1.15;
    }}
    h2 {{
      margin: 32px 0 12px;
      font-size: 18px;
      line-height: 1.25;
    }}
    p, li {{
      line-height: 1.55;
    }}
    p {{
      margin: 0 0 16px;
    }}
    ul {{
      margin: 0 0 18px;
      padding-left: 22px;
    }}
    code {{
      padding: 1px 5px;
      border-radius: 5px;
      background: #eef2f7;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }}
    a {{ color: #1663d6; }}
  </style>
</head>
<body>
  <main>{rendered_body}</main>
</body>
</html>
""".encode("utf-8")


def build_response(query: dict[str, Any]) -> dict[str, Any]:
    event_types = event_types_for_query(query)
    rows = fetch_candidate_events(query)
    qualified, player_blocks, matched_groups = qualify_players(rows, query)
    filtered = [row for row in rows if (row["sport"], row["player_id"]) in qualified]

    players: dict[tuple[str, str], dict[str, Any]] = {}
    dots: dict[tuple[str, str], dict[str, Any]] = {}
    for row in filtered:
        key = (row["sport"], row["player_id"])
        player = players.setdefault(
            key,
            {
                "sport": row["sport"],
                "player_id": row["player_id"],
                "display_name": row["display_name"],
                "nearest_mi": row["distance_mi"],
                "birth_date": row["birth_date"],
                "birth_year": row["birth_year"],
                "debut_year": row["debut_year"],
                "final_year": row["final_year"],
                "matched_blocks": sorted(player_blocks.get(key, set())),
                "matched_groups": sorted(matched_groups.get(key, set())),
                "matched_types": set(),
                "matched_locations": set(),
                "location_keys": set(),
                "first_year": None,
                "last_year": None,
                "pro_start_year": row["pro_start_year"],
                "pro_end_year": row["pro_end_year"],
                "pro_career_length": career_length_from_years(row["debut_year"], row["final_year"], row["pro_location_seasons"]),
                "all_star_count": row["all_star_count"],
                "all_pro_count": row["all_pro_count"],
                "hof_inducted": bool(row["hof_inducted"]),
                "hof_year": row["hof_year"],
                "honor_sources": row["honor_sources"],
                "media": {
                    "thumbnail_url": row["thumbnail_url"],
                    "source_page_url": row["media_source_page_url"],
                    "license": row["media_license"],
                    "license_url": row["media_license_url"],
                    "attribution": row["media_attribution"],
                    "usable": bool(row["media_usable"]),
                },
            },
        )
        player["nearest_mi"] = min(player["nearest_mi"], row["distance_mi"])
        event_layer = EVENT_LAYER_BY_TYPE.get(row["event_type"], row["event_type"])
        player["matched_types"].add(event_layer)
        player["matched_locations"].add(row["location_label"])
        player["location_keys"].add(f"{row['location_id']}|{event_layer}")
        for field, target in (("start_year", "first_year"), ("end_year", "last_year")):
            if row[field] is not None:
                if player[target] is None:
                    player[target] = row[field]
                elif target == "first_year":
                    player[target] = min(player[target], row[field])
                else:
                    player[target] = max(player[target], row[field])

        dot_key = (row["location_id"], event_layer)
        dot = dots.setdefault(
            dot_key,
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]},
                "properties": {
                    "location_id": row["location_id"],
                    "label": row["location_label"],
                    "event_type": event_layer,
                    "event_label": EVENT_LABELS.get(event_layer, event_layer),
                    "color": EVENT_COLORS.get(event_layer, "#6b7280"),
                    "filter_key": f"{row['location_id']}|{event_layer}",
                    "players": set(),
                    "events": 0,
                    "nearest_mi": row["distance_mi"],
                },
            },
        )
        dot["properties"]["players"].add(f"{row['sport']}:{row['player_id']}")
        dot["properties"]["events"] += 1
        dot["properties"]["nearest_mi"] = min(dot["properties"]["nearest_mi"], row["distance_mi"])

    player_rows = []
    for player in players.values():
        player["matched_types"] = sorted(player["matched_types"])
        player["matched_locations"] = sorted(player["matched_locations"])[:12]
        player["location_keys"] = sorted(player["location_keys"])
        for field in ("nearest_mi", "pro_career_length"):
            player[field] = clean_number(player[field])
        player_rows.append(player)

    if query["hof_only"]:
        player_rows = [player for player in player_rows if player["hof_inducted"]]
    if query["all_star_only"]:
        player_rows = [player for player in player_rows if (player["all_star_count"] or 0) > 0]
    if query["all_pro_only"]:
        player_rows = [player for player in player_rows if (player["all_pro_count"] or 0) > 0]

    sort = query["sort"]
    if sort == "year":
        player_rows.sort(key=lambda row: (row["first_year"] is None, row["first_year"] or 9999, row["display_name"]))
    elif sort == "career_length":
        player_rows.sort(key=lambda row: (-(row["pro_career_length"] or 0), row["display_name"]))
    elif sort == "all_star":
        player_rows.sort(key=lambda row: (-(row["all_star_count"] or 0), row["display_name"]))
    elif sort == "all_pro":
        player_rows.sort(key=lambda row: (-(row["all_pro_count"] or 0), row["display_name"]))
    else:
        player_rows.sort(key=lambda row: (row["nearest_mi"] or 9999, row["display_name"]))

    visible_players = {f"{row['sport']}:{row['player_id']}" for row in player_rows}
    features = []
    for dot in dots.values():
        props = dot["properties"]
        props["players"] = len(props["players"] & visible_players)
        if props["players"] == 0:
            continue
        props["nearest_mi"] = round(float(props["nearest_mi"]), 2)
        features.append(dot)
    features.sort(key=lambda feature: (feature["properties"]["nearest_mi"], feature["properties"]["event_type"]))

    returned_players = player_rows[:1000]
    attach_player_timelines(returned_players)

    return {
        "query": query,
        "available_event_types": event_types,
        "summary": {
            "players": len(player_rows),
            "location_features": len(features),
            "raw_events": len(rows),
            "qualified_events": len(filtered),
            "data_notes": [
                "NFL pro rows are roster-season/home-stadium associations for 1999-current.",
                "NBA pro rows are player/team seasons joined to schedule-derived venue city centroids.",
                "NHL pro rows are player/team seasons joined to team city centroids.",
                "MLB non-US birthplace coordinates use GeoNames cities500 centroids; GeoNames is licensed CC BY 4.0.",
                "MLB All-Star and HOF fields come from Lahman; NFL and NBA HOF fields come from Wikidata; NHL HOF fields come from NHL Records; NFL Pro Bowl/All-Pro and NBA All-Star/All-NBA counts come from Wikipedia infobox career highlights.",
            ],
        },
        "players": returned_players,
        "locations_geojson": {"type": "FeatureCollection", "features": features},
    }


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Geospatial sports analytics visualizer for athlete hometown, school, college, and professional-location associations.">
  <meta name="theme-color" content="#f7f9fc">
  <title>HometownHeroes Geospatial Sports Analytics</title>
  <link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="%231663d6"/><path d="M32 9l6.9 14 15.5 2.3-11.2 10.9 2.6 15.4L32 44.3 18.2 51.6l2.6-15.4L9.6 25.3 25.1 23z" fill="white"/></svg>'>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      color-scheme: light;
      --query-panel-width: 430px;
      --players-panel-width: clamp(300px, 22vw, 390px);
      --ink: #172033;
      --muted: #657085;
      --line: #e3e7ee;
      --panel: #ffffff;
      --bg: #f7f9fc;
      --accent: #1663d6;
      --field: #f1f4f8;
      --chip: #eef2f7;
      --chip-ink: #374151;
      --float: rgba(255,255,255,.94);
      --focus: rgba(22, 99, 214, .22);
      --shadow: rgba(15, 23, 42, .12);
      --sidebar-shadow: rgba(15, 23, 42, .08);
    }
    body.theme-dark {
      color-scheme: dark;
      --ink: #e8edf5;
      --muted: #9ca8bb;
      --line: #263244;
      --panel: #111827;
      --bg: #0b1120;
      --accent: #5aa2ff;
      --field: #1b2535;
      --chip: #202c3e;
      --chip-ink: #d8e0ee;
      --float: rgba(17,24,39,.94);
      --focus: rgba(90, 162, 255, .28);
      --shadow: rgba(0, 0, 0, .3);
      --sidebar-shadow: rgba(0, 0, 0, .32);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      letter-spacing: 0;
    }
    .app {
      height: 100vh;
      position: relative;
      overflow: hidden;
    }
    .query-panel {
      position: fixed;
      z-index: 650;
      top: 14px;
      left: 14px;
      width: min(var(--query-panel-width), calc(100vw - var(--players-panel-width) - 56px));
      max-height: calc(100vh - 28px);
      display: grid;
      grid-template-rows: auto auto;
      min-width: 0;
      overflow: auto;
      background: var(--float);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 12px 32px var(--sidebar-shadow);
      backdrop-filter: blur(10px);
    }
    .players-panel {
      position: fixed;
      z-index: 650;
      top: 14px;
      right: 14px;
      bottom: 14px;
      width: var(--players-panel-width);
      display: grid;
      grid-template-rows: auto 1fr;
      min-width: 0;
      overflow: hidden;
      background: var(--float);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 12px 32px var(--sidebar-shadow);
      backdrop-filter: blur(10px);
      transition: transform .18s ease;
    }
    .players-panel.collapsed {
      transform: translateX(calc(100% - 44px));
    }
    .players-panel.collapsed .results-bar,
    .players-panel.collapsed .list {
      opacity: 0;
      pointer-events: none;
    }
    .panel-toggle {
      position: absolute;
      z-index: 3;
      left: 6px;
      top: 8px;
      width: 32px;
      min-height: 32px;
      padding: 0;
      border-radius: 8px;
      color: var(--ink);
      background: var(--field);
      box-shadow: 0 2px 8px var(--shadow);
    }
    .controls {
      padding: 16px;
      display: grid;
      gap: 11px;
      border-bottom: 1px solid var(--line);
    }
    .title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .title-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }
    h1 {
      font-size: 25px;
      line-height: 1.1;
      margin: 0;
      font-weight: 750;
    }
    .badge {
      font-size: 11px;
      color: var(--muted);
      background: transparent;
      border: 0;
      padding: 0;
      white-space: nowrap;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .grid2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    label {
      display: grid;
      gap: 4px;
      font-size: 10px;
      color: var(--muted);
      font-weight: 750;
      text-transform: uppercase;
    }
    input, select, button {
      min-height: 38px;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
      background: var(--field);
      color: var(--ink);
    }
    input:focus, select:focus, button:focus-visible {
      outline: 2px solid var(--focus);
      outline-offset: 1px;
    }
    button {
      cursor: pointer;
      font-weight: 700;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    button.icon {
      width: 38px;
      padding: 0;
      display: inline-grid;
      place-items: center;
    }
    button.link {
      min-height: 28px;
      border: 0;
      background: transparent;
      color: var(--accent);
      padding: 3px 0;
    }
    .theme-toggle {
      min-height: 30px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      color: var(--muted);
      background: transparent;
      border: 0;
      font-size: 12px;
      font-weight: 750;
    }
    .theme-toggle .toggle-track {
      width: 34px;
      height: 18px;
      border-radius: 999px;
      background: var(--field);
      position: relative;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .theme-toggle .toggle-track::after {
      content: "";
      position: absolute;
      width: 14px;
      height: 14px;
      top: 2px;
      left: 2px;
      border-radius: 50%;
      background: var(--panel);
      box-shadow: 0 1px 3px var(--shadow);
      transition: transform .16s ease;
    }
    body.theme-dark .theme-toggle .toggle-track::after {
      transform: translateX(16px);
    }
    .query-toggle {
      display: none;
      min-height: 30px;
      padding: 4px 8px;
      color: var(--accent);
      background: var(--field);
      font-size: 12px;
      font-weight: 800;
    }
    .query-advanced {
      display: grid;
      gap: 11px;
    }
    .place-wrap {
      position: relative;
      display: grid;
      gap: 4px;
    }
    .suggestions {
      position: absolute;
      z-index: 20;
      top: calc(100% + 4px);
      left: 0;
      right: 0;
      max-height: 230px;
      overflow: auto;
      display: none;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 12px 26px var(--shadow);
    }
    .suggestions.open { display: grid; }
    .suggestion {
      width: 100%;
      min-height: 42px;
      display: grid;
      gap: 2px;
      padding: 7px 8px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      text-align: left;
      font-weight: 650;
    }
    .suggestion:hover,
    .suggestion.active {
      background: var(--field);
    }
    .suggestion span {
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .filter-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .filter-group {
      display: grid;
      gap: 8px;
      align-content: start;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }
    .filter-title {
      font-size: 11px;
      line-height: 1;
      color: var(--muted);
      font-weight: 800;
      text-transform: uppercase;
    }
    .checks {
      display: flex;
      gap: 7px 10px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 13px;
    }
    .checks label {
      display: flex;
      gap: 5px;
      align-items: center;
      text-transform: none;
      font-size: 13px;
      font-weight: 600;
      color: var(--ink);
      min-height: 20px;
    }
    .checks input { min-height: auto; }
    .query-builder {
      display: grid;
      gap: 9px;
      padding: 2px 0 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }
    .query-help {
      font-size: 12px;
      line-height: 1.35;
      color: var(--muted);
    }
    .group {
      display: grid;
      gap: 7px;
      padding: 0 0 0 10px;
      border: 0;
      border-left: 2px solid var(--line);
      border-radius: 0;
      background: transparent;
    }
    .group-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
      font-weight: 750;
    }
    .clause {
      display: grid;
      grid-template-columns: 1fr 38px;
      gap: 7px;
    }
    .or-divider {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
    }
    .or-divider::before,
    .or-divider::after {
      content: "";
      height: 1px;
      background: var(--line);
      flex: 1;
    }
    .meta {
      padding: 8px 16px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .results-bar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: 8px;
      padding: 12px 12px 12px 46px;
      border-bottom: 1px solid var(--line);
      background: transparent;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .results-title {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .results-title strong {
      font-size: 14px;
    }
    .results-title span {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .results-actions {
      display: flex;
      gap: 6px;
      align-items: center;
      justify-content: flex-end;
      flex-shrink: 0;
    }
    #showPlayers {
      display: none;
    }
    .list {
      overflow: auto;
      padding: 0 12px 14px;
      display: grid;
      gap: 0;
      align-content: start;
      min-height: 180px;
    }
    .empty {
      padding: 12px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--field);
    }
    .player {
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 12px 0;
      background: transparent;
      display: grid;
      grid-template-columns: 54px 1fr;
      gap: 9px;
      align-items: start;
      cursor: pointer;
    }
    .player.expanded {
      background: color-mix(in srgb, var(--field) 42%, transparent);
      margin: 0 -8px;
      padding: 12px 8px;
      border-radius: 8px;
    }
    .avatar {
      width: 54px;
      height: 54px;
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      background: var(--field);
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: 15px;
      font-weight: 800;
      line-height: 1;
      text-transform: uppercase;
    }
    .avatar img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .player-body {
      min-width: 0;
      display: grid;
      gap: 5px;
    }
    .player-head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: baseline;
    }
    .player strong {
      font-size: 14px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .dist {
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
    }
    .chips {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }
    .chip {
      font-size: 11px;
      border-radius: 999px;
      background: var(--chip);
      padding: 3px 6px;
      color: var(--chip-ink);
    }
    .small {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.35;
    }
    .timeline {
      display: none;
      gap: 9px;
      margin-top: 4px;
      padding-top: 9px;
      border-top: 1px solid var(--line);
    }
    .player.expanded .timeline {
      display: grid;
    }
    .timeline-section {
      display: grid;
      gap: 4px;
    }
    .timeline-section strong {
      font-size: 11px;
      color: var(--ink);
      text-transform: uppercase;
      letter-spacing: .02em;
    }
    .timeline-list {
      display: grid;
      gap: 3px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .timeline-list li {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .timeline-years {
      color: var(--ink);
      font-weight: 700;
    }
    .photo-credit a {
      color: var(--muted);
    }
    .source-link {
      color: var(--accent);
      font-size: 12px;
      font-weight: 750;
      text-decoration: none;
      white-space: nowrap;
    }
    main {
      position: relative;
      width: 100%;
      height: 100%;
      min-width: 0;
    }
    #map {
      position: absolute;
      inset: 0;
    }
    body.theme-dark .leaflet-tile-pane {
      filter: invert(.9) hue-rotate(180deg) saturate(.75) brightness(.72) contrast(1.08);
    }
    body.theme-dark .leaflet-marker-pane,
    body.theme-dark .leaflet-overlay-pane,
    body.theme-dark .leaflet-shadow-pane {
      filter: none;
    }
    .leaflet-bottom.leaflet-left {
      left: calc(var(--query-panel-width) + 24px);
      bottom: 12px;
    }
    .map-status {
      position: absolute;
      z-index: 500;
      left: calc(var(--query-panel-width) + 86px);
      bottom: 12px;
      max-width: min(520px, calc(100vw - var(--players-panel-width) - 44px));
      background: var(--float);
      border: 0;
      border-radius: 999px;
      padding: 8px 10px;
      font-size: 12px;
      color: var(--muted);
      box-shadow: 0 6px 16px var(--shadow);
    }
    .map-status strong {
      display: block;
      color: var(--ink);
      font-size: 13px;
      margin-bottom: 2px;
    }
    .map-status button {
      min-height: 28px;
      margin-top: 6px;
      padding: 4px 8px;
    }
    .legend {
      position: absolute;
      z-index: 500;
      right: calc(var(--players-panel-width) + 28px);
      top: 12px;
      background: var(--float);
      border: 0;
      border-radius: 10px;
      padding: 8px 10px;
      display: grid;
      gap: 5px;
      font-size: 12px;
      box-shadow: 0 6px 16px var(--shadow);
    }
    .legend-row {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }
    @media (max-width: 900px) {
      .query-panel {
        top: 8px;
        left: 8px;
        right: 8px;
        width: auto;
        max-height: 48vh;
      }
      .query-panel.collapsed .query-advanced {
        display: none;
      }
      .query-panel.collapsed .controls {
        border-bottom: 0;
      }
      .query-panel.collapsed .meta {
        display: none;
      }
      .title {
        gap: 8px;
      }
      h1 {
        font-size: 22px;
      }
      .query-toggle {
        display: inline-grid;
        place-items: center;
      }
      .controls {
        padding: 12px;
        gap: 9px;
      }
      input, select, button {
        min-height: 34px;
      }
      .players-panel {
        left: 8px;
        right: 8px;
        top: auto;
        bottom: 8px;
        width: auto;
        height: 43vh;
      }
      .players-panel.collapsed {
        transform: translateY(calc(100% - 46px));
      }
      .legend {
        display: none;
      }
      .leaflet-bottom.leaflet-left {
        left: 8px;
        bottom: calc(43vh + 18px);
      }
      .map-status {
        display: none;
      }
      .controls { padding-bottom: 10px; }
      .grid2 { grid-template-columns: 1fr 1fr; }
      .filter-row { grid-template-columns: 1fr; }
      .player {
        grid-template-columns: 48px 1fr;
      }
      .avatar {
        width: 48px;
        height: 48px;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <main>
      <div id="map"></div>
      <div class="legend" id="legend"></div>
      <div class="map-status" id="mapStatus">Click the map to move the search center. Click a colored dot to filter the Players list.</div>
    </main>
    <aside class="query-panel" id="queryPanel">
      <section class="controls">
        <div class="title">
          <h1>HometownHeroes</h1>
          <div class="title-actions">
            <button id="toggleQuery" class="query-toggle" type="button" title="Show query filters" aria-expanded="true">Filters</button>
            <button id="themeToggle" class="theme-toggle" type="button" title="Toggle light or dark mode" aria-pressed="false">
              <span class="toggle-track" aria-hidden="true"></span>
              <span id="themeLabel">Light</span>
            </button>
          </div>
        </div>
        <div class="row">
          <div class="place-wrap">
            <label>Place
              <input id="place" value="San Jose, CA" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="placeSuggestions">
            </label>
            <div id="placeSuggestions" class="suggestions" role="listbox"></div>
          </div>
          <button id="geocode" class="primary" title="Geocode typed place">Go</button>
        </div>
        <div class="query-advanced" id="queryAdvanced">
          <div class="grid2">
            <label>Radius Miles
              <input id="radius" type="number" min="1" max="500" value="50">
            </label>
            <label>Sort List
              <select id="sort">
                <option value="nearest">Nearest</option>
                <option value="year">Year</option>
                <option value="career_length">Career length</option>
                <option value="all_star">All-Star / Pro Bowl</option>
                <option value="all_pro">All-Pro / All-NBA</option>
              </select>
            </label>
          </div>
          <div class="grid2">
            <label>Pro Career Start
              <input id="proStartYear" type="number" value="1970">
            </label>
            <label>Pro Career End
              <input id="proEndYear" type="number" value="2026">
            </label>
          </div>
          <div class="grid2">
            <label>Birth Year Start
              <input id="birthStartYear" type="number" value="1800">
            </label>
            <label>Birth Year End
              <input id="birthEndYear" type="number" value="2026">
            </label>
          </div>
          <div class="filter-row">
            <section class="filter-group" aria-label="Sport filters">
              <div class="filter-title">Sports</div>
              <div class="checks">
                <label><input type="checkbox" id="sportMLB" checked> MLB</label>
                <label><input type="checkbox" id="sportNFL" checked> NFL</label>
                <label><input type="checkbox" id="sportNBA" checked> NBA</label>
                <label><input type="checkbox" id="sportNHL" checked> NHL</label>
              </div>
            </section>
            <section class="filter-group" aria-label="Honor filters">
              <div class="filter-title">Honors</div>
              <div class="checks">
                <label><input type="checkbox" id="allStarOnly"> All-Star / Pro Bowl</label>
                <label><input type="checkbox" id="allProOnly"> All-Pro / All-NBA</label>
                <label><input type="checkbox" id="hofOnly"> HOF only</label>
              </div>
            </section>
          </div>
          <div class="query-builder">
            <div class="row">
              <div class="query-help">Any group may match. Inside one group, every condition is required.</div>
              <button id="addGroup" class="icon" title="Add OR group">+</button>
            </div>
            <div id="groups"></div>
          </div>
          <button id="run" class="primary">Run Query</button>
        </div>
      </section>
      <section class="meta" id="meta">Ready.</section>
    </aside>
    <aside class="players-panel" id="playersPanel">
      <button id="togglePlayers" class="panel-toggle" type="button" title="Collapse players panel" aria-expanded="true">›</button>
      <section class="results-bar" id="resultsBar">
        <div class="results-title">
          <strong id="resultsTitle">Players</strong>
          <span id="resultsSubtitle">Run a query to populate the side list.</span>
        </div>
        <div class="results-actions">
          <a class="source-link" href="/attributions" target="_blank" rel="noreferrer">Attributions</a>
          <button id="clearLocationFilter" class="link" title="Show players from all map points">All dots</button>
          <button id="showPlayers" class="primary" title="Jump to player list">Players</button>
        </div>
      </section>
      <section class="list" id="list"></section>
    </aside>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const defaultQuery = __DEFAULT_QUERY__;
    const eventColors = __EVENT_COLORS__;
    const eventLabels = __EVENT_LABELS__;
    let center = { lat: defaultQuery.lat, lon: defaultQuery.lon };
    let radiusLayer = null;
    let resultLayer = L.layerGroup();
    let centerMarker = null;
    let groups = defaultQuery.groups.map(group => group.clauses.map(clause => clause.kind));
    let latestData = null;
    let activeLocationFilter = null;
    let placeSuggestions = [];
    let activeSuggestionIndex = -1;
    let suggestionTimer = null;
    let suggestionRequestId = 0;
    let currentTheme = localStorage.getItem('hhTheme') || 'light';
    let tileLayer = null;

    const map = L.map('map', { zoomControl: false }).setView([center.lat, center.lon], 9);
    L.control.zoom({ position: 'bottomleft' }).addTo(map);
    const tileSpec = {
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      options: { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }
    };
    resultLayer.addTo(map);

    function applyTheme(theme) {
      currentTheme = theme === 'dark' ? 'dark' : 'light';
      document.body.classList.toggle('theme-dark', currentTheme === 'dark');
      localStorage.setItem('hhTheme', currentTheme);
      const toggle = document.getElementById('themeToggle');
      const label = document.getElementById('themeLabel');
      if (toggle) toggle.setAttribute('aria-pressed', currentTheme === 'dark' ? 'true' : 'false');
      if (label) label.textContent = currentTheme === 'dark' ? 'Dark' : 'Light';
      if (tileLayer) tileLayer.remove();
      tileLayer = L.tileLayer(tileSpec.url, tileSpec.options).addTo(map);
      tileLayer.bringToBack();
      if (centerMarker || radiusLayer) updateCenterLayers();
    }

    function optionLabel(kind) {
      return {
        birthplace: 'Birthplace',
        high_school: 'High School',
        college: 'College',
        pro_sports: 'Pro Sports'
      }[kind] || kind;
    }

    function renderGroups() {
      const root = document.getElementById('groups');
      root.innerHTML = '';
      groups.forEach((clauses, groupIndex) => {
        if (groupIndex > 0) {
          const divider = document.createElement('div');
          divider.className = 'or-divider';
          divider.textContent = 'OR';
          root.appendChild(divider);
        }

        const group = document.createElement('section');
        group.className = 'group';

        const head = document.createElement('div');
        head.className = 'group-head';
        const label = document.createElement('span');
        label.textContent = `Group ${groupIndex + 1}: all rows below are AND`;
        const actions = document.createElement('div');
        actions.className = 'results-actions';
        const addClause = document.createElement('button');
        addClause.className = 'icon';
        addClause.title = 'Add AND condition';
        addClause.textContent = '+';
        addClause.addEventListener('click', () => {
          groups[groupIndex].push('birthplace');
          renderGroups();
        });
        const removeGroup = document.createElement('button');
        removeGroup.className = 'icon';
        removeGroup.title = 'Remove OR group';
        removeGroup.textContent = '-';
        removeGroup.addEventListener('click', () => {
          groups.splice(groupIndex, 1);
          if (groups.length === 0) groups.push(['birthplace']);
          renderGroups();
        });
        actions.append(addClause, removeGroup);
        head.append(label, actions);
        group.appendChild(head);

        clauses.forEach((kind, clauseIndex) => {
          const row = document.createElement('div');
          row.className = 'clause';
          const select = document.createElement('select');
          ['birthplace', 'high_school', 'college', 'pro_sports'].forEach(value => {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = optionLabel(value);
            if (value === kind) opt.selected = true;
            select.appendChild(opt);
          });
          select.addEventListener('change', () => {
            groups[groupIndex][clauseIndex] = select.value;
          });
          const remove = document.createElement('button');
          remove.className = 'icon';
          remove.title = 'Remove AND condition';
          remove.textContent = '-';
          remove.addEventListener('click', () => {
            groups[groupIndex].splice(clauseIndex, 1);
            if (groups[groupIndex].length === 0) groups[groupIndex].push('birthplace');
            renderGroups();
          });
          row.append(select, remove);
          group.appendChild(row);
        });

        root.appendChild(group);
      });
    }

    function renderLegend() {
      const legend = document.getElementById('legend');
      legend.innerHTML = '';
      Object.entries(eventColors).forEach(([type, color]) => {
        const row = document.createElement('div');
        row.className = 'legend-row';
        row.innerHTML = `<span class="swatch" style="background:${color}"></span><span>${eventLabels[type] || type}</span>`;
        legend.appendChild(row);
      });
    }

    function selectedSports() {
      const sports = [];
      if (document.getElementById('sportMLB').checked) sports.push('MLB');
      if (document.getElementById('sportNFL').checked) sports.push('NFL');
      if (document.getElementById('sportNBA').checked) sports.push('NBA');
      if (document.getElementById('sportNHL').checked) sports.push('NHL');
      return sports.length ? sports : ['MLB', 'NFL', 'NBA', 'NHL'];
    }

    function currentQuery() {
      return {
        place: document.getElementById('place').value,
        lat: center.lat,
        lon: center.lon,
        radius_mi: Number(document.getElementById('radius').value || 50),
        pro_start_year: Number(document.getElementById('proStartYear').value || 1970),
        pro_end_year: Number(document.getElementById('proEndYear').value || 2026),
        birth_start_year: Number(document.getElementById('birthStartYear').value || 1800),
        birth_end_year: Number(document.getElementById('birthEndYear').value || 2026),
        sports: selectedSports(),
        groups: groups.map(clauses => ({ clauses: clauses.map(kind => ({ kind })) })),
        sort: document.getElementById('sort').value,
        hof_only: document.getElementById('hofOnly').checked,
        all_star_only: document.getElementById('allStarOnly').checked,
        all_pro_only: document.getElementById('allProOnly').checked
      };
    }

    async function postJSON(path, body) {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    async function getJSON(path) {
      const response = await fetch(path);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function closePlaceSuggestions() {
      const box = document.getElementById('placeSuggestions');
      box.classList.remove('open');
      box.innerHTML = '';
      document.getElementById('place').setAttribute('aria-expanded', 'false');
      placeSuggestions = [];
      activeSuggestionIndex = -1;
    }

    function setActiveSuggestion(index) {
      activeSuggestionIndex = index;
      document.querySelectorAll('.suggestion').forEach((el, idx) => {
        el.classList.toggle('active', idx === activeSuggestionIndex);
      });
    }

    function choosePlaceSuggestion(suggestion) {
      document.getElementById('place').value = suggestion.label;
      center = { lat: Number(suggestion.lat), lon: Number(suggestion.lon) };
      map.setView([center.lat, center.lon], 10);
      closePlaceSuggestions();
      runQuery({ collapseCompact: true });
    }

    function renderPlaceSuggestions(suggestions) {
      const box = document.getElementById('placeSuggestions');
      box.innerHTML = '';
      placeSuggestions = suggestions || [];
      activeSuggestionIndex = -1;
      if (!placeSuggestions.length) {
        closePlaceSuggestions();
        return;
      }

      placeSuggestions.forEach((suggestion, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'suggestion';
        button.setAttribute('role', 'option');
        button.innerHTML = `${suggestion.label}<span>${suggestion.detail}</span>`;
        button.addEventListener('mousedown', event => {
          event.preventDefault();
          choosePlaceSuggestion(suggestion);
        });
        button.addEventListener('mouseenter', () => setActiveSuggestion(index));
        box.appendChild(button);
      });
      box.classList.add('open');
      document.getElementById('place').setAttribute('aria-expanded', 'true');
    }

    async function refreshPlaceSuggestions() {
      const input = document.getElementById('place');
      const value = input.value.trim();
      const requestId = ++suggestionRequestId;
      if (value.length < 2) {
        closePlaceSuggestions();
        return;
      }
      try {
        const data = await getJSON(`/api/place-suggest?q=${encodeURIComponent(value)}&limit=12`);
        if (requestId !== suggestionRequestId) return;
        renderPlaceSuggestions(data.suggestions || []);
      } catch {
        if (requestId === suggestionRequestId) closePlaceSuggestions();
      }
    }

    function queuePlaceSuggestions() {
      clearTimeout(suggestionTimer);
      suggestionTimer = setTimeout(refreshPlaceSuggestions, 160);
    }

    function updateCenterLayers() {
      if (centerMarker) centerMarker.remove();
      if (radiusLayer) radiusLayer.remove();
      centerMarker = L.marker([center.lat, center.lon]).addTo(map);
      const radiusColor = getComputedStyle(document.body).getPropertyValue('--ink').trim() || '#111827';
      radiusLayer = L.circle([center.lat, center.lon], {
        radius: Number(document.getElementById('radius').value || 50) * 1609.344,
        color: radiusColor,
        weight: 2,
        fill: false
      }).addTo(map);
    }

    function escapeHTML(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    function initialsFor(name) {
      const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
      if (!parts.length) return '?';
      return parts.slice(0, 2).map(part => part[0]).join('').toUpperCase();
    }

    function playerMediaMarkup(player) {
      const media = player.media || {};
      const initials = escapeHTML(initialsFor(player.display_name));
      const image = media.usable && media.thumbnail_url
        ? `<img src="${escapeHTML(media.thumbnail_url)}" alt="${escapeHTML(player.display_name)}" data-initials="${initials}">`
        : initials;
      const creditParts = [];
      if (media.attribution) creditParts.push(escapeHTML(media.attribution));
      if (media.source_page_url) creditParts.push(`<a href="${escapeHTML(media.source_page_url)}" target="_blank" rel="noreferrer">source</a>`);
      if (media.license_url && media.license) {
        creditParts.push(`<a href="${escapeHTML(media.license_url)}" target="_blank" rel="noreferrer">${escapeHTML(media.license)}</a>`);
      } else if (media.license) {
        creditParts.push(escapeHTML(media.license));
      }
      return {
        image,
        credit: creditParts.length ? `<div class="small photo-credit">Photo: ${creditParts.join(' · ')}</div>` : ''
      };
    }

    function wireAvatarFallback(root) {
      root.querySelectorAll('.avatar img').forEach(img => {
        img.addEventListener('error', () => {
          const avatar = img.closest('.avatar');
          if (!avatar) return;
          avatar.textContent = img.dataset.initials || '?';
        }, { once: true });
      });
    }

    function valueOrNA(value) {
      return value === null || value === undefined || value === '' ? 'NA' : String(value);
    }

    function earliestYear(...values) {
      const years = values
        .filter(value => value !== null && value !== undefined && value !== '')
        .map(value => Number(value))
        .filter(value => Number.isFinite(value));
      return years.length ? Math.min(...years) : null;
    }

    function latestYear(...values) {
      const years = values
        .filter(value => value !== null && value !== undefined && value !== '')
        .map(value => Number(value))
        .filter(value => Number.isFinite(value));
      return years.length ? Math.max(...years) : null;
    }

    function formatBirth(player) {
      const raw = player.birth_date || '';
      const match = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})/);
      if (match) {
        const [, year, month, day] = match;
        const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
        return date.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
      }
      return valueOrNA(player.birth_year);
    }

    function playerProfileLine(player) {
      const proDebut = earliestYear(player.debut_year, player.pro_start_year);
      const lastPlayed = latestYear(player.final_year, player.pro_end_year);
      const career = player.pro_career_length ? `${player.pro_career_length} pro seasons` : 'NA';
      const hof = player.hof_inducted ? `yes${player.hof_year ? ` (${player.hof_year})` : ''}` : 'no';
      return [
        `Birth: ${formatBirth(player)}`,
        `Pro debut: ${valueOrNA(proDebut)}`,
        `Last played: ${valueOrNA(lastPlayed)}`,
        `Career: ${career}`,
        `All-Star/Pro Bowl: ${player.all_star_count}`,
        `All-Pro/All-NBA: ${player.all_pro_count}`,
        `HOF: ${hof}`
      ].map(escapeHTML).join(' · ');
    }

    function playerTimelineMarkup(player) {
      const sections = player.timeline || [];
      if (!sections.length) return '';
      return `
        <div class="timeline" aria-label="Player trajectory">
          ${sections.map(section => `
            <section class="timeline-section">
              <strong>${escapeHTML(section.label)}</strong>
              <ul class="timeline-list">
                ${(section.items || []).map(item => `
                  <li><span class="timeline-years">${escapeHTML(item.years)}</span> · ${escapeHTML(item.label)}</li>
                `).join('')}
              </ul>
            </section>
          `).join('')}
        </div>
      `;
    }

    function renderPlayerList() {
      if (!latestData) return;
      const list = document.getElementById('list');
      list.innerHTML = '';
      const players = activeLocationFilter
        ? latestData.players.filter(player => player.location_keys.includes(activeLocationFilter.key))
        : latestData.players;

      document.getElementById('resultsTitle').textContent = activeLocationFilter ? 'Players At Dot' : 'Players';
      document.getElementById('resultsSubtitle').textContent = activeLocationFilter
        ? `${players.length.toLocaleString()} athletes at ${activeLocationFilter.label}`
        : `${players.length.toLocaleString()} athletes in current query`;
      document.getElementById('clearLocationFilter').style.visibility = activeLocationFilter ? 'visible' : 'hidden';
      if (activeLocationFilter) {
        document.getElementById('mapStatus').innerHTML =
          `<strong>${activeLocationFilter.label}</strong>${players.length.toLocaleString()} players at this dot are shown in the Players list.<br><button id="mapShowPlayers" class="primary">Show players</button>`;
        document.getElementById('mapShowPlayers').addEventListener('click', jumpToPlayers);
      }

      if (players.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = activeLocationFilter
          ? 'No players match this map-point filter. Show all dots or broaden the query.'
          : 'No players match this query. Try broadening the radius, years, sports, or location logic.';
        list.appendChild(empty);
        return;
      }

      players.forEach(player => {
        const item = document.createElement('article');
        item.className = 'player';
        item.tabIndex = 0;
        item.setAttribute('role', 'button');
        item.setAttribute('aria-expanded', 'false');
        const media = playerMediaMarkup(player);
        item.innerHTML = `
          <div class="avatar">${media.image}</div>
          <div class="player-body">
            <div class="player-head">
              <strong>${escapeHTML(player.display_name)}</strong>
              <span class="dist">${Number(player.nearest_mi).toFixed(1)} mi</span>
            </div>
            <div class="chips">
              <span class="chip">${escapeHTML(player.sport)}</span>
              ${player.matched_blocks.map(optionLabel).map(label => `<span class="chip">${escapeHTML(label)}</span>`).join('')}
            </div>
            <div class="small">${playerProfileLine(player)}</div>
            <div class="small">${escapeHTML(player.matched_locations.slice(0, 4).join(' | '))}</div>
            ${media.credit}
            ${playerTimelineMarkup(player)}
          </div>
        `;
        item.addEventListener('click', event => {
          if (event.target.closest('a, button')) return;
          const expanded = item.classList.toggle('expanded');
          item.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        });
        item.addEventListener('keydown', event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          const expanded = item.classList.toggle('expanded');
          item.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        });
        wireAvatarFallback(item);
        list.appendChild(item);
      });
    }

    function jumpToPlayers() {
      setPlayersCollapsed(false);
      document.getElementById('list').scrollTo({ top: 0, behavior: 'smooth' });
    }

    function isCompactLayout() {
      return window.matchMedia('(max-width: 900px)').matches;
    }

    function setQueryCollapsed(collapsed) {
      const panel = document.getElementById('queryPanel');
      const button = document.getElementById('toggleQuery');
      panel.classList.toggle('collapsed', collapsed);
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.textContent = collapsed ? 'Filters' : 'Hide';
      button.title = collapsed ? 'Show query filters' : 'Hide query filters';
      setTimeout(() => map.invalidateSize(), 200);
    }

    function setPlayersCollapsed(collapsed) {
      const panel = document.getElementById('playersPanel');
      const button = document.getElementById('togglePlayers');
      panel.classList.toggle('collapsed', collapsed);
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.title = collapsed ? 'Expand players panel' : 'Collapse players panel';
      button.textContent = collapsed ? '‹' : '›';
      setTimeout(() => map.invalidateSize(), 200);
    }

    function renderResults(data) {
      latestData = data;
      activeLocationFilter = null;
      resultLayer.clearLayers();
      data.locations_geojson.features.forEach(feature => {
        const [lon, lat] = feature.geometry.coordinates;
        const props = feature.properties;
        const marker = L.circleMarker([lat, lon], {
          radius: Math.min(18, 3 + Math.sqrt(props.players)),
          color: props.color,
          weight: 1,
          fill: true,
          fillOpacity: 0.34
        }).bindPopup(`<strong>${props.label}</strong><br>${props.event_label}<br>${props.players} players<br><span class="small">Click filters the side list.</span>`);
        marker.on('click', () => {
          activeLocationFilter = { key: props.filter_key, label: props.label, players: props.players };
          renderPlayerList();
        });
        resultLayer.addLayer(marker);
      });

      renderPlayerList();

      document.getElementById('meta').textContent =
        `${data.summary.players.toLocaleString()} athletes · ${data.summary.location_features.toLocaleString()} map points · ${data.summary.qualified_events.toLocaleString()} matching events`;
      document.getElementById('mapStatus').textContent = 'Click a colored dot to filter the Players list.';
    }

    async function runQuery(options = {}) {
      updateCenterLayers();
      document.getElementById('meta').textContent = 'Running query...';
      if (isCompactLayout() && options.collapseCompact === true) setQueryCollapsed(true);
      try {
        const data = await postJSON('/api/search', currentQuery());
        renderResults(data);
      } catch (error) {
        document.getElementById('meta').textContent = error.message;
      }
    }

    async function geocodeTypedPlace() {
      const place = document.getElementById('place').value;
      document.getElementById('meta').textContent = 'Geocoding place...';
      try {
        const result = await postJSON('/api/geocode', { place });
        center = { lat: result.lat, lon: result.lon };
        map.setView([center.lat, center.lon], 10);
        await runQuery({ collapseCompact: true });
      } catch (error) {
        document.getElementById('meta').textContent = error.message;
      }
    }

    document.getElementById('addGroup').addEventListener('click', () => {
      groups.push(['birthplace']);
      renderGroups();
    });
    document.getElementById('run').addEventListener('click', () => runQuery({ collapseCompact: true }));
    document.getElementById('geocode').addEventListener('click', geocodeTypedPlace);
    document.getElementById('place').addEventListener('input', queuePlaceSuggestions);
    document.getElementById('place').addEventListener('focus', queuePlaceSuggestions);
    document.getElementById('place').addEventListener('keydown', event => {
      if (!placeSuggestions.length) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setActiveSuggestion(Math.min(activeSuggestionIndex + 1, placeSuggestions.length - 1));
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        setActiveSuggestion(Math.max(activeSuggestionIndex - 1, 0));
      } else if (event.key === 'Enter' && activeSuggestionIndex >= 0) {
        event.preventDefault();
        choosePlaceSuggestion(placeSuggestions[activeSuggestionIndex]);
      } else if (event.key === 'Escape') {
        closePlaceSuggestions();
      }
    });
    document.addEventListener('mousedown', event => {
      if (!event.target.closest('.place-wrap')) closePlaceSuggestions();
    });
    document.getElementById('radius').addEventListener('change', updateCenterLayers);
    document.getElementById('sort').addEventListener('change', runQuery);
    document.getElementById('hofOnly').addEventListener('change', runQuery);
    document.getElementById('allStarOnly').addEventListener('change', runQuery);
    document.getElementById('allProOnly').addEventListener('change', runQuery);
    document.getElementById('showPlayers').addEventListener('click', jumpToPlayers);
    document.getElementById('toggleQuery').addEventListener('click', () => {
      const panel = document.getElementById('queryPanel');
      setQueryCollapsed(!panel.classList.contains('collapsed'));
    });
    document.getElementById('togglePlayers').addEventListener('click', () => {
      const panel = document.getElementById('playersPanel');
      setPlayersCollapsed(!panel.classList.contains('collapsed'));
    });
    document.getElementById('themeToggle').addEventListener('click', () => {
      applyTheme(currentTheme === 'dark' ? 'light' : 'dark');
    });
    document.getElementById('clearLocationFilter').addEventListener('click', () => {
      activeLocationFilter = null;
      renderPlayerList();
      document.getElementById('mapStatus').textContent = 'Showing all dots. Click a colored dot to filter the Players list.';
      jumpToPlayers();
    });
    map.on('click', event => {
      center = { lat: event.latlng.lat, lon: event.latlng.lng };
      map.setView([center.lat, center.lon], map.getZoom());
      runQuery({ collapseCompact: true });
    });

    renderGroups();
    renderLegend();
    applyTheme(currentTheme);
    let wasCompactLayout = isCompactLayout();
    setQueryCollapsed(wasCompactLayout);
    window.addEventListener('resize', () => {
      const compact = isCompactLayout();
      if (compact === wasCompactLayout) {
        setTimeout(() => map.invalidateSize(), 100);
        return;
      }
      wasCompactLayout = compact;
      if (compact) {
        setQueryCollapsed(true);
      } else {
        setQueryCollapsed(false);
      }
    });
    updateCenterLayers();
    runQuery({ collapseCompact: true });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HometownHeroesPilot/0.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"), "application/json")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            html = (
                HTML.replace("__DEFAULT_QUERY__", json.dumps(DEFAULT_QUERY))
                .replace("__EVENT_COLORS__", json.dumps(EVENT_COLORS))
                .replace("__EVENT_LABELS__", json.dumps(EVENT_LABELS))
            )
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/attributions":
            text = ATTRIBUTIONS_PATH.read_text(encoding="utf-8")
            self._send(200, render_text_page("HometownHeroes Attributions", text), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            con = connect()
            try:
                status = {
                    "database": repo_path(DB_PATH),
                    "players": con.execute("select count(*) from players").fetchone()[0],
                    "events": con.execute("select count(*) from player_location_events").fetchone()[0],
                    "geocoded_events": con.execute("select count(*) from geocoded_player_location_events").fetchone()[0],
                }
            finally:
                con.close()
            self._json(200, status)
            return
        if path == "/api/place-suggest":
            params = urllib.parse.parse_qs(parsed.query)
            q = params.get("q", [""])[0]
            limit = int(params.get("limit", ["12"])[0] or "12")
            self._json(200, suggest_places(q, max(1, min(limit, 25))))
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/api/geocode":
                self._json(200, geocode_place(str(payload.get("place", ""))))
                return
            if self.path == "/api/search":
                self._json(200, build_response(normalize_query(payload)))
                return
            self._json(404, {"error": "Not found"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the experimental HometownHeroes pilot visualizer.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Missing {repo_path(DB_PATH)}. Run python3 pipelines/build_database.py first.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving experimental visualizer at http://{args.host}:{args.port}")
    print("This is scratch code; stop with Ctrl-C.")
    server.serve_forever()


if __name__ == "__main__":
    main()
