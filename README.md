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
package the artifact, and upload it to the stable `data-latest` release:

```bash
python pipelines/build_database.py
python pipelines/package_release_artifacts.py
gh release upload data-latest scratch/release_artifacts/HometownHeroes.sqlite.gz --clobber
```

Render can resolve the current asset ID from a stable release tag and asset
name, so future artifact uploads do not require changing environment variables:

- `HH_DB_ARTIFACT_REPO`: `amiel-paz/HometownHeroes`
- `HH_DB_ARTIFACT_RELEASE_TAG`: `data-latest`
- `HH_DB_ARTIFACT_ASSET_NAME`: `HometownHeroes.sqlite.gz`
- `HH_DB_ARTIFACT_TOKEN`: GitHub read-only token for private release assets.
- `HH_DB_ARTIFACT_SHA256`: optional fixed checksum; leave blank for a moving
  `data-latest` artifact.

If `HH_DB_ARTIFACT_URL` is set, it takes precedence over the tag/name resolver.
That direct URL mode is useful for pinned historical releases, but it requires
updating the URL when a new asset ID is created.
