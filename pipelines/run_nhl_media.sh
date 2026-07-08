#!/usr/bin/env bash
set -euo pipefail

# Resumable NHL player-photo enrichment.
#
# This uses NHL.com player IDs from scratch/nhl_enrichment.sqlite to find
# Wikidata P18 image candidates, fetches Wikimedia Commons license/thumbnail
# metadata, then rebuilds the unified SQLite warehouse. Set
# INCLUDE_WIKIPEDIA_FALLBACK=1 for a slower Wikipedia page-image fallback pass.

IMAGEINFO_CHUNK_SIZE="${IMAGEINFO_CHUNK_SIZE:-20}"
WIKIDATA_CHUNK_SIZE="${WIKIDATA_CHUNK_SIZE:-200}"
MAX_CHUNKS="${MAX_CHUNKS:-0}"
MAX_IMAGEINFO_CHUNKS="${MAX_IMAGEINFO_CHUNKS:-0}"
SLEEP_SECONDS="${SLEEP_SECONDS:-6}"
MAX_RETRIES="${MAX_RETRIES:-20}"
RATE_LIMIT_SLEEP="${RATE_LIMIT_SLEEP:-120}"
INCLUDE_WIKIPEDIA_FALLBACK="${INCLUDE_WIKIPEDIA_FALLBACK:-0}"

fallback_flag="--skip-wikipedia-fallback"
if [[ "${INCLUDE_WIKIPEDIA_FALLBACK}" == "1" ]]; then
  fallback_flag=""
fi

python3 pipelines/enrich_player_media.py \
  --sports NHL \
  --media-sports NHL \
  --chunk-size "${WIKIDATA_CHUNK_SIZE}" \
  --imageinfo-chunk-size "${IMAGEINFO_CHUNK_SIZE}" \
  --max-chunks "${MAX_CHUNKS}" \
  --max-imageinfo-chunks "${MAX_IMAGEINFO_CHUNKS}" \
  --sleep-seconds "${SLEEP_SECONDS}" \
  --max-retries "${MAX_RETRIES}" \
  --rate-limit-sleep "${RATE_LIMIT_SLEEP}" \
  ${fallback_flag}

python3 pipelines/build_database.py
