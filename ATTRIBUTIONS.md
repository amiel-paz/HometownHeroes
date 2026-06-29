# Attributions

HometownHeroes combines local code with third-party data snapshots and map
services. The project code is licensed under the repository `LICENSE`; source
datasets and media remain governed by their own licenses and terms.

## Map and UI

- Leaflet: https://leafletjs.com/ ; BSD-2-Clause.
- OpenStreetMap map data and standard tiles: https://www.openstreetmap.org/copyright ; map data is licensed under the Open Database License and must be credited to OpenStreetMap contributors.
- CARTO basemaps: https://carto.com/basemaps/ ; the dark basemap used by the experimental visualizer requires CARTO and OpenStreetMap attribution.
- Nominatim geocoding: https://nominatim.org/ ; used only for typed-place lookup in the local visualizer cache.

## Sports Data

- Lahman Baseball Database: https://cran.r-project.org/package=Lahman and https://github.com/cdalzell/Lahman ; used for MLB people, teams, parks, college-playing rows, All-Star rows, and Hall of Fame rows.
- Chadwick Bureau Register: https://github.com/chadwickbureau/register ; used for MLB identity resolution context.
- nflverse data: https://github.com/nflverse/nflverse-data and https://github.com/nflverse/nfldata ; used for NFL player, roster, team, and schedule-derived venue rows.
- hoopR NBA data: https://github.com/sportsdataverse/hoopR-nba-data ; CC BY 4.0 snapshots used for NBA player/team seasons, schedules, rosters, and venue city context.

## Geography and Schools

- U.S. Census Gazetteer files: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html ; used for U.S. city/place centroids.
- GeoNames: https://www.geonames.org/ ; CC BY 4.0 data used for non-U.S. MLB birthplace centroids.
- NCES EDGE public school geocode data: https://nces.ed.gov/programs/edge/Geographic/SchoolLocations ; used for U.S. public high school location matching.
- College Scorecard: https://collegescorecard.ed.gov/data/ ; used for college and university location matching.

## Wikidata, Wikipedia, and Wikimedia Commons

- Wikidata structured data: https://www.wikidata.org/wiki/Wikidata:Copyright ; CC0 data used for education, birthplace, stadium, Hall of Fame identifier, all-time NBA, and image-candidate enrichment.
- Wikipedia text: https://en.wikipedia.org/wiki/Wikipedia:Copyrights ; CC BY-SA text used for conservative parsing of infobox career highlights and high school/education clues.
- Wikimedia Commons media metadata: https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia ; image metadata and per-file licenses are cached for usable player thumbnails. Player cards display photo attribution, source links, and license labels when available.

## Local Caches

Raw source snapshots live under `data/raw/`, derived runnable outputs live under
`scratch/`, and generated SQLite databases are intentionally ignored by git.
Before publishing a hosted database or bulk export, re-check the upstream
licenses and preserve source-specific attribution in the public surface.
