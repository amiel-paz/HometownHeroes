# HometownHeroes

Experimental sports geolocation data pipelines and a tracked prototype
visualizer for exploring athlete hometown, school, college, and pro-location
associations.

## Sanity Checks

```bash
python -m unittest discover -s tests
```

The current tests cover the visualizer query taxonomy, attribution surface,
optional local SQLite query behavior, and a tracked-text scrub for local
machine or identity fingerprints.

## Attribution

See `ATTRIBUTIONS.md` before publishing app views, exports, or hosted data.

## Render Prototype Deploy

This repo includes a minimal `render.yaml` blueprint for the tracked prototype.
Render builds the generated SQLite database during deploy and starts the app on
`0.0.0.0:$PORT`.

The default Render build includes MLB, NFL, current NBA, all-time NBA Wikidata
enrichment, honors, geocoded association rows, and the compressed derived media
cache at `data/derived/player_media.sqlite.gz`. Set `HH_RENDER_INCLUDE_MEDIA=1`
in Render only when you want that deploy to refresh Wikimedia thumbnail
metadata during the build instead of hydrating the cached artifact.

For faster free-tier deploys, Render can hydrate a prebuilt SQLite database
from a GitHub Release asset instead of running the data pipeline. Build locally,
package the artifact, upload it to a release, then set these Render variables:

```bash
python pipelines/build_database.py
python pipelines/package_release_artifacts.py
```

- `HH_DB_ARTIFACT_URL`: URL for `HometownHeroes.sqlite.gz`.
- `HH_DB_ARTIFACT_SHA256`: optional checksum from the packaging output.
- `HH_DB_ARTIFACT_TOKEN`: optional GitHub token for private release assets.

For private repositories, prefer the GitHub API release-asset URL
`https://api.github.com/repos/OWNER/REPO/releases/assets/ASSET_ID` and a
read-only token stored in Render as `HH_DB_ARTIFACT_TOKEN`. If
`HH_DB_ARTIFACT_URL` is set, `pipelines/render_build.py` downloads and verifies
the compressed database, skips the expensive ingest/enrichment steps, and
starts the app from the hydrated SQLite file.
