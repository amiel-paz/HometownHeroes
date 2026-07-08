#!/usr/bin/env python3
"""
Package deploy-ready SQLite artifacts for GitHub Releases.

The default output is a gzip-compressed copy of scratch/HometownHeroes.sqlite
plus a sidecar SHA-256 file. Upload the .gz to a GitHub Release, then point
Render at it with HH_DB_ARTIFACT_URL and, optionally, HH_DB_ARTIFACT_SHA256.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "scratch" / "HometownHeroes.sqlite"
DEFAULT_OUT = ROOT / "scratch" / "release_artifacts" / "HometownHeroes.sqlite.gz"


def repo_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gzip_copy(source: Path, target: Path, compresslevel: int = 9) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with source.open("rb") as raw, tmp.open("wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=compresslevel, mtime=0) as gz:
            shutil.copyfileobj(raw, gz)
    tmp.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package HometownHeroes SQLite artifacts for GitHub Releases.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database to package.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Compressed artifact path to write.")
    args = parser.parse_args()

    db_path = args.db
    out_path = args.out
    if not db_path.exists():
        raise SystemExit(f"database does not exist: {db_path}")

    gzip_copy(db_path, out_path)
    checksum = sha256_file(out_path)
    checksum_path = out_path.with_suffix(out_path.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {out_path.name}\n", encoding="utf-8")
    manifest_path = out_path.with_suffix(out_path.suffix + ".json")
    manifest = {
        "artifact": repo_path(out_path),
        "source_database": repo_path(db_path),
        "sha256": checksum,
        "bytes": out_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
