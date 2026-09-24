# Refreshing all supported data

Use this runbook for a current-source refresh, not simply reproducing a snapshot.
Commands run from the repository root with Python 3.11 or newer. This is an
agent-assisted procedure: there is currently no single automatic refresh command.

## 1. Establish a baseline and isolate the work

Inspect the remote, branch, working-tree changes, source manifest, and pipeline
code. Clone the actual remote or create a separate checkout; carry over any
intended uncommitted code deliberately. Never discard the user's working tree.
Use a fresh `scratch/` in the isolated checkout. Preserve the previous validated
warehouse, compressed release artifact, checksum, report, and curated overrides
outside that checkout. If a database is actively written, use SQLite's backup
API rather than copying an open database file.

Record baseline counts by sport, event type, populated field, and maximum pro
season. A clone includes some old raw caches: a fresh clone is not fresh data.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[notebook]"
```

## 2. Acquire current sources before rebuilding

Read `data/manifests/sources.md` and `ATTRIBUTIONS.md`. Verify upstream releases
and schemas at execution time. For each source record retrieval time in UTC,
URL, upstream version/commit or season, local path, SHA-256, and outcome. Retain
prior snapshots for rollback. Download to temporary files and validate before
replacing inputs; do not accept an HTML error page as CSV, JSON, or an archive.

| Fields / source | Required refresh action |
| --- | --- |
| MLB identities, births, careers, schools, teams, honors: Lahman and Chadwick | Find the current Lahman release and Chadwick commit using the manifest's upstream links. Download/extract them. Update `BASE` and `CHADWICK_DATA` in `ingest_mlb.py` and any other consumers found by searching for the old paths. The ingest does not fetch these registers for you. |
| NFL identities and career bounds: nflverse players | Fetch the current player master to a new dated snapshot; update `NFL_PLAYERS_CSV` and any other consumers of the old path. |
| NFL pro associations: nflverse rosters and schedules | Refresh `data/raw/nfl/nflverse-rosters/roster_{season}.csv`, including newly available seasons and revisions. Discover available release assets rather than assuming the calendar year exists. Stadium ingest reads local rosters; it does not acquire them. Use its `--refresh-downloads` flag for schedules and stadium responses. |
| NBA players, rosters, schedules: hoopR | Archive/remove the relevant `data/raw/nba/hoopr-nba-data/` caches in the isolated checkout. A nonempty parquet directory makes the fetcher skip discovery, including new seasons. |
| NHL bios, Hall of Fame, team seasons | Archive/remove `data/raw/nhl/` in the isolated checkout so records, season listings, and paginated stats are fetched again. |
| School names and coordinates: NCES, College Scorecard | Discover current releases. Update URL, path, archive member, and schema assumptions in all consumers. Existing code pins release dates. |
| Place coordinates: Census and GeoNames | Discover current Census vintage and update both download and internal ZIP-member names in all consumers. Refresh GeoNames dumps. Coordinate changes require rebuilding all dependent sports. |
| NFL optional school enrichment: CollegeFootballData | Inspect `CFBD_API_KEY` availability without printing it and update available year ranges. If unavailable, record this source as skipped and quantify any coverage loss. Never silently replace a richer validated result. |
| Education, births, NBA all-time identities/teams, honors, photos | Refresh corresponding Wikidata, Wikipedia, and Commons raw caches together with derived databases, as described below. |
| Reviewed corrections and venue overlays | Preserve `data/curation/`; check whether new source data changes the evidence. Change overrides only with documented evidence. |

Search for old version strings throughout `pipelines/` after changing a source.
Update the source manifest and attribution if required. A source with no newer
release can be recorded as checked/current at its published vintage; never
claim the source covers seasons or fields it does not provide.

### Cache invalidation

In the isolated checkout, archive or move aside the response directories for
each refreshed enrichment: `data/raw/wikidata/education`, `birthplace`, `honors`,
`stadiums`, `nba_alltime`, `nba_alltime_pro_teams`, `media`, `birthplace_audit`;
`data/raw/wikipedia/nfl_honors`, `nba_honors`, `player_media`; and
`data/raw/wikimedia_commons/player_media`. Clear the corresponding generated
SQLite outputs in that checkout, not the baseline backup. School Wikipedia
responses also live in `scratch/wikipedia_high_school_cache`.

Some response filenames identify only a chunk number. If the player set, order,
or chunk size changes, invalidate the whole affected response series and its
derived database. Resetting just the database can reuse responses for the wrong
set of players. For a resumed interrupted run, keep input identities and chunk
sizes fixed. Do not clear successful new chunks just to retry a transient error.

Do not hydrate old `data/derived/` artifacts and call them freshly enriched.
Existing reviewed fixes can be carried forward with their original provenance;
report that explicitly and recheck their applicability.

## 3. Rebuild in dependency order

Run each command to completion and inspect its summary/errors before proceeding.
These commands assume step 2 has actually refreshed or invalidated the inputs.

```bash
python3 pipelines/ingest_mlb.py
python3 pipelines/ingest_nfl.py
python3 pipelines/ingest_nba.py
python3 pipelines/ingest_nhl.py
python3 pipelines/ingest_nfl_stadiums.py --refresh-downloads --sleep-seconds 1
python3 pipelines/build_pro_venue_stints.py
python3 pipelines/enrich_wikidata_education.py --sports MLB,NFL,NBA --chunk-size 200 --max-chunks 0 --sleep-seconds 1
python3 pipelines/enrich_wikidata_birthplace.py --sports MLB,NFL,NBA --chunk-size 200 --max-chunks 0 --sleep-seconds 1
bash pipelines/run_nba_alltime_wikidata.sh
bash pipelines/run_nba_alltime_pro_teams.sh
python3 pipelines/enrich_player_honors.py --sports MLB,NFL,NBA --include-wikipedia-nfl --include-wikipedia-nba --chunk-size 200 --wikipedia-chunk-size 50 --max-chunks 0 --max-wikipedia-chunks 0 --sleep-seconds 1
python3 pipelines/enrich_player_media.py --sports MLB,NFL,NBA,NHL --chunk-size 200 --imageinfo-chunk-size 20 --max-chunks 0 --max-imageinfo-chunks 0 --sleep-seconds 6 --max-retries 20 --rate-limit-sleep 120
python3 pipelines/build_database.py
python3 pipelines/curate_high_schools.py --max-pages 0 --no-title-fallback
python3 pipelines/audit_birthplace_coverage.py --include-complete-birthplaces --chunk-size 200 --max-chunks 0 --sleep-seconds 1
python3 pipelines/apply_birthplace_audit_fixes.py --apply-wikidata-birthdates --apply-wikidata-birthplaces --rebuild
python3 pipelines/audit_pro_year_coverage.py
```

The media command includes Wikipedia fallback; it can take hours. Honor rate
limits, resume partial work, and document any deliberately omitted fallback.
Inspect fetch-status tables and summaries: some scripts catch errors and still
exit successfully. Review birthplace conflict candidates individually; apply
only evidence-backed corrections using the documented reviewed-override path.

### Current implementation limits

- `build_database.py` only requires MLB, NFL, and education caches. Require all
  expected stages yourself; optional inputs can silently disappear.
- High-school curation requires the warehouse first. Its separate review cache
  and CSV exports are not currently loaded by `build_database.py`. Do not claim
  these outputs reached the app. If ingestion of those corrections is requested,
  implement and validate that integration before declaring it complete.
- NHL honors currently provide Hall of Fame information; its All-Star and
  All-Pro counts are written as zero, not established complete historical counts.
- Venue stints are a separate cache; verify their actual use in warehouse/UI
  before claiming every venue correction is visible.
- Education associations do not establish athletic participation or dates.
  NFL roster/home-venue associations do not establish game appearances.

## 4. Validate before release

Run `python3 -m unittest discover -s tests` with the rebuilt database present.
Database-dependent skips must be investigated. Do not weaken tests just to
accept changed counts; establish the source explanation first.

Use SQLite read-only inspection to require `PRAGMA integrity_check` = `ok`, no
rows from `PRAGMA foreign_key_check`, all four sports populated, and expected
media/honor/event tables populated where supported. Compare the baseline with:

```sql
SELECT sport, COUNT(*) FROM players GROUP BY sport;
SELECT sport, event_type, COUNT(*) FROM player_location_events GROUP BY sport, event_type;
SELECT sport, COUNT(birth_date), COUNT(birth_year), COUNT(debut_year), COUNT(final_year)
FROM players GROUP BY sport;
SELECT sport, MAX(end_year) FROM player_location_events WHERE event_type = 'played_pro' GROUP BY sport;
SELECT sport, COUNT(*) FROM player_media WHERE usable = 1 GROUP BY sport;
```

Also compare geocoded coverage, honor counts, unresolved schools, new player
identities, duplicates, and audit findings. Explain every material loss; do not
require every field to be non-null when upstream data does not support it.
Inspect source freshness separately: a recent maximum year proves neither
complete coverage nor fresh profiles.

Run `python3 scratch/pilot_visualizer_experiment.py`, verify `/api/status`, and
exercise searches for all four sports, historical/current players, birthplace,
school, pro years, honors, photos, and `/attributions`. Save expected API counts
and representative search results for hosted comparison.

Complete the report template, then follow `RENDER_DEPLOY.md` if hosting is in
scope. This procedure does not itself make upstream data complete or eliminate
the implementation limits above.
