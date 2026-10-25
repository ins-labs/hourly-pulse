# Cloudflare Worker + D1 Hourly Pulse Dispatcher Runbook

## Overview & Architecture

This repository contains the source, database schema, and deployment configuration for an external Cloudflare Worker dispatcher that triggers the GitHub Actions hourly pulse workflow (`.github/workflows/pulse.yml`) in `ins-labs/hourly-pulse`.

### Design & Guiding Principles
- **At-most-one dispatcher winning per slot**: Atomic `UNIQUE` key constraint on `pulse_claims.slot_utc` in Cloudflare D1 guarantees that across duplicate or concurrent Worker invocations, at most one Worker claims a UTC hour slot.
- **Fail-closed D1 errors**: If D1 is unavailable or throws an error, the execution fails closed immediately without POSTing to GitHub API.
- **Duplicate Invocations**: Losing workers or re-runs on an already claimed slot never issue a `POST`.
- **Pre-POST Run Check**: Before issuing a POST, the winning worker queries existing workflow runs for the UTC hour slot. If a manual run or existing execution is present, dispatch is suppressed (`SUPPRESSED_PRE_POST`).
- **Ambiguous POST Receipts**: If a POST request is issued but experiences a network error, HTTP 5xx, or dropped TCP connection, the slot is marked `AMBIGUOUS`. The worker **NEVER** retries a POST in the same slot to guarantee no duplicate dispatches occur.
- **Exact Run Binding**: After dispatch, list-runs reconciliation matches exactly one candidate run created within the UTC slot for `ref: main`. If multiple runs match, the slot is marked `AMBIGUOUS` (no guessing). The exact `workflow_run_id` is then polled until concluded or until the network budget (<= 9 calls per slot) is exhausted.

---

## D1 Database Schema (`schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS pulse_claims (
    slot_utc TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    dispatch_attempted_at TEXT,
    workflow_run_id INTEGER,
    workflow_run_url TEXT,
    conclusion TEXT,
    error_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Claim State Machine
- `CLAIMED`: Worker successfully claimed the UTC slot in D1.
- `DISPATCHED`: Dispatch POST request completed successfully (or direct 200 response received).
- `BOUND`: Reconciled and bound to exact `workflow_run_id`.
- `CONCLUDED`: Bound workflow run finished with a terminal conclusion (e.g., `success`, `failure`).
- `AMBIGUOUS`: Network failure during/after POST, non-2xx dispatch response, or multiple run candidates found.
- `SUPPRESSED_PRE_POST`: Existing workflow run already present for the current UTC slot.
- `INCOMPLETE_BUDGET_EXHAUSTED`: Network call budget (<= 9 calls) reached before binding or conclusion completed.

---

## Deployment & Cron Activation Configuration

The checked-in `wrangler.toml` file is **cron-inactive by default** (`triggers = { crons = [] }`).
To activate scheduled dispatches in production, apply the explicit separate activation template below.

### Cron Activation Template (`wrangler.prod.toml`)
```toml
name = "hourly-pulse-dispatcher"
main = "src/worker.js"
compatibility_date = "2024-05-01"

# Active hourly cron schedule at top of the hour (:00 UTC)
triggers = { crons = ["0 * * * *"] }

[[d1_databases]]
binding = "DB"
database_name = "hourly-pulse-claims-db"
database_id = "<YOUR_PRODUCTION_D1_DATABASE_ID>"

[vars]
GITHUB_TOKEN = "<SET_VIA_WRANGLER_SECRET_GITHUB_TOKEN>"
```

*Note: Never commit `GITHUB_TOKEN` values into repository files. Set secrets via `wrangler secret put GITHUB_TOKEN`.*

---

## Operational Cutover & Rollback Plan

### Cutover Plan
1. **Deploy Inactive Worker**: Provision D1 database (`schema.sql`) and deploy Worker with cron-inactive configuration (`triggers = { crons = [] }`).
2. **Review & Apply Schedule Removal Patch**: Review and apply `docs/schedule-removal.patch` in a separate PR to remove `schedule:` cron from `.github/workflows/pulse.yml`.
3. **Verify GitHub Actions Schedule Disabled**: Confirm `.github/workflows/pulse.yml` no longer triggers automatically on GitHub schedule.
4. **Enable `:00` Worker Cron**: Update Worker trigger configuration to `0 * * * *` and deploy.
5. **Verify First Hourly Execution**: Confirm D1 claim record transitions (`CLAIMED` -> `DISPATCHED` -> `BOUND` -> `CONCLUDED`).

### Rollback Plan
1. **Disable Worker First**: Re-deploy Worker with `triggers = { crons = [] }` or remove cron trigger in Cloudflare dashboard.
2. **Restore Actions Schedule**: Revert schedule-removal patch on `.github/workflows/pulse.yml` to restore GitHub native `schedule` cron.
3. **Verify Schedule Operational**: Confirm GitHub Actions resume scheduled pulse runs.

*Rule: Never have both Cloudflare Worker cron and GitHub Actions schedule active at the same time.*

---

## Test Verification & Operational Limitations

### Local Test Execution Commands & Results
- **Python Pulse Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest discover -s tests -v
  ```
  Result: 12 tests passed (0 failures).
- **Worker & D1 Claim Unit Tests**:
  ```bash
  node --test tests/worker.test.js
  ```
  Result: 10 tests passed (0 failures).

### Operational Limitations
- **Cloudflare CPU Measurement**: Local unit tests use fakes/emulators and do not measure actual Cloudflare CPU execution duration (<10ms target required later in live Cloudflare Workers environment).
- **Live Reliability Proof**: Local tests do not prove real-world network latency or live Cloudflare D1/GitHub API edge reliability.
