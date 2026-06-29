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
