#!/usr/bin/env python3
"""
Audit professional-team location events with missing year coverage.

This is intentionally read-only against the application database. It writes a
scratch SQLite audit DB plus a JSON summary so missing pro timeline years can be
reviewed separately from the main warehouse build.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
APP_DB = SCRATCH / "HometownHeroes.sqlite"
OUT_DB = SCRATCH / "pro_year_coverage_audit.sqlite"
SUMMARY_PATH = SCRATCH / "pro_year_coverage_audit_summary.json"


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def normalize_team(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"\b(los angeles|la)\b", "los angeles", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def team_from_source_key(source_key: object) -> str:
    text = str(source_key or "")
    if "|" in text:
        return text.split("|", 1)[0].strip()
    return text.strip()


def init_audit_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table missing_pro_year_events (
            sport text not null,
            player_id text not null,
            display_name text,
            event_id text not null,
            missing_label text,
            missing_team_key text,
            missing_source text,
            missing_source_key text,
            missing_notes text,
            player_debut_year integer,
            player_final_year integer,
            pro_summary_start_year integer,
            pro_summary_end_year integer,
            same_team_known_start_year integer,
            same_team_known_end_year integer,
            same_team_known_sources text,
            classification text not null,
            primary key (event_id)
        );

        create table known_pro_year_events (
            sport text not null,
            player_id text not null,
            display_name text,
            event_id text not null,
            known_label text,
            known_team_key text,
            start_year integer,
            end_year integer,
            source text,
            source_key text,
            primary key (event_id)
        );
        """
    )
    return con


def load_known_events(app_con: sqlite3.Connection) -> list[dict[str, object]]:
    rows = app_con.execute(
        """
        select
            p.sport,
            p.player_id,
            p.display_name,
            e.event_id,
            l.label as known_label,
            e.start_year,
            e.end_year,
            e.source,
            e.source_key
        from player_location_events e
        join players p on p.sport = e.sport and p.player_id = e.player_id
        join locations l using (location_id)
        where e.event_type = 'played_pro'
          and (e.start_year is not null or e.end_year is not null)
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "known_team_key": normalize_team(team_from_source_key(row["source_key"]) or row["known_label"]),
        }
        for row in rows
    ]


def load_missing_events(app_con: sqlite3.Connection) -> list[dict[str, object]]:
    rows = app_con.execute(
        """
        select
            p.sport,
            p.player_id,
            p.display_name,
            p.debut_year as player_debut_year,
            p.final_year as player_final_year,
            pcs.pro_start_year as pro_summary_start_year,
            pcs.pro_end_year as pro_summary_end_year,
            e.event_id,
            l.label as missing_label,
            e.source as missing_source,
            e.source_key as missing_source_key,
            e.notes as missing_notes
        from player_location_events e
        join players p on p.sport = e.sport and p.player_id = e.player_id
        join locations l using (location_id)
        left join pro_career_summary pcs on pcs.sport = p.sport and pcs.player_id = p.player_id
        where e.event_type = 'played_pro'
          and e.start_year is null
          and e.end_year is null
        order by p.sport, p.display_name, l.label
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "missing_team_key": normalize_team(row["missing_label"]),
        }
        for row in rows
    ]


