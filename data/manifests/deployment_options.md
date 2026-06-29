# Deployment Options

Cached on 2026-06-28.

## Current Recommendation

Use SQLite as the local workshop database and design the schema so it can be
exported to Postgres/PostGIS when the app needs always-on hosting.

## SQLite

- Best for laptop experiments, reproducible crawlers, local notebooks, and
  single-user visualizer work.
- Keep raw downloads and derived caches local and rebuildable.
- Radius queries are acceptable while the number of geocoded locations remains
  modest and concurrency is not a concern.

## Postgres/PostGIS

- Best when the app is deployed for users to query any time.
- Adds managed backups, safer concurrent reads, and native spatial indexing.
- Use PostGIS once map interaction becomes the primary product surface:
  radius queries, viewport filtering, clustering, nearest-neighbor searches,
  and public traffic.

## Practical Split

- Local: `scratch/HometownHeroes.sqlite`.
- Prototype hosted on Render: build `scratch/HometownHeroes.sqlite` during
  deploy via `pipelines/render_build.py`, then serve it read-only from the
  Python visualizer process.
- Production hosted: managed Postgres with PostGIS loaded from the same source
  caches.
- Keep the durable model PostGIS-friendly: canonical places with latitude and
  longitude, association/event rows pointing to places, and query logic that can
  be expressed in SQL.

## Render Notes

- `render.yaml` is the deploy contract.
- The service binds to `0.0.0.0:$PORT`.
- `/api/status` is the health-check path.
- Player photo/media enrichment is opt-in for Render builds because Wikimedia
  image metadata fetches are intentionally slow and rate-limit aware.
- The default Render build includes the all-time NBA Wikidata enrichment unless
  `HH_RENDER_SKIP_NBA_ALLTIME=1` is set.
