# Hometown Heroes

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
enrichment, honors, and geocoded association rows. It skips the slow
player-media enrichment pass. Set `HH_RENDER_INCLUDE_MEDIA=1` in Render only
when you want that deploy to refresh Wikimedia thumbnail metadata during the
build.
