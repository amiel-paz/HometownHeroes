# NBA Data Options

Cached on 2026-06-28.

## Recommended Starting Source

### hoopR NBA data

- Repository: https://github.com/sportsdataverse/hoopR-nba-data
- License: CC BY 4.0, based on the repository `LICENSE.md`.
- Useful snapshots:
  - `nba/player_season_stats/parquet/`: player/team season stats, 2002-current.
  - `nba/schedules/parquet/`: schedule rows with venue name, city, and state.
  - `nba/rosters/parquet/`: current/recent roster rows with birthplace fields.
- Fit for this project:
  - Good for NBA pro team-season associations from 2002-current.
  - Useful but incomplete for birthplaces because roster snapshots are only
    current/recent.
  - Does not directly provide high school or college history for all players.

## Conservative Enrichment Sources

### Wikidata

- License: CC0 structured data.
- Useful properties:
  - `P19`: place of birth.
  - `P69`: educated at.
  - `P3685`: ESPN.com NBA player ID.
  - `P3646`: Naismith Memorial Basketball Hall of Fame ID.
  - `P54`: member of sports team.
- Fit for this project:
  - Best next enrichment path for historical NBA birthplaces and education.
  - Requires careful identifier matching from ESPN/hoopR IDs to Wikidata items.

### Wikipedia

- License: CC BY-SA for page text.
- Useful page area:
  - Lead-section basketball infobox `highlights` rows.
- Fit for this project:
  - Current parser extracts NBA All-Star and All-NBA counts from cached page
    wikitext.
  - Raw page responses are cached under `data/raw/wikipedia/nba_honors/`.

## Deferred Sources

### nba_api

- Repository: https://github.com/swar/nba_api
- License: MIT for the client library.
- Fit for this project:
  - Useful programmatic client, but the data access pattern is API-oriented.
  - Use only after checking NBA endpoint terms and rate behavior for the exact
    endpoints needed.

### Basketball Reference / Sports Reference style pages

- Fit for this project:
  - Often rich in player biographies, awards, schools, and teams.
  - Deferred under the current conservative crawl posture unless a clearly
    permissible bulk snapshot is identified.
