# Derived Artifacts

These files are compact, deploy-time artifacts generated from reproducible
pipeline runs. They are tracked only when they materially speed up hosted
builds or avoid repeated long network enrichment during deploys.

## `birthplace_audit_fixes.sqlite.gz`

Safe missing-data fills produced by the birthplace/date audit pipeline.
Current contents:

- `player_birthdate_overrides`: Wikidata P569 birth date/year fills for players
  that were missing those fields.
- `birthplace_overrides`: reviewed birthplace conflict overrides, currently
  empty unless manually promoted after review.

Reproduce from scratch:

```bash
python3 pipelines/audit_birthplace_coverage.py \
  --include-complete-birthplaces \
  --chunk-size 200 \
  --max-chunks 0 \
  --sleep-seconds 0.5

python3 pipelines/apply_birthplace_audit_fixes.py \
  --apply-wikidata-birthdates \
  --apply-wikidata-birthplaces

gzip -n -c scratch/birthplace_audit_fixes.sqlite > data/derived/birthplace_audit_fixes.sqlite.gz
```

Then rebuild the app database:

```bash
python3 pipelines/build_database.py
```

Conflict candidates remain review-only in
`scratch/birthplace_conflict_review_candidates.json`; do not auto-promote them.
