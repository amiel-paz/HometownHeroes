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

## NBA

- hoopR NBA data: https://github.com/sportsdataverse/hoopR-nba-data
  - Repository license: CC BY 4.0.
  - Used snapshots:
    - `nba/player_season_stats/parquet/`: player/team season stats from 2002-current.
    - `nba/schedules/parquet/`: schedule rows with home venue name, city, and state.
    - `nba/rosters/parquet/`: current/recent roster rows with birthplace fields.
  - NBA pro location rows are player/team season associations joined to the team's schedule-derived home venue city centroid.
- Wikidata all-time NBA/ABA enrichment cache:
  - `data/raw/wikidata/nba_alltime/`: Basketball Reference NBA player IDs, birthplace rows, and education rows.
  - `data/raw/wikidata/nba_alltime_pro_teams/`: P54 team-membership rows filtered to major NBA/ABA/BAA/NBL league context.
  - Wikidata structured data is published under CC0.

## Education / School Geocoding

- NCES EDGE public school locations 2024-25: https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PUBLICSCH_2425.zip
  - Public school location snapshot used to canonicalize and geocode U.S. public high schools.
  - Data.gov metadata for this layer states the file is in the public domain.
- College Scorecard most-recent institution file: https://collegescorecard.ed.gov/data/
  - Used to canonicalize and geocode college and university associations.

## Geography

- U.S. Census Gazetteer 2025 place file: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html
  - Used for U.S. place centroids.
- GeoNames `cities500`, `admin1CodesASCII`, and `countryInfo` dumps: https://download.geonames.org/export/dump/
  - Used for non-U.S. MLB birthplace centroids.
  - GeoNames data is licensed CC BY 4.0.

## Wikidata

- Wikidata SPARQL education responses: data/raw/wikidata/education/
  - Cached responses for `P69` education associations.
  - Wikidata structured data is published under CC0.
- Wikidata SPARQL birthplace responses: data/raw/wikidata/birthplace/
  - Cached responses for `P19` place-of-birth associations.
  - Wikidata structured data is published under CC0.
- Wikidata SPARQL honors responses: data/raw/wikidata/honors/
  - Cached responses for HOF identifiers and English Wikipedia title maps.
  - Includes NFL `P6930` and NBA `P3646` HOF identifiers.
  - Wikidata structured data is published under CC0.
- Wikidata SPARQL stadium responses: data/raw/wikidata/stadiums/
  - Cached responses for stadium coordinates (`P625`) used by schedule-derived NFL home venues.
  - Wikidata structured data is published under CC0.
- Wikidata image-candidate responses: data/raw/wikidata/media/
  - Cached responses for player image-file candidates.
  - Wikidata structured data is published under CC0.

## Wikipedia

- Cached Wikipedia lead-section responses:
  - `data/raw/wikipedia/nfl_honors/`
  - `data/raw/wikipedia/nba_honors/`
  - `data/raw/wikipedia/player_media/`
- Used to parse infobox career highlights for All-Star/Pro Bowl and All-Pro/All-NBA counts.
- Wikipedia text is licensed under CC BY-SA; derived counts should carry attribution in public-facing exports or app documentation.

## Wikimedia Commons

- Cached Commons image metadata: data/raw/wikimedia_commons/
  - Used to retrieve thumbnail URLs, source page URLs, author/attribution text, and per-file license metadata for player photos.
  - Public player cards should display the cached attribution and license fields when a thumbnail is shown.

## Map / Visualizer

- Leaflet: https://leafletjs.com/
- OpenStreetMap tiles/data: https://www.openstreetmap.org/copyright
- CARTO dark basemap tiles: https://carto.com/basemaps/
- Nominatim typed-place geocoding: https://nominatim.org/

## Notes

- These are raw source snapshots only.
- No Sports Reference page crawl has been attempted.
- Public app surfaces should link to the root `ATTRIBUTIONS.md`.
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
