#!/usr/bin/env python3
"""
Build the SQLite application database during a Render deploy.

This keeps generated SQLite files out of git while making the hosted service
start from the same reproducible pipeline used locally.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SCRATCH = ROOT / "scratch"
APP_DB = SCRATCH / "HometownHeroes.sqlite"
DERIVED = ROOT / "data" / "derived"
PLAYER_MEDIA_DB = SCRATCH / "player_media.sqlite"
PLAYER_MEDIA_ARTIFACT = DERIVED / "player_media.sqlite.gz"
BIRTH_AUDIT_FIXES_DB = SCRATCH / "birthplace_audit_fixes.sqlite"
BIRTH_AUDIT_FIXES_ARTIFACT = DERIVED / "birthplace_audit_fixes.sqlite.gz"
APP_DB_ARTIFACT = SCRATCH / "HometownHeroes.sqlite.gz"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run(args: list[str]) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def artifact_headers(url: str) -> dict[str, str]:
    headers = {"User-Agent": "HometownHeroesRenderBuild/0.1"}
    token = os.environ.get("HH_DB_ARTIFACT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "api.github.com" in url and "/releases/assets/" in url:
        headers["Accept"] = "application/octet-stream"
    return headers


def github_api_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "HometownHeroesRenderBuild/0.1",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("HH_DB_ARTIFACT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_github_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=github_api_headers())
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_app_database_artifact_url() -> str:
    url = os.environ.get("HH_DB_ARTIFACT_URL", "").strip()
    if url:
        return url

    repo = os.environ.get("HH_DB_ARTIFACT_REPO", "").strip()
    tag = os.environ.get("HH_DB_ARTIFACT_RELEASE_TAG", "").strip()
    asset_name = os.environ.get("HH_DB_ARTIFACT_ASSET_NAME", APP_DB_ARTIFACT.name).strip()
    if not repo or not tag:
        return ""

    encoded_tag = urllib.parse.quote(tag, safe="")
    release_url = f"https://api.github.com/repos/{repo}/releases/tags/{encoded_tag}"
    release = request_github_json(release_url)
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name and asset.get("url"):
            print(f"resolved release artifact {repo}@{tag}:{asset_name} to asset id {asset.get('id')}", flush=True)
            return str(asset["url"])
    raise RuntimeError(f"release asset not found: repo={repo} tag={tag} asset={asset_name}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hydrate_app_database_from_url() -> bool:
    url = resolve_app_database_artifact_url()
    if not url:
        return False

    expected_sha256 = os.environ.get("HH_DB_ARTIFACT_SHA256", "").strip().lower()
    APP_DB_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    tmp_gz = APP_DB_ARTIFACT.with_suffix(APP_DB_ARTIFACT.suffix + ".tmp")
    tmp_db = APP_DB.with_suffix(APP_DB.suffix + ".tmp")

    print(f"hydrating app database artifact from {url}", flush=True)
    request = urllib.request.Request(url, headers=artifact_headers(url))
    with urllib.request.urlopen(request, timeout=600) as response, tmp_gz.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp_gz.replace(APP_DB_ARTIFACT)

    observed_sha256 = sha256_file(APP_DB_ARTIFACT)
    print(f"downloaded app database artifact sha256={observed_sha256}", flush=True)
    if expected_sha256 and observed_sha256 != expected_sha256:
        raise RuntimeError(
            "HH_DB_ARTIFACT_SHA256 mismatch: "
            f"expected {expected_sha256}, observed {observed_sha256}"
        )

    APP_DB.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(APP_DB_ARTIFACT, "rb") as source, tmp_db.open("wb") as target:
        shutil.copyfileobj(source, target)
    tmp_db.replace(APP_DB)
    maybe_copy_database_to_runtime_path()
    print(f"Render deploy database hydrated from artifact: {APP_DB}", flush=True)
    return True


def maybe_copy_database_to_runtime_path() -> None:
    runtime_db = os.environ.get("HH_DB_PATH")
    if not runtime_db:
        return
    target = Path(runtime_db)
    if target.resolve() == APP_DB.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(APP_DB, target)
    print(f"copied deploy database to {target}", flush=True)


def hydrate_player_media_from_artifact() -> None:
    if PLAYER_MEDIA_DB.exists():
        print(f"using existing media cache: {PLAYER_MEDIA_DB}", flush=True)
        return
    if not PLAYER_MEDIA_ARTIFACT.exists():
        print(f"media artifact not found; player photos will be skipped: {PLAYER_MEDIA_ARTIFACT}", flush=True)
        return

    PLAYER_MEDIA_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYER_MEDIA_DB.with_suffix(PLAYER_MEDIA_DB.suffix + ".tmp")
    print(f"hydrating media cache from {PLAYER_MEDIA_ARTIFACT}", flush=True)
    with gzip.open(PLAYER_MEDIA_ARTIFACT, "rb") as source, tmp.open("wb") as target:
        shutil.copyfileobj(source, target)
    tmp.replace(PLAYER_MEDIA_DB)


def hydrate_birthplace_audit_fixes_from_artifact() -> None:
    if BIRTH_AUDIT_FIXES_DB.exists():
        print(f"using existing birthplace audit fixes cache: {BIRTH_AUDIT_FIXES_DB}", flush=True)
        return
    if not BIRTH_AUDIT_FIXES_ARTIFACT.exists():
        print(f"birthplace audit fixes artifact not found; no audit fixes will be applied: {BIRTH_AUDIT_FIXES_ARTIFACT}", flush=True)
        return

    BIRTH_AUDIT_FIXES_DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = BIRTH_AUDIT_FIXES_DB.with_suffix(BIRTH_AUDIT_FIXES_DB.suffix + ".tmp")
    print(f"hydrating birthplace audit fixes from {BIRTH_AUDIT_FIXES_ARTIFACT}", flush=True)
    with gzip.open(BIRTH_AUDIT_FIXES_ARTIFACT, "rb") as source, tmp.open("wb") as target:
        shutil.copyfileobj(source, target)
    tmp.replace(BIRTH_AUDIT_FIXES_DB)


def main() -> int:
    SCRATCH.mkdir(exist_ok=True)
    if hydrate_app_database_from_url():
        return 0
    if env_flag("HH_RENDER_SKIP_DATA_BUILD"):
        print("HH_RENDER_SKIP_DATA_BUILD is set; skipping data build.", flush=True)
        return 0

    sleep_seconds = os.environ.get("HH_RENDER_SLEEP_SECONDS", "0.2")

    commands = [
        [PYTHON, "pipelines/ingest_mlb.py"],
        [PYTHON, "pipelines/ingest_nfl.py"],
        [PYTHON, "pipelines/ingest_nba.py"],
        [PYTHON, "pipelines/ingest_nhl.py"],
        [PYTHON, "pipelines/ingest_nfl_stadiums.py", "--sleep-seconds", sleep_seconds],
        [PYTHON, "pipelines/build_pro_venue_stints.py"],
        [
            PYTHON,
            "pipelines/enrich_wikidata_education.py",
            "--sports",
            "MLB,NFL,NBA",
            "--chunk-size",
            "200",
            "--max-chunks",
            "0",
            "--sleep-seconds",
            sleep_seconds,
        ],
        [
            PYTHON,
            "pipelines/enrich_wikidata_birthplace.py",
            "--sports",
            "NFL,NBA",
            "--chunk-size",
            "200",
            "--max-chunks",
            "0",
            "--sleep-seconds",
            sleep_seconds,
        ],
        [
            PYTHON,
            "pipelines/enrich_player_honors.py",
            "--sports",
            "MLB,NFL,NBA",
            "--include-wikipedia-nfl",
            "--include-wikipedia-nba",
            "--chunk-size",
            "200",
            "--wikipedia-chunk-size",
            "50",
            "--max-chunks",
            "0",
            "--max-wikipedia-chunks",
            "0",
            "--sleep-seconds",
            sleep_seconds,
        ],
    ]

    if not env_flag("HH_RENDER_SKIP_NBA_ALLTIME"):
        commands.extend(
            [
                [
                    PYTHON,
                    "pipelines/enrich_nba_alltime_wikidata.py",
                    "--chunk-size",
                    "200",
                    "--max-chunks",
                    "0",
                    "--sleep-seconds",
                    sleep_seconds,
                ],
                [
                    PYTHON,
                    "pipelines/enrich_nba_alltime_pro_teams.py",
                    "--chunk-size",
                    "200",
                    "--max-chunks",
                    "0",
                    "--sleep-seconds",
                    sleep_seconds,
                ],
            ]
        )

    if env_flag("HH_RENDER_INCLUDE_MEDIA"):
        commands.append(
            [
                PYTHON,
                "pipelines/enrich_player_media.py",
                "--sports",
                "MLB,NFL,NBA,NHL",
                "--chunk-size",
                "200",
                "--imageinfo-chunk-size",
                "20",
                "--max-chunks",
                "0",
                "--max-imageinfo-chunks",
                "0",
                "--sleep-seconds",
                os.environ.get("HH_RENDER_MEDIA_SLEEP_SECONDS", "6"),
                "--max-retries",
                "20",
                "--rate-limit-sleep",
                "120",
                "--skip-wikipedia-fallback",
            ]
        )
    else:
        hydrate_player_media_from_artifact()

    hydrate_birthplace_audit_fixes_from_artifact()
    commands.append([PYTHON, "pipelines/build_database.py"])

    for command in commands:
        run(command)

    maybe_copy_database_to_runtime_path()
    print(f"Render deploy database ready: {APP_DB}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
