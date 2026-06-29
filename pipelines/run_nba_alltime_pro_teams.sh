#!/usr/bin/env bash
set -euo pipefail

# Resumable all-time NBA/ABA pro-team membership enrichment from Wikidata P54.

CHUNK_SIZE="${CHUNK_SIZE:-200}"
MAX_CHUNKS="${MAX_CHUNKS:-0}"
SLEEP_SECONDS="${SLEEP_SECONDS:-1}"
MAX_RETRIES="${MAX_RETRIES:-20}"
RATE_LIMIT_SLEEP="${RATE_LIMIT_SLEEP:-120}"

python3 pipelines/enrich_nba_alltime_pro_teams.py \
  --chunk-size "${CHUNK_SIZE}" \
  --max-chunks "${MAX_CHUNKS}" \
  --sleep-seconds "${SLEEP_SECONDS}" \
  --max-retries "${MAX_RETRIES}" \
  --rate-limit-sleep "${RATE_LIMIT_SLEEP}"

python3 pipelines/build_database.py
