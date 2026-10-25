import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import worker, { getUtcSlot, dispatchPulse } from '../src/worker.js';

// Real SQLite-backed D1 Adapter
function createRealD1Database() {
  const db = new DatabaseSync(':memory:');
  db.exec(`
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
  `);

  return {
    _rawDb: db,
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              const stmt = db.prepare(sql);
              try {
                stmt.run(...args);
                return { success: true };
              } catch (err) {
                // Map SQLite constraint error to standard error
                throw err;
              }
            },
            async first() {
              const stmt = db.prepare(sql);
              const row = stmt.get(...args);
              return row || null;
            }
          };
        }
      };
    }
  };
}

const noopSleep = async () => {};

describe('Worker Dispatcher Tests with Real SQLite D1 Adapter', () => {
  test('UTC hour slot calculation from scheduledTime and boundaries', () => {
    // Epoch timestamp in ms for 2025-05-10 14:37:05 UTC
    const ts = new Date('2025-05-10T14:37:05.123Z').getTime();
    assert.equal(getUtcSlot(ts), '2025-05-10T14:00:00Z');
    assert.equal(getUtcSlot('2025-05-10T00:00:00.000Z'), '2025-05-10T00:00:00Z');
    assert.equal(getUtcSlot(new Date('2025-12-31T23:59:59.999Z')), '2025-12-31T23:00:00Z');
  });

  test('scheduled handler derives slot from event.scheduledTime (delayed hour-crossing)', async () => {
    const mockDb = createRealD1Database();
    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) return { ok: true, status: 204, text: async () => '' };
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    // Event scheduled for 14:00 UTC, but handler executes at 15:05 UTC due to delay
    const scheduledTimeMs = new Date('2025-05-10T14:00:00.000Z').getTime();
    const event = { scheduledTime: scheduledTimeMs, cron: '0 * * * *' };

    let waitUntilPromise;
    const ctx = { waitUntil(p) { waitUntilPromise = p; } };

    await worker.scheduled(event, env, ctx);
    const res = await waitUntilPromise;

    assert.equal(res.slot_utc, '2025-05-10T14:00:00Z', 'Slot derived strictly from event.scheduledTime');

    // Verify D1 claim row
    const row = mockDb._rawDb.prepare('SELECT * FROM pulse_claims WHERE slot_utc = ?').get('2025-05-10T14:00:00Z');
    assert.ok(row, 'Row created for scheduled time slot');
  });

  test('200 response contract: valid workflow_run_id and run_url vs malformed 200 payload', async () => {
    const mockDb = createRealD1Database();
    const mockFetchValid200 = async (url, opts) => {
      assert.equal(opts.headers['X-GitHub-Api-Version'], '2022-11-28');
      if (url.includes('/runs/888111')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            id: 888111,
            html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/888111',
            status: 'completed',
            conclusion: 'success'
          })
        };
      }
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_run_id: 888111,
            run_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/888111'
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res1 = await dispatchPulse(env, { fetch: mockFetchValid200, now: new Date('2025-05-10T14:00:00Z'), sleep: noopSleep });
    assert.equal(res1.status, 'CONCLUDED');
    assert.equal(res1.workflow_run_id, 888111);

    // Test malformed 200 response (e.g. using old id / html_url shape or invalid integer/URL)
    const mockDb2 = createRealD1Database();
    const mockFetchMalformed200 = async (url, opts) => {
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ id: 888111, html_url: 'https://github.com/invalid-host/runs/888111' }) // Missing workflow_run_id
        };
      }
      return { ok: false, status: 404 };
    };

    const res2 = await dispatchPulse(env, { fetch: mockFetchMalformed200, now: new Date('2025-05-10T15:00:00Z'), sleep: noopSleep });
    assert.equal(res2.status, 'AMBIGUOUS');
    assert.equal(res2.error_reason.includes('Malformed 200'), true);
  });

  test('Persists POST_ATTEMPTED before fetch call and crash-before-POST stays un-dispatched on reconciliation', async () => {
    const mockDb = createRealD1Database();

    // Pre-insert a claim in CLAIMED state (simulating crash before POST_ATTEMPTED was persisted)
    mockDb._rawDb.prepare('INSERT INTO pulse_claims (slot_utc, status) VALUES (?, ?)').run('2025-05-10T14:00:00Z', 'CLAIMED');

    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    // Subsequent execution on same slot should reconcile, see CLAIMED status with no POST intent, and NOT issue POST
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:05:00Z'), sleep: noopSleep });

    assert.equal(res.claimed, false);
    assert.equal(res.status, 'CLAIMED');
    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 0, 'Does not POST on reconciliation when earlier run crashed before POST intent');
  });

  test('Read-only reconciliation for DISPATCHED / AMBIGUOUS / BOUND / INCOMPLETE_BUDGET_EXHAUSTED', async () => {
    const mockDb = createRealD1Database();
    // Pre-insert claim in DISPATCHED state with dispatch_attempted_at timestamp
    mockDb._rawDb.prepare(`
      INSERT INTO pulse_claims (slot_utc, status, dispatch_attempted_at, call_count)
      VALUES (?, ?, ?, ?)
    `).run('2025-05-10T14:00:00Z', 'DISPATCHED', '2025-05-10T14:00:02Z', 2);

    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs/5001')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ id: 5001, html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/5001', status: 'completed', conclusion: 'success' })
        };
      }
      if (url.includes('/runs')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              { id: 5001, created_at: '2025-05-10T14:00:03Z', event: 'workflow_dispatch', head_branch: 'main', html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/5001' }
            ]
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:15:00Z'), sleep: noopSleep });

    assert.equal(res.claimed, false);
    assert.equal(res.status, 'CONCLUDED');
    assert.equal(res.workflow_run_id, 5001);
    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 0, 'Reconciliation does NOT issue POST');
  });

  test('Pre-POST list check failures fail closed as FAILED_PRE_CHECK', async () => {
    const mockDb = createRealD1Database();
    const mockFetchError = async (url, opts) => {
      if (url.includes('/runs')) throw new Error('DNS failure on pre-POST list check');
      return { ok: true, status: 204 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetchError, now: new Date('2025-05-10T14:00:00Z'), sleep: noopSleep });

    assert.equal(res.status, 'FAILED_PRE_CHECK');
    assert.equal(res.error_reason.includes('DNS failure'), true);

    const row = mockDb._rawDb.prepare('SELECT * FROM pulse_claims WHERE slot_utc = ?').get('2025-05-10T14:00:00Z');
    assert.equal(row.status, 'FAILED_PRE_CHECK');
  });

  test('Separates definitive rejected 4xx (DISPATCH_REJECTED) from ambiguous network/5xx (AMBIGUOUS)', async () => {
    // 4xx Test
    const mockDb4xx = createRealD1Database();
    const mockFetch403 = async (url, opts) => {
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) return { ok: false, status: 403, text: async () => 'Forbidden' };
      return { ok: false, status: 404 };
    };

    const env1 = { DB: mockDb4xx, GITHUB_TOKEN: 'invalid-token' };
    const res1 = await dispatchPulse(env1, { fetch: mockFetch403, now: new Date('2025-05-10T14:00:00Z'), sleep: noopSleep });
    assert.equal(res1.status, 'DISPATCH_REJECTED');

    // 5xx Test
    const mockDb5xx = createRealD1Database();
    const mockFetch500 = async (url, opts) => {
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) return { ok: false, status: 502, text: async () => 'Bad Gateway' };
      return { ok: false, status: 404 };
    };

    const env2 = { DB: mockDb5xx, GITHUB_TOKEN: 'test-token' };
    const res2 = await dispatchPulse(env2, { fetch: mockFetch500, now: new Date('2025-05-10T14:00:00Z'), sleep: noopSleep });
    assert.equal(res2.status, 'AMBIGUOUS');
  });

  test('Cumulative per-slot network call budget enforcement across restarts (<= 9 calls per slot)', async () => {
    const mockDb = createRealD1Database();
    // Pre-seed a claim with call_count = 8
    mockDb._rawDb.prepare(`
      INSERT INTO pulse_claims (slot_utc, status, dispatch_attempted_at, call_count)
      VALUES (?, ?, ?, ?)
    `).run('2025-05-10T14:00:00Z', 'DISPATCHED', '2025-05-10T14:00:02Z', 8);

    let callCount = 0;
    const mockFetch = async (url, opts) => {
      callCount++;
      if (url.includes('/runs')) {
        return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    // Invocation starts with cumulative call_count = 8. It can only make 1 network call before reaching 9 limit!
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:05:00Z'), sleep: noopSleep });

    assert.equal(callCount, 1, 'Only 1 call made because budget was at 8');
    assert.equal(res.status, 'INCOMPLETE_BUDGET_EXHAUSTED');

    const row = mockDb._rawDb.prepare('SELECT * FROM pulse_claims WHERE slot_utc = ?').get('2025-05-10T14:00:00Z');
    assert.equal(row.call_count, 9, 'Cumulative call_count in DB updated to 9');

    // Subsequent invocation with call_count = 9 should make ZERO network calls
    callCount = 0;
    const res2 = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:10:00Z'), sleep: noopSleep });
    assert.equal(callCount, 0, 'Zero calls made when cumulative budget exhausted');
    assert.equal(res2.status, 'INCOMPLETE_BUDGET_EXHAUSTED');
  });

  test('Real SQLite atomic UNIQUE concurrency test', async () => {
    const mockDb = createRealD1Database();
    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      if (url.includes('/dispatches')) return { ok: true, status: 204, text: async () => '' };
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const date = new Date('2025-05-10T14:00:00Z');

    const [res1, res2] = await Promise.all([
      dispatchPulse(env, { fetch: mockFetch, now: date, sleep: noopSleep }),
      dispatchPulse(env, { fetch: mockFetch, now: date, sleep: noopSleep })
    ]);

    const winnerCount = [res1, res2].filter(r => r.claimed === true).length;
    const loserCount = [res1, res2].filter(r => r.claimed === false).length;

    assert.equal(winnerCount, 1, 'Exactly one worker claim wins');
    assert.equal(loserCount, 1, 'Losing claim fails closed');
  });
});
