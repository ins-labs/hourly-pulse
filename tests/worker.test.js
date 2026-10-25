import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { getUtcSlot, dispatchPulse } from '../src/worker.js';

describe('Worker Dispatcher Tests', () => {
  test('UTC hour slot calculation at boundaries', () => {
    assert.equal(getUtcSlot(new Date('2025-05-10T14:37:05.123Z')), '2025-05-10T14:00:00Z');
    assert.equal(getUtcSlot(new Date('2025-05-10T00:00:00.000Z')), '2025-05-10T00:00:00Z');
    assert.equal(getUtcSlot(new Date('2025-12-31T23:59:59.999Z')), '2025-12-31T23:00:00Z');
  });

  test('Concurrent claims enforce atomic UNIQUE constraint (at-most-one winner)', async () => {
    // Mock environment with D1 and GitHub API
    const mockDb = createMockD1Database();
    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) {
        return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      }
      if (url.includes('/dispatches')) {
        return { ok: true, status: 204, text: async () => '' };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const date = new Date('2025-05-10T14:00:00Z');

    // Run two dispatches concurrently for the same slot
    const [res1, res2] = await Promise.all([
      dispatchPulse(env, { fetch: mockFetch, now: date }),
      dispatchPulse(env, { fetch: mockFetch, now: date }),
    ]);

    const winnerCount = [res1, res2].filter(r => r.claimed === true).length;
    const loserCount = [res1, res2].filter(r => r.claimed === false).length;

    assert.equal(winnerCount, 1, 'Exactly one worker claim wins');
    assert.equal(loserCount, 1, 'Losing claim fails closed gracefully');

    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 1, 'Only winning claim performs POST');
  });

  test('Transient D1 error fails closed (no POST attempted)', async () => {
    const mockDb = {
      prepare: () => ({
        bind: () => ({
          run: async () => { throw new Error('D1 database unavailable'); },
          first: async () => { throw new Error('D1 database unavailable'); }
        })
      })
    };
    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      return { ok: true, status: 204 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };

    await assert.rejects(async () => {
      await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });
    }, /D1 database unavailable/);

    assert.equal(fetchCalls.length, 0, 'No fetch attempted on D1 failure');
  });

  test('Pre-POST run-list slot check suppresses dispatch if run already exists', async () => {
    const mockDb = createMockD1Database();
    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              {
                id: 999111,
                html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/999111',
                event: 'workflow_dispatch',
                created_at: '2025-05-10T14:05:00Z',
                status: 'in_progress',
                head_branch: 'main'
              }
            ]
          })
        };
      }
      return { ok: true, status: 204 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:10:00Z') });

    assert.equal(res.status, 'SUPPRESSED_PRE_POST');
    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 0, 'POST was suppressed due to pre-existing run in slot');
  });

  test('Happy path 204 POST response fallback to list-runs reconciliation and exact run ID polling', async () => {
    const mockDb = createMockD1Database();
    let runsCallCount = 0;
    const fetchCalls = [];

    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/dispatches')) {
        return { ok: true, status: 204, text: async () => '' };
      }
      if (url.includes('/runs/888222')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            id: 888222,
            html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/888222',
            status: 'completed',
            conclusion: 'success'
          })
        };
      }
      if (url.includes('/runs')) {
        runsCallCount++;
        if (runsCallCount === 1) {
          // Pre-POST check -> empty
          return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
        }
        // Reconciliation -> finds created run
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              {
                id: 888222,
                html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/888222',
                event: 'workflow_dispatch',
                created_at: '2025-05-10T14:00:02Z',
                status: 'completed',
                conclusion: 'success',
                head_branch: 'main'
              }
            ]
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });

    assert.equal(res.status, 'CONCLUDED');
    assert.equal(res.workflow_run_id, 888222);
    assert.equal(res.conclusion, 'success');
  });

  test('Direct 200 validated exact run ID response', async () => {
    const mockDb = createMockD1Database();
    const mockFetch = async (url, opts) => {
      if (url.includes('/runs/777333')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            id: 777333,
            html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/777333',
            status: 'completed',
            conclusion: 'success'
          })
        };
      }
      if (url.includes('/runs')) {
        return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      }
      if (url.includes('/dispatches')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ id: 777333, html_url: 'https://github.com/ins-labs/hourly-pulse/actions/runs/777333' })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });

    assert.equal(res.status, 'CONCLUDED');
    assert.equal(res.workflow_run_id, 777333);
  });

  test('Ambiguous POST failure records AMBIGUOUS and NEVER retries POST', async () => {
    const mockDb = createMockD1Database();
    const fetchCalls = [];

    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) {
        return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
      }
      if (url.includes('/dispatches')) {
        throw new Error('Network timeout / dropped connection after sending POST');
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });

    assert.equal(res.status, 'AMBIGUOUS');
    assert.equal(res.error_reason.includes('Network timeout'), true);

    // Run dispatch again on same slot -> should NOT POST again
    const res2 = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:05:00Z') });
    assert.equal(res2.claimed, false);
    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 1, 'Never retries POST in same slot after ambiguous POST');
  });

  test('Multiple candidate runs in reconciliation marks AMBIGUOUS without guessing', async () => {
    const mockDb = createMockD1Database();
    let runsCount = 0;
    const mockFetch = async (url, opts) => {
      if (url.includes('/dispatches')) {
        return { ok: true, status: 204, text: async () => '' };
      }
      if (url.includes('/runs')) {
        runsCount++;
        if (runsCount === 1) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              { id: 101, created_at: '2025-05-10T14:00:01Z', event: 'workflow_dispatch', head_branch: 'main' },
              { id: 102, created_at: '2025-05-10T14:00:02Z', event: 'workflow_dispatch', head_branch: 'main' }
            ]
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });

    assert.equal(res.status, 'AMBIGUOUS');
    assert.equal(res.error_reason, 'Multiple candidate runs match slot');
  });

  test('Enforces network call budget (<= 9 calls per slot)', async () => {
    const mockDb = createMockD1Database();
    let callCount = 0;

    const mockFetch = async (url, opts) => {
      callCount++;
      if (url.includes('/dispatches')) {
        return { ok: true, status: 204, text: async () => '' };
      }
      if (url.includes('/runs/999')) {
        // Still in progress
        return {
          ok: true,
          status: 200,
          json: async () => ({ id: 999, status: 'in_progress', conclusion: null })
        };
      }
      if (url.includes('/runs')) {
        if (callCount === 1) return { ok: true, status: 200, json: async () => ({ workflow_runs: [] }) };
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              { id: 999, created_at: '2025-05-10T14:00:01Z', event: 'workflow_dispatch', head_branch: 'main', status: 'in_progress' }
            ]
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:00:00Z') });

    assert.ok(callCount <= 9, `Network calls count ${callCount} must be <= 9`);
    assert.equal(res.status, 'INCOMPLETE_BUDGET_EXHAUSTED');
  });

  test('Restart/reconcile does not attempt duplicate POST', async () => {
    const mockDb = createMockD1Database();
    // Pre-seed claim in DISPATCHED state
    await mockDb.prepare('INSERT INTO pulse_claims (slot_utc, status) VALUES (?, ?)')
      .bind('2025-05-10T14:00:00Z', 'DISPATCHED').run();

    const fetchCalls = [];
    const mockFetch = async (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.includes('/runs')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            workflow_runs: [
              { id: 5001, created_at: '2025-05-10T14:00:01Z', event: 'workflow_dispatch', head_branch: 'main', status: 'completed', conclusion: 'success' }
            ]
          })
        };
      }
      return { ok: false, status: 404 };
    };

    const env = { DB: mockDb, GITHUB_TOKEN: 'test-token' };
    const res = await dispatchPulse(env, { fetch: mockFetch, now: new Date('2025-05-10T14:05:00Z') });

    assert.equal(res.claimed, false);
    const postCalls = fetchCalls.filter(c => c.url.includes('/dispatches'));
    assert.equal(postCalls.length, 0, 'Reconciliation on already-claimed slot does not issue POST');
  });
});

// Helper: Mock D1 Database implementation
function createMockD1Database() {
  const store = new Map();
  return {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async run() {
              if (sql.startsWith('INSERT INTO pulse_claims')) {
                const [slot_utc, status] = args;
                if (store.has(slot_utc)) {
                  const err = new Error('UNIQUE constraint failed: pulse_claims.slot_utc');
                  err.code = 'SQLITE_CONSTRAINT';
                  throw err;
                }
                const record = { slot_utc, status, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
                store.set(slot_utc, record);
                return { success: true };
              }
              if (sql.startsWith('UPDATE pulse_claims')) {
                // simple mock update
                const slot_utc = args[args.length - 1];
                if (store.has(slot_utc)) {
                  const record = store.get(slot_utc);
                  // parse set values if needed or just simulate update
                  store.set(slot_utc, { ...record, updated_at: new Date().toISOString() });
                  return { success: true };
                }
              }
              return { success: true };
            },
            async first() {
              if (sql.includes('FROM pulse_claims WHERE slot_utc')) {
                const slot_utc = args[0];
                return store.get(slot_utc) || null;
              }
              return null;
            }
          };
        }
      };
    }
  };
}
