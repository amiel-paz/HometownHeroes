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

- Local: `scratch/hometown_heroes.sqlite`.
- Hosted: managed Postgres with PostGIS loaded from the same source caches.
- Keep the durable model PostGIS-friendly: canonical places with latitude and
  longitude, association/event rows pointing to places, and query logic that can
  be expressed in SQL.
