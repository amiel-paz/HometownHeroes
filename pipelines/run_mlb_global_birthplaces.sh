#!/usr/bin/env bash
set -euo pipefail

# Rebuild MLB geography enrichment, including cached global GeoNames birthplace
# matches for non-US Lahman birthplaces, then refresh the unified warehouse.

PYTHON="${PYTHON:-.venv/bin/python}"

"${PYTHON}" pipelines/ingest_mlb.py
"${PYTHON}" pipelines/build_database.py
