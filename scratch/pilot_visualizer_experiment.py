#!/usr/bin/env python3
"""
EXPERIMENT: Pilot Hometown Heroes visualizer.

This is an explicitly tracked, agent-coded experiment for working out
map/search/list interaction kinks before promoting any durable app
architecture. Treat this as a playable prototype, not the final frontend.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "scratch" / "hometown_heroes.sqlite"
GEOCODE_CACHE = ROOT / "scratch" / "place_geocode_cache.sqlite"

EVENT_TYPE_GROUPS = {
    "birthplace": ["born"],
    "high_school": ["attended_high_school"],
    "college": ["attended_college", "attended_school", "played_college"],
    "pro_sports": ["played_pro"],
}

EVENT_LABELS = {
    "born": "Birthplace",
    "attended_high_school": "High School",
    "attended_college": "College",
    "attended_school": "School",
    "played_college": "College Sports",
    "played_pro": "Pro Sports",
}

EVENT_COLORS = {
    "born": "#2563eb",
    "attended_high_school": "#16a34a",
    "attended_college": "#7c3aed",
    "attended_school": "#a855f7",
    "played_college": "#ea580c",
    "played_pro": "#dc2626",
}

DEFAULT_QUERY = {
    "place": "San Jose, CA",
    "lat": 37.3382,
    "lon": -121.8863,
    "radius_mi": 50,
    "start_year": 1970,
    "end_year": 2026,
    "sports": ["MLB", "NFL"],
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
    return path.resolve().relative_to(ROOT).as_posix()


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
            "countrycodes": "us",
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
    with connect() as con:
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
    query["start_year"] = int(query["start_year"])
    query["end_year"] = int(query["end_year"])
    if query["start_year"] > query["end_year"]:
        query["start_year"], query["end_year"] = query["end_year"], query["start_year"]
    query["sports"] = [sport for sport in query.get("sports", []) if sport in {"MLB", "NFL"}] or ["MLB", "NFL"]
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
        query["end_year"],
        query["start_year"],
        query["radius_mi"],
    ]
    sql = f"""
        with candidate_events as (
            select
                v.*,
                pcs.pro_start_year,
                pcs.pro_end_year,
                pcs.pro_location_seasons,
                coalesce(phs.all_star_count, 0) as all_star_count,
                coalesce(phs.all_pro_count, 0) as all_pro_count,
                coalesce(phs.hof_inducted, 0) as hof_inducted,
                phs.hof_year,
                phs.honor_sources,
                3958.7613 * 2 * asin(
                    min(1.0, sqrt(
                        pow(sin(((v.latitude - ?) * 0.017453292519943295) / 2), 2) +
                        cos(? * 0.017453292519943295) *
                        cos(v.latitude * 0.017453292519943295) *
                        pow(sin(((v.longitude - ?) * 0.017453292519943295) / 2), 2)
                    ))
                ) as distance_mi
            from geocoded_player_location_events v
            left join pro_career_summary pcs
              on pcs.sport = v.sport and pcs.player_id = v.player_id
            left join player_honor_summary phs
              on phs.sport = v.sport and phs.player_id = v.player_id
            where v.sport in ({placeholders(query["sports"])})
              and v.event_type in ({placeholders(event_types)})
              and v.latitude between ? and ?
              and v.longitude between ? and ?
              and (
                v.start_year is null
                or v.end_year is null
                or (v.start_year <= ? and v.end_year >= ?)
              )
        )
        select *
        from candidate_events
        where distance_mi <= ?
        order by distance_mi, sport, display_name, event_type
    """
    with connect() as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


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
                "matched_blocks": sorted(player_blocks.get(key, set())),
                "matched_groups": sorted(matched_groups.get(key, set())),
                "matched_types": set(),
                "matched_locations": set(),
                "location_keys": set(),
                "first_year": None,
                "last_year": None,
                "pro_start_year": row["pro_start_year"],
                "pro_end_year": row["pro_end_year"],
                "pro_career_length": row["pro_location_seasons"],
                "all_star_count": row["all_star_count"],
                "all_pro_count": row["all_pro_count"],
                "hof_inducted": bool(row["hof_inducted"]),
                "hof_year": row["hof_year"],
                "honor_sources": row["honor_sources"],
            },
        )
        player["nearest_mi"] = min(player["nearest_mi"], row["distance_mi"])
        player["matched_types"].add(row["event_type"])
        player["matched_locations"].add(row["location_label"])
        player["location_keys"].add(f"{row['location_id']}|{row['event_type']}")
        for field, target in (("start_year", "first_year"), ("end_year", "last_year")):
            if row[field] is not None:
                if player[target] is None:
                    player[target] = row[field]
                elif target == "first_year":
                    player[target] = min(player[target], row[field])
                else:
                    player[target] = max(player[target], row[field])

        dot_key = (row["location_id"], row["event_type"])
        dot = dots.setdefault(
            dot_key,
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]},
                "properties": {
                    "location_id": row["location_id"],
                    "label": row["location_label"],
                    "event_type": row["event_type"],
                    "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
                    "color": EVENT_COLORS.get(row["event_type"], "#6b7280"),
                    "filter_key": f"{row['location_id']}|{row['event_type']}",
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
                "MLB All-Star and HOF fields come from Lahman; NFL HOF comes from Wikidata P6930; NFL Pro Bowl and All-Pro counts come from Wikipedia infobox career highlights.",
            ],
        },
        "players": player_rows[:1000],
        "locations_geojson": {"type": "FeatureCollection", "features": features},
    }


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hometown Heroes Pilot Visualizer</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      color-scheme: light;
      --ink: #172033;
      --muted: #657085;
      --line: #d9dee8;
      --panel: #ffffff;
      --bg: #eef1f5;
      --accent: #1663d6;
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
      display: grid;
      grid-template-columns: minmax(360px, 430px) 1fr;
      overflow: hidden;
    }
    aside {
      display: grid;
      grid-template-rows: auto auto auto 1fr;
      min-width: 0;
      border-right: 1px solid var(--line);
      background: var(--panel);
      overflow: hidden;
    }
    .controls {
      padding: 14px;
      display: grid;
      gap: 10px;
      border-bottom: 1px solid var(--line);
    }
    .title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h1 {
      font-size: 18px;
      line-height: 1.1;
      margin: 0;
      font-weight: 750;
    }
    .badge {
      font-size: 11px;
      color: #7a4f00;
      background: #fff5d6;
      border: 1px solid #f1d27b;
      border-radius: 8px;
      padding: 4px 7px;
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
      font-size: 11px;
      color: var(--muted);
      font-weight: 650;
      text-transform: uppercase;
    }
    input, select, button {
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 7px 9px;
      font: inherit;
      background: #fff;
      color: var(--ink);
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
      width: 34px;
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
      background: #fff;
      box-shadow: 0 12px 26px rgba(15, 23, 42, .16);
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
      background: #edf4ff;
    }
    .suggestion span {
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }
    .checks {
      display: flex;
      gap: 10px;
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
    }
    .checks input { min-height: auto; }
    .query-builder {
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fafc;
    }
    .query-help {
      font-size: 12px;
      line-height: 1.35;
      color: var(--muted);
    }
    .group {
      display: grid;
      gap: 8px;
      padding: 8px;
      border: 1px solid #dfe5ef;
      border-radius: 8px;
      background: #fff;
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
      grid-template-columns: 1fr 34px;
      gap: 8px;
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
      padding: 9px 14px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      border-bottom: 1px solid var(--line);
    }
    .results-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 9px 14px;
      border-bottom: 1px solid var(--line);
      background: #fff;
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
      gap: 8px;
      align-items: center;
      flex-shrink: 0;
    }
    .list {
      overflow: auto;
      padding: 8px;
      display: grid;
      gap: 8px;
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
      background: #fafbfc;
    }
    .player {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      background: #fff;
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
      border-radius: 7px;
      background: #eef2f7;
      padding: 3px 6px;
      color: #374151;
    }
    .small {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.35;
    }
    main {
      position: relative;
      min-width: 0;
    }
    #map {
      position: absolute;
      inset: 0;
    }
    .map-status {
      position: absolute;
      z-index: 500;
      left: 12px;
      bottom: 12px;
      max-width: min(520px, calc(100vw - 470px));
      background: rgba(255,255,255,.94);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      color: var(--muted);
      box-shadow: 0 6px 16px rgba(15, 23, 42, .12);
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
      right: 12px;
      top: 12px;
      background: rgba(255,255,255,.94);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      display: grid;
      gap: 5px;
      font-size: 12px;
      box-shadow: 0 6px 16px rgba(15, 23, 42, .12);
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
    @media (max-width: 860px) {
      .app { grid-template-columns: 1fr; grid-template-rows: 48vh 52vh; }
      main { order: 1; }
      aside { order: 2; border-right: 0; border-top: 1px solid var(--line); overflow: auto; }
      .map-status { max-width: calc(100vw - 24px); }
      .controls { padding-bottom: 10px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <section class="controls">
        <div class="title">
          <h1>Hometown Heroes</h1>
          <span class="badge">Experiment</span>
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
              <option value="all_pro">All-Pro</option>
            </select>
          </label>
        </div>
        <div class="grid2">
          <label>Start Year
            <input id="startYear" type="number" value="1970">
          </label>
          <label>End Year
            <input id="endYear" type="number" value="2026">
          </label>
        </div>
        <div class="checks">
          <label><input type="checkbox" id="sportMLB" checked> MLB</label>
          <label><input type="checkbox" id="sportNFL" checked> NFL</label>
          <label><input type="checkbox" id="allStarOnly"> All-Star / Pro Bowl</label>
          <label><input type="checkbox" id="allProOnly"> All-Pro</label>
          <label><input type="checkbox" id="hofOnly"> HOF only</label>
        </div>
        <div class="query-builder">
          <div class="row">
            <div class="query-help">Any group may match. Inside one group, every condition is required.</div>
            <button id="addGroup" class="icon" title="Add OR group">+</button>
          </div>
          <div id="groups"></div>
        </div>
        <button id="run" class="primary">Run Query</button>
      </section>
      <section class="meta" id="meta">Ready.</section>
      <section class="results-bar" id="resultsBar">
        <div class="results-title">
          <strong id="resultsTitle">Players</strong>
          <span id="resultsSubtitle">Run a query to populate the side list.</span>
        </div>
        <div class="results-actions">
          <button id="clearLocationFilter" class="link" title="Show players from all map points">All dots</button>
          <button id="showPlayers" class="primary" title="Jump to player list">Players</button>
        </div>
      </section>
      <section class="list" id="list"></section>
    </aside>
    <main>
      <div id="map"></div>
      <div class="legend" id="legend"></div>
      <div class="map-status" id="mapStatus">Click the map to move the search center. Click a colored dot to filter the Players list.</div>
    </main>
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

    const map = L.map('map', { zoomControl: true }).setView([center.lat, center.lon], 9);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);
    resultLayer.addTo(map);

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
      return sports.length ? sports : ['MLB', 'NFL'];
    }

    function currentQuery() {
      return {
        place: document.getElementById('place').value,
        lat: center.lat,
        lon: center.lon,
        radius_mi: Number(document.getElementById('radius').value || 50),
        start_year: Number(document.getElementById('startYear').value || 1970),
        end_year: Number(document.getElementById('endYear').value || 2026),
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
      runQuery();
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
      radiusLayer = L.circle([center.lat, center.lon], {
        radius: Number(document.getElementById('radius').value || 50) * 1609.344,
        color: '#111827',
        weight: 2,
        fill: false
      }).addTo(map);
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
        empty.textContent = 'No players match this filtered view.';
        list.appendChild(empty);
        return;
      }

      players.forEach(player => {
        const item = document.createElement('article');
        item.className = 'player';
        const career = player.pro_career_length ? `${player.pro_career_length} pro seasons` : 'career length unavailable';
        const years = [player.first_year, player.last_year].filter(v => v !== null && v !== undefined).join('-') || 'no event years';
        const hof = player.hof_inducted ? `yes${player.hof_year ? ` (${player.hof_year})` : ''}` : 'no';
        const groupsText = player.matched_groups && player.matched_groups.length
          ? `Groups ${player.matched_groups.join(', ')}`
          : 'Matched query';
        item.innerHTML = `
          <div class="player-head">
            <strong>${player.display_name}</strong>
            <span class="dist">${Number(player.nearest_mi).toFixed(1)} mi</span>
          </div>
          <div class="chips">
            <span class="chip">${player.sport}</span>
            <span class="chip">${groupsText}</span>
            ${player.matched_blocks.map(optionLabel).map(label => `<span class="chip">${label}</span>`).join('')}
          </div>
          <div class="small">${years} · ${career} · All-Star/Pro Bowl ${player.all_star_count} · All-Pro ${player.all_pro_count} · HOF ${hof}</div>
          <div class="small">${player.matched_locations.slice(0, 4).join(' | ')}</div>
        `;
        list.appendChild(item);
      });
    }

    function jumpToPlayers() {
      document.getElementById('resultsBar').scrollIntoView({ behavior: 'smooth', block: 'start' });
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
      document.getElementById('mapStatus').textContent =
        `Click a colored dot to filter the Players list. ${data.summary.data_notes.join(' ')}`;
    }

    async function runQuery() {
      updateCenterLayers();
      document.getElementById('meta').textContent = 'Running query...';
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
        await runQuery();
      } catch (error) {
        document.getElementById('meta').textContent = error.message;
      }
    }

    document.getElementById('addGroup').addEventListener('click', () => {
      groups.push(['birthplace']);
      renderGroups();
    });
    document.getElementById('run').addEventListener('click', runQuery);
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
    document.getElementById('clearLocationFilter').addEventListener('click', () => {
      activeLocationFilter = null;
      renderPlayerList();
      document.getElementById('mapStatus').textContent = 'Showing all dots. Click a colored dot to filter the Players list.';
      jumpToPlayers();
    });
    map.on('click', event => {
      center = { lat: event.latlng.lat, lon: event.latlng.lng };
      map.setView([center.lat, center.lon], map.getZoom());
      runQuery();
    });

    renderGroups();
    renderLegend();
    updateCenterLayers();
    runQuery();
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
        if path == "/api/status":
            with connect() as con:
                status = {
                    "database": repo_path(DB_PATH),
                    "players": con.execute("select count(*) from players").fetchone()[0],
                    "events": con.execute("select count(*) from player_location_events").fetchone()[0],
                    "geocoded_events": con.execute("select count(*) from geocoded_player_location_events").fetchone()[0],
                }
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
    parser = argparse.ArgumentParser(description="Run the experimental Hometown Heroes pilot visualizer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Missing {repo_path(DB_PATH)}. Run python3 pipelines/build_database.py first.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving experimental visualizer at http://{args.host}:{args.port}")
    print("This is scratch code; stop with Ctrl-C.")
    server.serve_forever()


if __name__ == "__main__":
    main()
