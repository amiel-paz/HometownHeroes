# Data Sources

Acquired on 2026-06-24.

## MLB

- Lahman CRAN package 14.0-0: https://cran.r-project.org/src/contrib/Lahman_14.0-0.tar.gz
  - Current R package snapshot of Sean Lahman's Baseball Database, covering 1871-2025.
  - Used because the official SABR comma-delimited Box link is browser/JavaScript-oriented and did not expose a simple static download endpoint during this pass.
- Lahman GitHub snapshot: https://github.com/cdalzell/Lahman/archive/refs/heads/master.zip
  - Repository snapshot for package source context and metadata.
- Chadwick Bureau Register: https://github.com/chadwickbureau/register/archive/refs/heads/master.zip
  - Public baseball identity register, useful for player/entity resolution.

## NFL

- nflverse player master: https://github.com/nflverse/nflverse-data/releases/download/players/players.csv
- nflverse player master parquet: https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet
- nflverse seasonal rosters: https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv
- nflverse team metadata: https://github.com/nflverse/nflverse-data/releases/download/teams/teams_colors_logos.csv
- nflverse / Lee Sharpe schedules: https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
  - Used for schedule-derived team-season home stadiums from 1999 onward.

## Education / School Geocoding

- NCES EDGE public school locations 2024-25: https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PUBLICSCH_2425.zip
  - Public school location snapshot used to canonicalize and geocode U.S. public high schools.
  - Data.gov metadata for this layer states the file is in the public domain.

## Wikidata

- Wikidata SPARQL education responses: data/raw/wikidata/education/
  - Cached responses for `P69` education associations.
  - Wikidata structured data is published under CC0.
- Wikidata SPARQL birthplace responses: data/raw/wikidata/birthplace/
  - Cached responses for `P19` place-of-birth associations.
  - Wikidata structured data is published under CC0.
- Wikidata SPARQL stadium responses: data/raw/wikidata/stadiums/
  - Cached responses for stadium coordinates (`P625`) used by schedule-derived NFL home venues.
  - Wikidata structured data is published under CC0.

## Notes

- These are raw source snapshots only.
- No Sports Reference page crawl has been attempted.
- Before public redistribution, review each upstream source's license and terms.

## Snapshot Inventory

- Total raw snapshot size after unpacking: about 174 MB.
- Total raw files after unpacking: 485.
- MLB Lahman CRAN package includes the key early tables for this project:
  - `People`: 24,270 documented rows, including birth city/state/country fields.
  - `CollegePlaying`: 17,687 documented rows.
  - `Schools`: 1,287 documented rows.
  - `Teams`: 3,614 documented rows.
  - `HomeGames`: 3,303 documented rows.
- Chadwick Register public people files:
  - 16 `people-*.csv` files.
  - 516,122 total CSV lines including headers.
- NFL nflverse files:
  - `players.csv`: 25,033 lines including header.
  - `players.parquet`: downloaded alongside CSV.
  - `roster_1999.csv` through `roster_2025.csv`: 27 seasonal roster files.
  - NFL roster files total 66,507 lines including headers.
  - `teams_colors_logos.csv`: 37 lines including header.
  - `games.csv`: schedule rows with `stadium_id` and `stadium` fields for 1999 onward.
