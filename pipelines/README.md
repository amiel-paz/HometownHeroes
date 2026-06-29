# Data Pipelines

These scripts rebuild the local enrichment caches and unified SQLite database from the source snapshots under `data/raw`.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[notebook]"
```

## Full Rebuild

```bash
python3 pipelines/ingest_mlb.py
python3 pipelines/ingest_nfl.py
python3 pipelines/ingest_nba.py
python3 pipelines/ingest_nfl_stadiums.py
python3 pipelines/enrich_wikidata_education.py --sports MLB,NFL,NBA --chunk-size 200 --max-chunks 0 --sleep-seconds 0.5
python3 pipelines/enrich_wikidata_birthplace.py --sports NFL,NBA --chunk-size 200 --max-chunks 0 --sleep-seconds 0.5
python3 pipelines/curate_high_schools.py --max-pages 0 --no-title-fallback
python3 pipelines/enrich_player_honors.py --sports MLB,NFL,NBA --include-wikipedia-nfl --include-wikipedia-nba --chunk-size 200 --wikipedia-chunk-size 50 --max-chunks 0 --max-wikipedia-chunks 0 --sleep-seconds 0.5
python3 pipelines/build_database.py
```

Generated SQLite and CSV outputs are written under `scratch/`. Raw Wikidata responses are cached under `data/raw/wikidata/` so interrupted runs can resume without refetching completed chunks.

## Year Semantics

- `played_pro` rows may carry `start_year`, `end_year`, and `duration_years` only when a source provides season-level professional participation. The unified database exposes those rows through `pro_career_summary`.
- Current NFL `played_pro` rows are derived from nflverse roster seasons joined to schedule-inferred home stadiums. They mean roster membership associated with a team home stadium, not confirmed game appearances.
- `attended_high_school`, `attended_college`, and `attended_school` are association rows unless their source explicitly provides years. Do not infer high school or college dates from age, draft year, or pro debut.
- Current NFL `rookie_season` and `last_season` fields from nflverse are retained in the source cache, but are not loaded as canonical `players.debut_year` / `players.final_year`.
- Current NBA `played_pro` rows are derived from hoopR player/team season rows joined to schedule-derived home venue cities. Coordinates are city centroids for the venue city, not exact arena coordinates.
- Current NBA `attended_high_school` and `attended_college` rows are Wikidata `P69` education associations. They mean attended/educated at, not confirmed sports participation.
- Current NBA `all_star_count` and `all_pro_count` are parsed from Wikipedia infobox career highlights. For NBA, `all_pro_count` means All-NBA selections.
