// src/worker.js - Cloudflare Worker + D1 Hourly Pulse Dispatcher

const GITHUB_API_VERSION = '2022-11-28';
const OWNER_REPO = 'ins-labs/hourly-pulse';
const MAX_CALLS_PER_SLOT = 9;

/**
 * Calculates the UTC hour slot string (YYYY-MM-DDTHH:00:00Z) from a Date, timestamp, or string.
 */
export function getUtcSlot(input) {
  let d;
  if (input === undefined || input === null) {
    d = new Date();
  } else if (typeof input === 'number') {
    d = new Date(input);
  } else if (input instanceof Date) {
    d = input;
  } else {
    d = new Date(input);
  }

  if (isNaN(d.getTime())) {
    d = new Date();
  }

  const year = d.getUTCFullYear();
  const month = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  const hours = String(d.getUTCHours()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:00:00Z`;
}

/**
 * Validates a GitHub workflow run ID and run URL according to repository contract.
 */
function isValidRunPayload(runId, runUrl, ownerRepo = OWNER_REPO) {
  const isIdValid = typeof runId === 'number' && Number.isInteger(runId) && runId > 0;
  if (!isIdValid) return false;

  const expectedPrefix = `https://github.com/${ownerRepo}/actions/runs/`;
  const isUrlValid = typeof runUrl === 'string' && runUrl.startsWith(expectedPrefix) && /^\d+$/.test(runUrl.slice(expectedPrefix.length));
  return isUrlValid;
}

/**
 * Main dispatch and reconciliation function for the hourly pulse.
 */
export async function dispatchPulse(env, options = {}) {
  const customFetch = options.fetch || globalThis.fetch;
  const sleep = options.sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const sleepMs = options.sleepMs !== undefined ? options.sleepMs : 2000;
  const ownerRepo = options.ownerRepo || OWNER_REPO;
  const token = env.GITHUB_TOKEN || '';

  const slotTimeInput = options.scheduledTime !== undefined ? options.scheduledTime : options.now;
  const slot_utc = getUtcSlot(slotTimeInput);

  const getHeaders = () => ({
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'Cloudflare-Worker-Pulse-Dispatcher',
    'X-GitHub-Api-Version': GITHUB_API_VERSION,
    'Authorization': `Bearer ${token}`
  });

  // Helper to safely execute a fetch and increment cumulative call_count in D1
  let cumulativeCallCount = 0;
  const dbRun = async (sql, ...params) => {
    return await env.DB.prepare(sql).bind(...params).run();
  };
  const dbFirst = async (sql, ...params) => {
    return await env.DB.prepare(sql).bind(...params).first();
  };

  const updateClaim = async (status, extra = {}) => {
    const fields = ['status = ?', 'call_count = ?', "updated_at = datetime('now')"];
    const params = [status, cumulativeCallCount];

    if (extra.dispatch_attempted_at !== undefined) {
      fields.push('dispatch_attempted_at = ?');
      params.push(extra.dispatch_attempted_at);
    }
    if (extra.workflow_run_id !== undefined) {
      fields.push('workflow_run_id = ?');
      params.push(extra.workflow_run_id);
    }
    if (extra.workflow_run_url !== undefined) {
      fields.push('workflow_run_url = ?');
      params.push(extra.workflow_run_url);
    }
    if (extra.conclusion !== undefined) {
      fields.push('conclusion = ?');
      params.push(extra.conclusion);
    }
    if (extra.error_reason !== undefined) {
      fields.push('error_reason = ?');
      params.push(extra.error_reason);
    }
    params.push(slot_utc);
    await dbRun(`UPDATE pulse_claims SET ${fields.join(', ')} WHERE slot_utc = ?`, ...params);
  };

  const trackedFetch = async (url, fetchOpts = {}) => {
    if (cumulativeCallCount >= MAX_CALLS_PER_SLOT) {
      throw new Error('CALL_BUDGET_EXHAUSTED');
    }
    cumulativeCallCount++;
    await dbRun('UPDATE pulse_claims SET call_count = ?, updated_at = datetime(\'now\') WHERE slot_utc = ?', cumulativeCallCount, slot_utc);
    return await customFetch(url, fetchOpts);
  };

  // 1. D1 Claim or Reconcile Lookup
  let existing = null;
  try {
    existing = await dbFirst('SELECT * FROM pulse_claims WHERE slot_utc = ?', slot_utc);
    if (!existing) {
      await dbRun('INSERT INTO pulse_claims (slot_utc, status, call_count) VALUES (?, ?, 0)', slot_utc, 'CLAIMED');
      cumulativeCallCount = 0;
    } else {
      cumulativeCallCount = existing.call_count || 0;
    }
  } catch (err) {
    if (err.message && err.message.includes('UNIQUE constraint failed')) {
      return { claimed: false, status: 'SLOT_ALREADY_CLAIMED', slot_utc };
    }
    throw err;
  }

  // Handle existing claims (Reconciliation / Duplicate invocation path)
  if (existing) {
    const status = existing.status;

    // Terminal or non-dispatchable states
    if (['DISPATCH_REJECTED', 'SUPPRESSED_PRE_POST', 'FAILED_PRE_CHECK', 'CONCLUDED'].includes(status)) {
      return { claimed: false, status, slot_utc, workflow_run_id: existing.workflow_run_id, conclusion: existing.conclusion };
    }

    // If initial claim crashed before POST_ATTEMPTED was persisted, no POST was made. Fail closed.
    if (status === 'CLAIMED') {
      return { claimed: false, status: 'CLAIMED', slot_utc, note: 'Crash before POST intent; no dispatch was attempted' };
    }

    if (cumulativeCallCount >= MAX_CALLS_PER_SLOT) {
      return { claimed: false, status: 'INCOMPLETE_BUDGET_EXHAUSTED', slot_utc, workflow_run_id: existing.workflow_run_id };
    }

    // If already bound to exact workflow_run_id, poll status directly
    if (existing.workflow_run_id) {
      return await pollExactRunId(existing.workflow_run_id, existing.workflow_run_url, false);
    }

    // Attempt read-only reconciliation for DISPATCHED, POST_ATTEMPTED, AMBIGUOUS, INCOMPLETE_BUDGET_EXHAUSTED
    return await reconcileAndPoll(false, existing.dispatch_attempted_at);
  }

  // Fresh Claim Execution Path (First Winner)

  // 2. Pre-POST Run-List Slot Check
  try {
    const runsUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/runs?event=workflow_dispatch&branch=main`;
    const runsRes = await trackedFetch(runsUrl, { headers: getHeaders() });

    if (!runsRes.ok) {
      await updateClaim('FAILED_PRE_CHECK', { error_reason: `Pre-POST check returned HTTP status ${runsRes.status}` });
      return { claimed: true, status: 'FAILED_PRE_CHECK', slot_utc };
    }

    const data = await runsRes.json();
    const runs = data.workflow_runs || [];
    const slotStart = new Date(slot_utc).getTime();
    const slotEnd = slotStart + 3600 * 1000;

    const preExistingRun = runs.find((r) => {
      const createdTime = new Date(r.created_at).getTime();
      return createdTime >= slotStart && createdTime < slotEnd;
    });

    if (preExistingRun) {
      const runUrl = preExistingRun.html_url || preExistingRun.url;
      await updateClaim('SUPPRESSED_PRE_POST', {
        workflow_run_id: preExistingRun.id,
        workflow_run_url: runUrl,
        conclusion: preExistingRun.conclusion || preExistingRun.status
      });
      return { claimed: true, status: 'SUPPRESSED_PRE_POST', slot_utc, workflow_run_id: preExistingRun.id };
    }
  } catch (err) {
    const reason = err.message === 'CALL_BUDGET_EXHAUSTED' ? 'Budget exhausted pre-check' : `Pre-POST check error: ${err.message}`;
    const status = err.message === 'CALL_BUDGET_EXHAUSTED' ? 'INCOMPLETE_BUDGET_EXHAUSTED' : 'FAILED_PRE_CHECK';
    await updateClaim(status, { error_reason: reason });
    return { claimed: true, status, slot_utc, error_reason: reason };
  }

  // 3. Dispatch POST
  const dispatchUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/dispatches`;
  const dispatchAttemptTime = new Date().toISOString();

  // Persist POST intent in D1 BEFORE fetch call
  await updateClaim('POST_ATTEMPTED', { dispatch_attempted_at: dispatchAttemptTime });

  let postRes;
  try {
    postRes = await trackedFetch(dispatchUrl, {
      method: 'POST',
      headers: {
        ...getHeaders(),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ ref: 'main' })
    });
  } catch (postErr) {
    const reason = postErr.message === 'CALL_BUDGET_EXHAUSTED' ? 'Budget exhausted before POST' : `POST network exception: ${postErr.message}`;
    const status = postErr.message === 'CALL_BUDGET_EXHAUSTED' ? 'INCOMPLETE_BUDGET_EXHAUSTED' : 'AMBIGUOUS';
    await updateClaim(status, { error_reason: reason });
    return { claimed: true, status, slot_utc, error_reason: reason };
  }

  if (postRes.status >= 400 && postRes.status < 500) {
    // Definitive 4xx rejection
    await updateClaim('DISPATCH_REJECTED', { error_reason: `HTTP ${postRes.status}` });
    return { claimed: true, status: 'DISPATCH_REJECTED', slot_utc, error_reason: `HTTP ${postRes.status}` };
  }

  if (!postRes.ok && postRes.status !== 204) {
    // 5xx or other non-2xx
    await updateClaim('AMBIGUOUS', { error_reason: `HTTP ${postRes.status}` });
    return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: `HTTP ${postRes.status}` };
  }

  // Handle 200 vs 204 response
  if (postRes.status === 200) {
    try {
      const resJson = await postRes.json();
      const runId = resJson.workflow_run_id;
      const runUrl = resJson.run_url;

      if (isValidRunPayload(runId, runUrl, ownerRepo)) {
        await updateClaim('DISPATCHED', { workflow_run_id: runId, workflow_run_url: runUrl });
        return await pollExactRunId(runId, runUrl, true);
      } else {
        await updateClaim('AMBIGUOUS', { error_reason: 'Malformed 200 response payload' });
        return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: 'Malformed 200 response payload' };
      }
    } catch (parseErr) {
      await updateClaim('AMBIGUOUS', { error_reason: `Malformed 200 response JSON: ${parseErr.message}` });
      return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: 'Malformed 200 response JSON' };
    }
  }

  // 204 No Content -> update status to DISPATCHED and proceed to reconciliation
  await updateClaim('DISPATCHED');
  return await reconcileAndPoll(true, dispatchAttemptTime);

  // Helper: Reconcile list-runs to bind exact workflow_run_id and poll
  async function reconcileAndPoll(isFirstWinner, attemptTime) {
    const slotStart = new Date(slot_utc).getTime();
    const slotEnd = slotStart + 3600 * 1000;

    let boundRunId = null;
    let boundRunUrl = null;

    while (cumulativeCallCount < MAX_CALLS_PER_SLOT && !boundRunId) {
      try {
        const listUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/runs?event=workflow_dispatch&branch=main`;
        const listRes = await trackedFetch(listUrl, { headers: getHeaders() });

        if (listRes.ok) {
          const listData = await listRes.json();
          const runs = (listData.workflow_runs || []).filter((r) => {
            const createdTime = new Date(r.created_at).getTime();
            return createdTime >= slotStart && createdTime < slotEnd;
          });

          if (runs.length === 1) {
            boundRunId = runs[0].id;
            boundRunUrl = runs[0].html_url || runs[0].url;
            await updateClaim('BOUND', { workflow_run_id: boundRunId, workflow_run_url: boundRunUrl });
            break;
          } else if (runs.length > 1) {
            await updateClaim('AMBIGUOUS', { error_reason: `Multiple candidate runs match slot (${runs.length} found)` });
            return { claimed: isFirstWinner, status: 'AMBIGUOUS', slot_utc, error_reason: 'Multiple candidate runs match slot' };
          }
        }
      } catch (err) {
        if (err.message === 'CALL_BUDGET_EXHAUSTED') break;
      }
      await sleep(sleepMs);
    }

    if (!boundRunId) {
      await updateClaim('INCOMPLETE_BUDGET_EXHAUSTED', { error_reason: 'Could not bind run before call budget exhausted' });
      return { claimed: isFirstWinner, status: 'INCOMPLETE_BUDGET_EXHAUSTED', slot_utc };
    }

    return await pollExactRunId(boundRunId, boundRunUrl, isFirstWinner);
  }

  // Helper: Poll exact bound workflow_run_id until finished or call budget exhausted
  async function pollExactRunId(runId, runUrl, isFirstWinner) {
    while (cumulativeCallCount < MAX_CALLS_PER_SLOT) {
      try {
        const pollUrl = `https://api.github.com/repos/${ownerRepo}/actions/runs/${runId}`;
        const pollRes = await trackedFetch(pollUrl, { headers: getHeaders() });

        if (pollRes.ok) {
          const runData = await pollRes.json();
          if (runData.status === 'completed') {
            const conclusion = runData.conclusion || 'completed';
            await updateClaim('CONCLUDED', {
              workflow_run_id: runId,
              workflow_run_url: runUrl || runData.html_url || runData.url,
              conclusion
            });
            return {
              claimed: isFirstWinner,
              status: 'CONCLUDED',
              slot_utc,
              workflow_run_id: runId,
              conclusion
            };
          }
        }
      } catch (err) {
        if (err.message === 'CALL_BUDGET_EXHAUSTED') break;
      }
      await sleep(sleepMs);
    }

    await updateClaim('INCOMPLETE_BUDGET_EXHAUSTED', {
      workflow_run_id: runId,
      workflow_run_url: runUrl
    });

    return {
      claimed: isFirstWinner,
      status: 'INCOMPLETE_BUDGET_EXHAUSTED',
      slot_utc,
      workflow_run_id: runId
    };
  }
}

export default {
  async scheduled(event, env, ctx) {
    const scheduledTime = event && event.scheduledTime ? event.scheduledTime : Date.now();
    const promise = dispatchPulse(env, { scheduledTime });
    if (ctx && typeof ctx.waitUntil === 'function') {
      ctx.waitUntil(promise);
    }
    return promise;
  },
  async fetch(request, env, ctx) {
    return new Response('Hourly Pulse Dispatcher Worker is active.', { status: 200 });
  }
};
