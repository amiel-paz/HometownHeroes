#!/usr/bin/env bash
set -euo pipefail

# Resumable photo metadata enrichment for the expanded NBA all-time cache.
#
# The all-time NBA runner stores Wikidata P18 image-file candidates locally.
# This runner imports those candidates, fetches Wikimedia Commons license and
# thumbnail metadata, then rebuilds the unified SQLite warehouse.

IMAGEINFO_CHUNK_SIZE="${IMAGEINFO_CHUNK_SIZE:-20}"
MAX_IMAGEINFO_CHUNKS="${MAX_IMAGEINFO_CHUNKS:-0}"
SLEEP_SECONDS="${SLEEP_SECONDS:-6}"
MAX_RETRIES="${MAX_RETRIES:-20}"
RATE_LIMIT_SLEEP="${RATE_LIMIT_SLEEP:-120}"

python3 pipelines/enrich_player_media.py \
  --sports NBA \
  --media-sports NBA \
  --chunk-size 200 \
  --imageinfo-chunk-size "${IMAGEINFO_CHUNK_SIZE}" \
  --max-chunks 0 \
  --max-imageinfo-chunks "${MAX_IMAGEINFO_CHUNKS}" \
  --sleep-seconds "${SLEEP_SECONDS}" \
  --max-retries "${MAX_RETRIES}" \
  --rate-limit-sleep "${RATE_LIMIT_SLEEP}" \
  --skip-wikipedia-fallback

python3 pipelines/build_database.py
