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
python3 pipelines/enrich_player_media.py --sports MLB,NFL,NBA --chunk-size 200 --imageinfo-chunk-size 20 --max-chunks 0 --max-imageinfo-chunks 0 --sleep-seconds 6 --max-retries 20 --rate-limit-sleep 120 --skip-wikipedia-fallback
python3 pipelines/build_database.py
```

Generated SQLite and CSV outputs are written under `scratch/`. Raw Wikidata, Wikipedia, and Wikimedia Commons responses are cached under `data/raw/` so interrupted runs can resume without refetching completed chunks.

MLB birthplace geocoding uses the US Census Gazetteer for US places and
GeoNames `cities500`, `admin1CodesASCII`, and `countryInfo` dumps for non-US
Lahman birthplaces. GeoNames data is cached under
`data/raw/geography/geonames/` and is licensed CC BY 4.0, so downstream views
need GeoNames attribution when those coordinates are shown.

To refresh only MLB geography plus the unified warehouse:

```bash
bash pipelines/run_mlb_global_birthplaces.sh
```

## Player Media Resume Runner

For a conservative MLB/NFL player-photo enrichment pass:

```bash
bash pipelines/run_player_media_enrichment.sh
```

The wrapper defaults to:

- `MEDIA_SPORTS=MLB,NFL`
- `IMAGEINFO_CHUNK_SIZE=20`
- `SLEEP_SECONDS=6`
- `MAX_RETRIES=20`
- `RATE_LIMIT_SLEEP=120`
- Wikipedia fallback disabled

Useful overrides:

```bash
MEDIA_SPORTS=NFL SLEEP_SECONDS=10 bash pipelines/run_player_media_enrichment.sh
MEDIA_SPORTS=MLB MAX_IMAGEINFO_CHUNKS=100 bash pipelines/run_player_media_enrichment.sh
INCLUDE_WIKIPEDIA_FALLBACK=1 SLEEP_SECONDS=10 bash pipelines/run_player_media_enrichment.sh
```

Current MLB/NFL Commons backlog is about 18,100 Wikidata image candidates
before Commons license filtering. With 20 files per request, expect roughly
860 imageinfo requests remaining. At 6 seconds between requests and occasional
rate-limit pauses, the Commons-only MLB/NFL pass should be treated as a
2-4 hour run. At 10 seconds between requests, budget 3-6 hours. The Wikipedia
page-image fallback should be run separately and more slowly.

## NBA All-Time Wikidata Runner

The current hoopR NBA cache starts at 2002. To seed a broader historical
NBA/ABA player universe from Wikidata's Basketball Reference NBA player ID
property:

```bash
bash pipelines/run_nba_alltime_wikidata.sh
```

This caches all-time player identities, birthplace rows, and education rows
under `scratch/nba_alltime_wikidata.sqlite` and raw SPARQL responses under
`data/raw/wikidata/nba_alltime/`. It does not scrape Basketball-Reference pages.

After the all-time NBA cache exists, fetch Commons license/thumbnail metadata
for the expanded NBA player universe:

```bash
bash pipelines/run_nba_alltime_media.sh
```

The all-time photo runner imports cached Wikidata image-file candidates from
`scratch/nba_alltime_wikidata.sqlite`, fetches Wikimedia Commons `imageinfo`
metadata, and rebuilds `scratch/hometown_heroes.sqlite`.

To add all-time NBA/ABA pro-team memberships from Wikidata P54:

```bash
bash pipelines/run_nba_alltime_pro_teams.sh
```

These rows become `played_pro` events when Wikidata identifies a major pro
league such as the NBA, ABA, BAA, or NBL. Location coordinates prefer team
headquarters/city over current arena coordinates to reduce historical
anachronism.

## Year Semantics

- `played_pro` rows may carry `start_year`, `end_year`, and `duration_years` only when a source provides season-level professional participation. The unified database exposes those rows through `pro_career_summary`.
- Current NFL `played_pro` rows are derived from nflverse roster seasons joined to schedule-inferred home stadiums. They mean roster membership associated with a team home stadium, not confirmed game appearances.
- `attended_high_school`, `attended_college`, and `attended_school` are association rows unless their source explicitly provides years. Do not infer high school or college dates from age, draft year, or pro debut.
- Current NFL `rookie_season` and `last_season` fields from nflverse are retained in the source cache, but are not loaded as canonical `players.debut_year` / `players.final_year`.
- Current NBA `played_pro` rows are derived from hoopR player/team season rows joined to schedule-derived home venue cities. Coordinates are city centroids for the venue city, not exact arena coordinates.
- Current NBA `attended_high_school` and `attended_college` rows are Wikidata `P69` education associations. They mean attended/educated at, not confirmed sports participation.
- Current NBA `all_star_count` and `all_pro_count` are parsed from Wikipedia infobox career highlights. For NBA, `all_pro_count` means All-NBA selections.
- Player profile photos are discovered through Wikidata/Wikimedia metadata. Sports Reference-style IDs are used only for identity matching; photos are not downloaded from Sports Reference.