def classify_missing_events(
    known_rows: list[dict[str, object]], missing_rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    known_by_player_team: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in known_rows:
        key = (str(row["sport"]), str(row["player_id"]), str(row["known_team_key"]))
        known_by_player_team.setdefault(key, []).append(row)

    out = []
    for row in missing_rows:
        key = (str(row["sport"]), str(row["player_id"]), str(row["missing_team_key"]))
        matches = known_by_player_team.get(key, [])
        starts = [int(match["start_year"]) for match in matches if match["start_year"] is not None]
        ends = [int(match["end_year"]) for match in matches if match["end_year"] is not None]
        if matches:
            classification = "same_team_years_known_elsewhere"
        elif row["player_debut_year"] is not None or row["pro_summary_start_year"] is not None:
            classification = "player_career_years_known_team_years_missing"
        else:
            classification = "player_and_team_years_missing"
        out.append(
            {
                **row,
                "same_team_known_start_year": min(starts) if starts else None,
                "same_team_known_end_year": max(ends) if ends else None,
                "same_team_known_sources": "; ".join(sorted({str(match["source"]) for match in matches})),
                "classification": classification,
            }
        )
    return out


def write_summary(con: sqlite3.Connection, summary_path: Path, audit_db_path: Path) -> dict[str, object]:
    summary = {
        "audit_database": repo_path(audit_db_path),
        "missing_pro_year_rows_by_sport": dict(
            con.execute(
                """
                select sport, count(*)
                from missing_pro_year_events
                group by sport
                order by sport
                """
            ).fetchall()
        ),
        "players_with_missing_pro_year_rows_by_sport": dict(
            con.execute(
                """
                select sport, count(distinct player_id)
                from missing_pro_year_events
                group by sport
                order by sport
                """
            ).fetchall()
        ),
        "classification_counts": {
            row["classification"]: row["rows"]
            for row in con.execute(
                """
                select classification, count(*) as rows
                from missing_pro_year_events
                group by classification
                order by rows desc, classification
                """
            )
        },
        "source_counts": {
            row["missing_source"]: row["rows"]
            for row in con.execute(
                """
                select missing_source, count(*) as rows
                from missing_pro_year_events
                group by missing_source
                order by rows desc, missing_source
                """
            )
        },
        "top_players": [
            dict(row)
            for row in con.execute(
                """
                select sport, player_id, display_name, count(*) as missing_rows
                from missing_pro_year_events
                group by sport, player_id, display_name
                order by missing_rows desc, display_name
                limit 25
                """
            )
        ],
        "notes": [
            "Rows classified as same_team_years_known_elsewhere are usually duplicate location-source rows where another pro event already has a same-team year span.",
            "Rows classified as player_career_years_known_team_years_missing have player-level career dates but no team-specific dates for that event.",
            "This audit does not auto-fill or suppress events; it is a review artifact.",
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, default=APP_DB)
    parser.add_argument("--out-db", type=Path, default=OUT_DB)
    parser.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    args = parser.parse_args()

    if not args.app_db.exists():
        raise SystemExit(f"Missing app database: {args.app_db}")

    app_con = sqlite3.connect(args.app_db)
    app_con.row_factory = sqlite3.Row
    known_rows = load_known_events(app_con)
    missing_rows = load_missing_events(app_con)
    app_con.close()

    audit_rows = classify_missing_events(known_rows, missing_rows)
    con = init_audit_db(args.out_db)
    con.row_factory = sqlite3.Row
    con.executemany(
        """
        insert into known_pro_year_events
        (sport, player_id, display_name, event_id, known_label, known_team_key, start_year, end_year, source, source_key)
        values
        (:sport, :player_id, :display_name, :event_id, :known_label, :known_team_key, :start_year, :end_year, :source, :source_key)
        """,
        known_rows,
    )
    con.executemany(
        """
        insert into missing_pro_year_events
        (sport, player_id, display_name, event_id, missing_label, missing_team_key,
         missing_source, missing_source_key, missing_notes, player_debut_year, player_final_year,
         pro_summary_start_year, pro_summary_end_year, same_team_known_start_year,
         same_team_known_end_year, same_team_known_sources, classification)
        values
        (:sport, :player_id, :display_name, :event_id, :missing_label, :missing_team_key,
         :missing_source, :missing_source_key, :missing_notes, :player_debut_year, :player_final_year,
         :pro_summary_start_year, :pro_summary_end_year, :same_team_known_start_year,
         :same_team_known_end_year, :same_team_known_sources, :classification)
        """,
        audit_rows,
    )
    con.commit()
    summary = write_summary(con, args.summary, args.out_db)
    con.close()
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
