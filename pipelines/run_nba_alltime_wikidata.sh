#!/usr/bin/env bash
set -euo pipefail

# Conservative, resumable all-time NBA/ABA identity enrichment from Wikidata.
#
# Seeds players from Wikidata P2685 (Basketball Reference NBA player ID), then
# caches birthplace and education associations for later warehouse integration.

CHUNK_SIZE="${CHUNK_SIZE:-200}"
MAX_CHUNKS="${MAX_CHUNKS:-0}"
SLEEP_SECONDS="${SLEEP_SECONDS:-1}"
MAX_RETRIES="${MAX_RETRIES:-20}"
RATE_LIMIT_SLEEP="${RATE_LIMIT_SLEEP:-120}"

python3 pipelines/enrich_nba_alltime_wikidata.py \
  --chunk-size "${CHUNK_SIZE}" \
  --max-chunks "${MAX_CHUNKS}" \
  --sleep-seconds "${SLEEP_SECONDS}" \
  --max-retries "${MAX_RETRIES}" \
  --rate-limit-sleep "${RATE_LIMIT_SLEEP}"
