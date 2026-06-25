# Data Pipelines

These scripts rebuild the local enrichment caches and unified SQLite database from the source snapshots under `data/raw`.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

## Full Rebuild

```bash
python3 pipelines/ingest_mlb.py
python3 pipelines/ingest_nfl.py
python3 pipelines/enrich_wikidata_education.py --sports MLB,NFL --chunk-size 200 --max-chunks 0 --sleep-seconds 0.5
python3 pipelines/enrich_wikidata_birthplace.py --sports NFL --chunk-size 200 --max-chunks 0 --sleep-seconds 0.5
python3 pipelines/curate_high_schools.py --max-pages 0 --no-title-fallback
python3 pipelines/build_database.py
```

Generated SQLite and CSV outputs are written under `scratch/`. Raw Wikidata responses are cached under `data/raw/wikidata/` so interrupted runs can resume without refetching completed chunks.
