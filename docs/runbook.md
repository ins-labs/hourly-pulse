# Cloudflare Worker + D1 Hourly Pulse Dispatcher Runbook

## Overview & Architecture

This repository contains the source, database schema, and deployment configuration for an external Cloudflare Worker dispatcher that triggers the GitHub Actions hourly pulse workflow (`.github/workflows/pulse.yml`) in `ins-labs/hourly-pulse`.

### Design & Guiding Principles
- **At-most-one dispatcher winning per slot**: An atomic `UNIQUE` key constraint on `pulse_claims.slot_utc` in Cloudflare D1 guarantees that across duplicate or concurrent Worker invocations, at most one Worker claims a UTC hour slot.
- **`scheduled` Event Slot Calculation**: Slot is derived strictly from `event.scheduledTime` (ms epoch). Delayed executions retain their original scheduled UTC hour slot without dispatching catchup runs for past slots.
- **Fail-closed D1 and Pre-Check errors**: If D1 is unavailable or if the pre-POST run check fails (network error, HTTP non-200, malformed JSON), execution fails closed immediately as `FAILED_PRE_CHECK` without POSTing.
- **Persisted POST Intent**: The worker records `POST_ATTEMPTED` with `dispatch_attempted_at` in D1 **before** issuing the `POST /dispatches` fetch. If a crash occurs before this write, reconciliation sees `CLAIMED` status and never attempts a POST.
- **Ambiguous POST Receipts**: If a POST request experiences a network exception or HTTP 5xx error, the slot is marked `AMBIGUOUS`. The worker **NEVER** retries a POST in the same slot.
- **Exact Run Binding & Paced Observation**: A valid 200 dispatches response containing valid `workflow_run_id` and `run_url` is bound directly. A 204 response triggers list-runs reconciliation filtering by dispatch attempt window, `workflow_dispatch`, and `main` branch. Bound runs are polled with paced delays up to a strict cumulative call budget (<= 9 calls per slot).
- **Read-Only Reconciliation**: Subsequent invocations on an already claimed slot never issue a `POST`. They perform read-only status observation or reconciliation.

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
    call_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Persisted Claim State Machine
- `CLAIMED`: Slot claimed in D1. If crashed prior to POST intent persistence, reconciliation sees `CLAIMED` and does NOT POST.
- `FAILED_PRE_CHECK`: Pre-POST run list check failed (network throw, non-200 HTTP response, or malformed JSON). Execution stopped safely before POST.
- `SUPPRESSED_PRE_POST`: Existing run already present in the current UTC slot prior to dispatch.
- `POST_ATTEMPTED`: Persisted in D1 immediately before issuing `POST /dispatches` request.
- `DISPATCH_REJECTED`: GitHub API returned a definitive rejected HTTP 4xx status (e.g., 401, 403, 404, 422).
- `DISPATCHED`: Dispatch request succeeded (204 or validated 200 response).
- `BOUND`: Reconciled and bound to exact `workflow_run_id`.
- `CONCLUDED`: Bound workflow run completed with a terminal conclusion.
- `AMBIGUOUS`: Network exception during/after POST, HTTP 5xx, malformed 200 payload, or multiple candidate runs found during list reconciliation.
- `INCOMPLETE_BUDGET_EXHAUSTED`: Cumulative `call_count` reached maximum budget (9 calls per slot) across invocations before run bound or concluded.

---

## Deployment & Secret Binding Configuration

The checked-in `wrangler.toml` file is **cron-inactive by default** (`triggers = { crons = [] }`).

### Secret Binding
Do NOT place plain token credentials in `[vars]` or check secrets into repository files. Bind secret credentials via Wrangler secret management:
```bash
npx wrangler secret put GITHUB_TOKEN
```

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
```

---

## Operational Cutover & Rollback Plan

### Cutover Plan
1. **Deploy Inactive Worker**: Provision D1 database (`schema.sql`), configure `GITHUB_TOKEN` secret via `wrangler secret put GITHUB_TOKEN`, and deploy Worker with cron-inactive configuration (`triggers = { crons = [] }`).
2. **Review & Apply Schedule Removal Patch**: Review and apply `docs/schedule-removal.patch` in a separate PR to remove `schedule:` cron from `.github/workflows/pulse.yml`.
3. **Verify GitHub Actions Schedule Disabled**: Confirm `.github/workflows/pulse.yml` no longer triggers automatically on GitHub schedule.
4. **Enable `:00` Worker Cron**: Update Worker trigger configuration to `0 * * * *` and deploy.
5. **Verify First Hourly Execution**: Confirm D1 claim record transitions (`CLAIMED` -> `POST_ATTEMPTED` -> `DISPATCHED` -> `BOUND` -> `CONCLUDED`).

### Rollback Plan
1. **Disable Worker First**: Re-deploy Worker with `triggers = { crons = [] }` or remove cron trigger in Cloudflare dashboard.
2. **Restore Actions Schedule**: Revert schedule-removal patch on `.github/workflows/pulse.yml` to restore GitHub native `schedule` cron.
3. **Verify Schedule Operational**: Confirm GitHub Actions resume scheduled pulse runs.

*Rule: Never have both Cloudflare Worker cron and GitHub Actions schedule active at the same time.*

---

## Honest Operational Limitations & Correlation Disclosures

1. **No External Manual Dispatch Guarantee**: The Worker guarantees at-most-one *Worker* dispatch per UTC hour slot. It cannot prevent external human users from manually triggering `workflow_dispatch` via the GitHub UI at the same time.
2. **Exact Run IDs vs Timestamp Correlation**: Exact `workflow_run_id` binding is guaranteed when GitHub API returns a trustworthy, validated 200 payload. On 204 responses, list-runs reconciliation uses timestamp correlation within the UTC slot window (`dispatch_attempted_at` + slot window). If multiple candidate runs fit the window, the state is recorded as `AMBIGUOUS` (no guessing).
3. **Unmeasured Edge Performance**: Local test suite uses emulated environments and SQLite adapters (`node:sqlite`). Cloudflare Workers CPU execution duration (<10ms target) and live edge network reliability remain unmeasured in local testing.
