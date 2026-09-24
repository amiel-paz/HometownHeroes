# Refresh and deployment report

Copy to a dated report, for example `docs/reports/YYYY-MM-DD-refresh.md`.
Use actual observations; mark unknown or incomplete items explicitly. Do not
include credentials, deploy-hook URLs, or machine-specific absolute paths.

- Requested scope and as-of time (UTC):
- Repository, branch, and code commit:
- Baseline artifact/commit and backup location:
- Outcome: complete / partial / blocked, with reasons:

## Source freshness

| Source and fields | URL | Version/commit/season | Retrieved UTC | SHA-256 / snapshot manifest | Outcome and exceptions |
| --- | --- | --- | --- | --- | --- |

Include every source family in `DATA_REFRESH.md`, including checked-unchanged,
unavailable, reused, or skipped sources. Large inventories may link to a
machine-readable manifest with one entry per fetched file.

## Execution and coverage

- Cache groups invalidated; fixed chunk sizes for resumed jobs:
- Commands completed and fetch errors/retries/unprocessed chunks:
- Source schema changes and pipeline changes:
- Reviewed corrections preserved/changed and supporting evidence:
- Unsupported fields or outputs that are not integrated into the warehouse:

| Metric by sport/event/field | Before | After | Explanation for change |
| --- | --- | --- | --- |

## Validation

- SQLite integrity and foreign-key checks:
- Test command, result, skipped tests, and reasons:
- Birthplace/date and pro-year audit results:
- Expected `/api/status` counts:
- Representative searches and updated-record evidence:
- Unexplained regressions (must resolve before a complete release):

## Deployment and rollback

- Render service ID and public URL:
- Previous successful deploy ID, code commit, artifact reference and checksum:
- New deploy ID, code commit, release tag and asset ID:
- Validated compressed artifact SHA-256 and observed Render log SHA-256:
- Remote status counts and search verification:
- Verification timestamp (UTC):
- Rollback location/procedure, if needed:
- Remaining limitations or missing access:
