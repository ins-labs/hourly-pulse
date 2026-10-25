-- D1 Schema for Hourly Pulse Dispatcher Claims
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
