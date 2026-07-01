#!/usr/bin/env python3
"""
Apply conservative fixes from a birthplace/date coverage audit.

The audit runner is read-only. This fixer turns reviewed or low-risk audit
findings into reproducible cache updates:

- Wikidata P569 can fill missing player birth dates.
- Wikidata P19 with coordinates can fill missing geocoded birthplace events.
- Conflicting birthplace corrections require a reviewed JSON override file.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
AUDIT_DB = SCRATCH / "birthplace_coverage_audit.sqlite"
FIXES_DB = SCRATCH / "birthplace_audit_fixes.sqlite"
WIKIDATA_BIRTHPLACE_DB = SCRATCH / "wikidata_birthplace_enrichment.sqlite"
CURATED_BIRTHPLACE_OVERRIDES_PATH = ROOT / "data" / "curation" / "birthplace_overrides.json"


def log(message: str) -> None:
    print(message, flush=True)


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def parse_wikidata_date(value: str) -> tuple[str | None, int | None]:
    match = re.match(r"^\+?(\d{4})-(\d{2})-(\d{2})T", value or "")
    if not match:
        return None, None
    year, month, day = match.groups()
    if month == "00" or day == "00":
        return None, int(year)
    return f"{year}-{month}-{day}", int(year)


def init_fixes_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode = wal")
    con.executescript(
        """
        create table if not exists player_birthdate_overrides (
            sport text not null,
            player_id text not null,
            birth_date text,
            birth_year integer,
            source text not null,
            source_key text,
            notes text,
            primary key (sport, player_id)
        );

        create table if not exists birthplace_overrides (
            sport text not null,
            player_id text not null,
            event_type text not null default 'born',
            location_id text not null,
            location_kind text not null default 'birthplace',
            label text not null,
            city text,
            state text,
            country text,
            latitude real,
            longitude real,
            geocode_status text not null default 'matched',
            geocode_source text,
            source text not null,
            source_key text,
            confidence text not null default 'source_reported_cross_checked',
            notes text,
            primary key (sport, player_id, event_type)
        );
        """
    )
    return con


def init_wikidata_birthplace_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode = wal")
    con.executescript(
        """
        create table if not exists wikidata_birthplace_events (
            sport text,
            source_player_id text,
            wikidata_qid text,
            external_property text,
            external_id text,
            person_label text,
            birthplace_qid text,
            birthplace_label text,
            located_in_label text,
            country_label text,
            latitude real,
            longitude real,
            raw_cache_path text,
            primary key (sport, source_player_id, wikidata_qid, birthplace_qid, external_id)
        );
        """
    )
    return con


def apply_wikidata_birthdates(audit_con: sqlite3.Connection, fixes_con: sqlite3.Connection, dry_run: bool) -> int:
    rows = audit_con.execute(
        """
        select
            c.sport,
            c.player_id,
            c.display_name,
            h.wikidata_qid,
            h.birth_date
        from candidates c
        join wikidata_hits h
          on h.sport = c.sport
         and h.player_id = c.player_id
         and h.external_property = c.external_property
         and h.external_id = c.external_id
        where (c.missing_birth_date = 1 or c.missing_birth_year = 1)
          and h.birth_date is not null
          and h.birth_date != ''
        group by c.sport, c.player_id
        having count(distinct h.birth_date) = 1
        """
    ).fetchall()
    out = []
    for row in rows:
        birth_date, birth_year = parse_wikidata_date(row["birth_date"])
        if birth_date is None and birth_year is None:
            continue
        out.append(
            {
                "sport": row["sport"],
                "player_id": row["player_id"],
                "birth_date": birth_date,
                "birth_year": birth_year,
                "source": "Wikidata P569 via birthplace coverage audit",
                "source_key": row["wikidata_qid"],
                "notes": f"Filled missing birth date/year for {row['display_name'] or row['player_id']}.",
            }
        )
    if dry_run:
        return len(out)
    fixes_con.executemany(
        """
        insert or replace into player_birthdate_overrides
        (sport, player_id, birth_date, birth_year, source, source_key, notes)
        values
        (:sport, :player_id, :birth_date, :birth_year, :source, :source_key, :notes)
        """,
        out,
    )
    fixes_con.commit()
    return len(out)


def apply_wikidata_birthplaces(
    audit_con: sqlite3.Connection, wikidata_birthplace_con: sqlite3.Connection, dry_run: bool
) -> int:
    rows = audit_con.execute(
        """
        select
            c.sport,
            c.player_id,
            c.display_name,
            h.external_property,
            h.external_id,
            h.wikidata_qid,
            h.person_label,
            h.birthplace_qid,
            h.birthplace_label,
            h.located_in_label,
            h.country_label,
            h.latitude,
            h.longitude,
            h.raw_cache_path
        from candidates c
        join wikidata_hits h
          on h.sport = c.sport
         and h.player_id = c.player_id
         and h.external_property = c.external_property
         and h.external_id = c.external_id
        where c.missing_geocoded_birth = 1
          and h.birthplace_qid is not null
          and h.birthplace_qid != ''
          and h.latitude is not null
          and h.longitude is not null
        group by c.sport, c.player_id, h.wikidata_qid, h.birthplace_qid
        """
    ).fetchall()
    out = [
        {
            "sport": row["sport"],
            "source_player_id": row["player_id"],
            "wikidata_qid": row["wikidata_qid"],
            "external_property": row["external_property"],
            "external_id": row["external_id"],
            "person_label": row["person_label"] or row["display_name"],
            "birthplace_qid": row["birthplace_qid"],
            "birthplace_label": row["birthplace_label"],
            "located_in_label": row["located_in_label"],
            "country_label": row["country_label"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "raw_cache_path": row["raw_cache_path"],
        }
        for row in rows
    ]
    if dry_run:
        return len(out)
    wikidata_birthplace_con.executemany(
        """
        insert or replace into wikidata_birthplace_events
        (sport, source_player_id, wikidata_qid, external_property, external_id, person_label,
         birthplace_qid, birthplace_label, located_in_label, country_label, latitude, longitude, raw_cache_path)
        values
        (:sport, :source_player_id, :wikidata_qid, :external_property, :external_id, :person_label,
         :birthplace_qid, :birthplace_label, :located_in_label, :country_label, :latitude, :longitude, :raw_cache_path)
        """,
        out,
    )
    wikidata_birthplace_con.commit()
    return len(out)


def load_json_list(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{repo_path(path)} must contain a JSON list")
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"{repo_path(path)} row {index} must be an object")
    return data


def merge_reviewed_overrides(input_path: Path, output_path: Path, dry_run: bool) -> int:
    required = {
        "sport",
        "player_id",
        "event_type",
        "location_id",
        "location_kind",
        "label",
        "geocode_status",
        "source",
        "confidence",
    }
    incoming = load_json_list(input_path)
    for index, row in enumerate(incoming):
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"{repo_path(input_path)} row {index} is missing: {', '.join(missing)}")
    existing = load_json_list(output_path) if output_path.exists() else []
    merged: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in existing + incoming:
        key = (str(row["sport"]), str(row["player_id"]), str(row.get("event_type") or "born"))
        merged[key] = row
    if dry_run:
        return len(incoming)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(list(merged.values()), indent=2) + "\n", encoding="utf-8")
    return len(incoming)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-db", type=Path, default=AUDIT_DB)
    parser.add_argument("--fixes-db", type=Path, default=FIXES_DB)
    parser.add_argument("--wikidata-birthplace-db", type=Path, default=WIKIDATA_BIRTHPLACE_DB)
    parser.add_argument("--curated-overrides", type=Path, default=CURATED_BIRTHPLACE_OVERRIDES_PATH)
    parser.add_argument("--reviewed-overrides", type=Path)
    parser.add_argument("--apply-wikidata-birthdates", action="store_true")
    parser.add_argument("--apply-wikidata-birthplaces", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.audit_db.exists():
        raise SystemExit(f"Missing audit DB: {args.audit_db}")
    audit_con = sqlite3.connect(args.audit_db)
    audit_con.row_factory = sqlite3.Row
    fixes_con = init_fixes_db(args.fixes_db)
    wikidata_birthplace_con = init_wikidata_birthplace_db(args.wikidata_birthplace_db)

    applied: dict[str, int] = {}
    if args.apply_wikidata_birthdates:
        applied["wikidata_birthdate_overrides"] = apply_wikidata_birthdates(audit_con, fixes_con, args.dry_run)
    if args.apply_wikidata_birthplaces:
        applied["wikidata_birthplace_cache_rows"] = apply_wikidata_birthplaces(
            audit_con, wikidata_birthplace_con, args.dry_run
        )
    if args.reviewed_overrides:
        applied["reviewed_curated_birthplace_overrides"] = merge_reviewed_overrides(
            args.reviewed_overrides, args.curated_overrides, args.dry_run
        )

    audit_con.close()
    fixes_con.close()
    wikidata_birthplace_con.close()

    if not applied:
        log("No fixes selected. Pass --apply-wikidata-birthdates, --apply-wikidata-birthplaces, or --reviewed-overrides.")
    else:
        log(json.dumps({"dry_run": args.dry_run, "applied": applied}, indent=2))

    if args.rebuild and not args.dry_run:
        subprocess.run([sys.executable, str(ROOT / "pipelines" / "build_database.py")], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
