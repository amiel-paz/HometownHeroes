# Deploying a validated refresh to Render

Use after `DATA_REFRESH.md`. Prefer building locally and deploying the compressed
warehouse. The Render source-build fallback reuses snapshots, omits the school
curation/audit workflow, and is not a substitute for a validated full refresh.

## 1. Identify the existing deployment

Inspect the actual Git remote, Render service ID, connected repo/branch, public
URL, current successful deploy/commit, build/start commands, and environment
settings. `render.yaml` is a default, not proof of live configuration. Do not
invent a hostname, create a duplicate service, or change plans for a redeploy.
If the service does not exist, a hosting request may be fulfilled by creating
it from `render.yaml`; establish the intended account/repo first.

Use available authenticated tools, CLI, or dashboard. Request missing access
only when needed; continue local preparation while it is unavailable. Do not
print tokens or deploy-hook URLs. Preserve the previous database artifact and
its checksum before replacing a moving release asset. Record the previous
Render deploy ID and non-secret configuration needed for rollback.

## 2. Package and publish

```bash
python3 pipelines/package_release_artifacts.py
```

This produces `scratch/release_artifacts/HometownHeroes.sqlite.gz`, its
`.sha256` sidecar, and `.json` packaging manifest. Verify gzip decompression
matches the validated SQLite file, and independently verify the compressed
file's checksum. The packaging manifest has no upstream freshness evidence;
include the refresh report as well.

If source-build fallback must reproduce the refreshed media and audit fixes,
regenerate these helpers from validated caches, review the binary changes, and
include them in the intended Git change:

```bash
gzip -n -c scratch/player_media.sqlite > data/derived/player_media.sqlite.gz
gzip -n -c scratch/birthplace_audit_fixes.sqlite > data/derived/birthplace_audit_fixes.sqlite.gz
```

Ensure all intended code, configuration, documentation, and reproducibility
inputs are committed and pushed to the service's connected repository/branch
when authorized by the hosting request. Never commit secrets, unrelated edits,
or the ignored working databases. Record oversized/raw sources in the refresh
report with retrievable immutable locations rather than silently omitting them.

For the existing moving-release convention, verify `data-latest` exists in the
actual artifact repository and upload the following using an explicit repo:

```bash
gh release upload data-latest scratch/release_artifacts/HometownHeroes.sqlite.gz scratch/release_artifacts/HometownHeroes.sqlite.gz.sha256 scratch/release_artifacts/HometownHeroes.sqlite.gz.json --repo OWNER/REPO --clobber
```

Replace `OWNER/REPO` with the verified target. If the tag is absent, create the
release at the intended commit before uploading. Keep a separate versioned
release or external backup of the previous artifact; `--clobber` replaces it.
Avoid concurrent deployments while replacing the moving asset. Download the
published asset into a separate verification directory and compare its checksum.

For reproducible deployment/rollback, a unique release tag plus a fixed checksum
is preferable to overwriting a moving tag. The current loader supports either.

## 3. Configure and trigger a build

The repository contract is:

| Setting | Expected value |
| --- | --- |
| Build command | `pip install -e . && python pipelines/render_build.py` |
| Start command | `python scratch/pilot_visualizer_experiment.py --host 0.0.0.0 --port $PORT` |
| Health check | `/api/status` |
| `HH_DB_ARTIFACT_REPO` | Verified artifact `OWNER/REPO` |
| `HH_DB_ARTIFACT_RELEASE_TAG` | `data-latest` or the chosen immutable release |
| `HH_DB_ARTIFACT_ASSET_NAME` | `HometownHeroes.sqlite.gz` |
| `HH_DB_ARTIFACT_TOKEN` | Secret read-only GitHub access if the release is private |
| `HH_DB_ARTIFACT_URL` | Unset for tag/name resolution; otherwise overrides it |
| `HH_DB_ARTIFACT_SHA256` | New compressed artifact checksum for a pinned release; update every replacement if set on a moving tag |

The existing moving-tag convention permits a blank checksum. If using that
mode, still verify the uploaded checksum against Render's download log. Never
leave the old checksum configured after replacing an artifact. Sidecar uploads
alone do not configure checksum validation: the loader reads the environment.
Leave `HH_RENDER_SKIP_DATA_BUILD` unset for normal builds. Inspect `HH_DB_PATH`
if configured so the process serves the database that was actually hydrated.

Artifact hydration returns early from `render_build.py`; it skips enrichment.
`HH_RENDER_INCLUDE_MEDIA=1` therefore does not refresh a hydrated artifact.
Uploading to GitHub Releases alone does not trigger Render in this repository.

Trigger an actual build using **Manual Deploy → Deploy latest commit**, or the
authenticated CLI/API equivalent. If changing environment variables, use
**Save, rebuild, and deploy**. A restart or reuse of an existing build does not
run the database download step. See Render's official
[deployment](https://render.com/docs/deploys) and
[environment variable](https://render.com/docs/configure-environment-variables)
documentation; these procedures were checked on 2026-09-21 and should be
rechecked if the platform changes.

## 4. Verify the hosted result

Wait for the specific deployment to finish. Inspect its logs for the expected
commit, resolved release asset, downloaded SHA-256, successful hydration, and
server startup. Compare the log checksum to the validated artifact.

Fetch the actual public URL's `/api/status`; require HTTP 200 and exact expected
`players`, `events`, and `geocoded_events` counts. These counts alone do not
identify an artifact: corroborate them with the checksum and deploy logs.
Repeat the local representative searches through the hosted UI or
`POST /api/search`, including a newly updated record. Check map behavior, all
four sports, photos/attribution, and `/attributions`. Investigate stale browser
state or a wrong `HH_DB_PATH` if hosted results disagree.

Record deploy ID, commit, release tag/asset ID, checksum, public URL, observed
counts, checks, and remaining source limitations. Do not report success merely
because a deployment was queued or the health check passed.

## 5. Recovery

If validation fails, preserve the failed build's logs/report. Restore the
previous immutable artifact reference and matching checksum, then build and
deploy the compatible previous code, or use Render's previous successful deploy
where available. Verify public counts and queries again. Rebuilding an old code
commit while it still points to a replaced `data-latest` asset is not a data
rollback. Avoid overwriting the only known-good artifact during recovery.
