#!/usr/bin/env bash
set -euo pipefail

# Conservative, resumable player-photo enrichment.
#
# Defaults focus on the MLB/NFL backlog and use Wikidata/Wikimedia Commons
# metadata only. Set INCLUDE_WIKIPEDIA_FALLBACK=1 for the slower Wikipedia
# page-image fallback pass after the Commons run has settled.

SPORTS="${SPORTS:-MLB,NFL,NBA}"
MEDIA_SPORTS="${MEDIA_SPORTS:-MLB,NFL}"
WIKIDATA_CHUNK_SIZE="${WIKIDATA_CHUNK_SIZE:-200}"
IMAGEINFO_CHUNK_SIZE="${IMAGEINFO_CHUNK_SIZE:-20}"
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
  --sports "${SPORTS}" \
  --media-sports "${MEDIA_SPORTS}" \
  --chunk-size "${WIKIDATA_CHUNK_SIZE}" \
  --imageinfo-chunk-size "${IMAGEINFO_CHUNK_SIZE}" \
  --max-chunks "${MAX_CHUNKS}" \
  --max-imageinfo-chunks "${MAX_IMAGEINFO_CHUNKS}" \
  --sleep-seconds "${SLEEP_SECONDS}" \
  --max-retries "${MAX_RETRIES}" \
  --rate-limit-sleep "${RATE_LIMIT_SLEEP}" \
  ${fallback_flag}

python3 pipelines/build_database.py
