// src/worker.js - Cloudflare Worker + D1 Hourly Pulse Dispatcher

/**
 * Calculates the UTC hour slot string (YYYY-MM-DDTHH:00:00Z) for a given Date.
 */
export function getUtcSlot(date = new Date()) {
  const d = new Date(date);
  const year = d.getUTCFullYear();
  const month = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  const hours = String(d.getUTCHours()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:00:00Z`;
}

/**
 * Main dispatch function for the hourly pulse.
 */
export async function dispatchPulse(env, options = {}) {
  const customFetch = options.fetch || globalThis.fetch;
  const now = options.now || new Date();
  const slot_utc = getUtcSlot(now);
  const ownerRepo = options.ownerRepo || 'ins-labs/hourly-pulse';
  const token = env.GITHUB_TOKEN || '';

  let callCount = 0;
  const maxCalls = 9;

  // 1. Atomic D1 Claim
  let claimRecord;
  try {
    const checkSql = 'SELECT slot_utc, status, workflow_run_id, conclusion FROM pulse_claims WHERE slot_utc = ?';
    claimRecord = await env.DB.prepare(checkSql).bind(slot_utc).first();

    if (!claimRecord) {
      const insertSql = 'INSERT INTO pulse_claims (slot_utc, status) VALUES (?, ?)';
      await env.DB.prepare(insertSql).bind(slot_utc, 'CLAIMED').run();
      claimRecord = { slot_utc, status: 'CLAIMED' };
    } else {
      // Slot already claimed by a previous worker or execution
      return { claimed: false, status: claimRecord.status, slot_utc };
    }
  } catch (err) {
    if (err.message && err.message.includes('UNIQUE constraint failed')) {
      return { claimed: false, status: 'SLOT_ALREADY_CLAIMED', slot_utc };
    }
    // Fail closed on D1 errors
    throw err;
  }

  const updateStatus = async (status, extra = {}) => {
    const fields = ['status = ?', "updated_at = datetime('now')"];
    const params = [status];
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
    const sql = `UPDATE pulse_claims SET ${fields.join(', ')} WHERE slot_utc = ?`;
    await env.DB.prepare(sql).bind(...params).run();
  };

  const headers = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'Cloudflare-Worker-Pulse-Dispatcher',
    'Authorization': `Bearer ${token}`
  };

  // 2. Pre-POST Run-List Slot Check (Suppress manual/pre-existing runs)
  if (callCount >= maxCalls) {
    await updateStatus('INCOMPLETE_BUDGET_EXHAUSTED', { error_reason: 'Budget exhausted pre-check' });
    return { claimed: true, status: 'INCOMPLETE_BUDGET_EXHAUSTED', slot_utc };
  }

  try {
    callCount++;
    const runsUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/runs?event=workflow_dispatch&branch=main`;
    const runsRes = await customFetch(runsUrl, { headers });
    if (runsRes.ok) {
      const data = await runsRes.json();
      const runs = data.workflow_runs || [];
      const slotStart = new Date(slot_utc).getTime();
      const slotEnd = slotStart + 3600 * 1000;

      const preExistingRun = runs.find(r => {
        const createdTime = new Date(r.created_at).getTime();
        return createdTime >= slotStart && createdTime < slotEnd;
      });

      if (preExistingRun) {
        await updateStatus('SUPPRESSED_PRE_POST', {
          workflow_run_id: preExistingRun.id,
          workflow_run_url: preExistingRun.html_url || preExistingRun.url,
          conclusion: preExistingRun.conclusion || preExistingRun.status
        });
        return {
          claimed: true,
          status: 'SUPPRESSED_PRE_POST',
          slot_utc,
          workflow_run_id: preExistingRun.id
        };
      }
    }
  } catch (err) {
    // If pre-POST check fails with network error, proceed cautiously or record error
  }

  // 3. Dispatch POST
  if (callCount >= maxCalls) {
    await updateStatus('INCOMPLETE_BUDGET_EXHAUSTED', { error_reason: 'Budget exhausted before POST' });
    return { claimed: true, status: 'INCOMPLETE_BUDGET_EXHAUSTED', slot_utc };
  }

  const dispatchUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/dispatches`;
  const dispatchAttemptTime = new Date().toISOString();

  let postRes;
  try {
    callCount++;
    postRes = await customFetch(dispatchUrl, {
      method: 'POST',
      headers: {
        ...headers,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ ref: 'main' })
    });
  } catch (postErr) {
    // Ambiguous POST: request was sent, but response/network threw.
    // NEVER retry POST. Mark ambiguous.
    await updateStatus('AMBIGUOUS', {
      dispatch_attempted_at: dispatchAttemptTime,
      error_reason: `POST request exception: ${postErr.message}`
    });
    return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: postErr.message };
  }

  if (!postRes.ok && postRes.status !== 204) {
    // Direct error response from GitHub (e.g., 401, 403, 404, 500)
    await updateStatus('AMBIGUOUS', {
      dispatch_attempted_at: dispatchAttemptTime,
      error_reason: `POST rejected with HTTP status ${postRes.status}`
    });
    return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: `HTTP ${postRes.status}` };
  }

  // Record DISPATCHED status
  let directRunId = null;
  let directRunUrl = null;
  if (postRes.status === 200) {
    try {
      const resJson = await postRes.json();
      if (resJson && resJson.id) {
        directRunId = resJson.id;
        directRunUrl = resJson.html_url || resJson.url;
      }
    } catch (_) {}
  }

  await updateStatus('DISPATCHED', {
    dispatch_attempted_at: dispatchAttemptTime,
    workflow_run_id: directRunId,
    workflow_run_url: directRunUrl
  });

  // 4. List-Runs Reconciliation (if run ID not directly provided)
  let boundRunId = directRunId;
  let boundRunUrl = directRunUrl;

  if (!boundRunId) {
    const slotStart = new Date(slot_utc).getTime();
    const slotEnd = slotStart + 3600 * 1000;

    while (callCount < maxCalls && !boundRunId) {
      callCount++;
      const listUrl = `https://api.github.com/repos/${ownerRepo}/actions/workflows/pulse.yml/runs?event=workflow_dispatch&branch=main`;
      const listRes = await customFetch(listUrl, { headers });
      if (listRes.ok) {
        const listData = await listRes.json();
        const runs = (listData.workflow_runs || []).filter(r => {
          const createdTime = new Date(r.created_at).getTime();
          return createdTime >= slotStart && createdTime < slotEnd;
        });

        if (runs.length === 1) {
          boundRunId = runs[0].id;
          boundRunUrl = runs[0].html_url || runs[0].url;
          await updateStatus('BOUND', {
            workflow_run_id: boundRunId,
            workflow_run_url: boundRunUrl
          });
        } else if (runs.length > 1) {
          await updateStatus('AMBIGUOUS', {
            error_reason: 'Multiple candidate runs match slot'
          });
          return { claimed: true, status: 'AMBIGUOUS', slot_utc, error_reason: 'Multiple candidate runs match slot' };
        }
      }
    }
  }

  if (!boundRunId) {
    await updateStatus('INCOMPLETE_BUDGET_EXHAUSTED', {
      error_reason: 'Could not bind run before call budget exhausted'
    });
    return { claimed: true, status: 'INCOMPLETE_BUDGET_EXHAUSTED', slot_utc };
  }

  // 5. Poll Exact Bound Run Status
  while (callCount < maxCalls) {
    callCount++;
    const pollUrl = `https://api.github.com/repos/${ownerRepo}/actions/runs/${boundRunId}`;
    const pollRes = await customFetch(pollUrl, { headers });
    if (pollRes.ok) {
      const runData = await pollRes.json();
      if (runData.status === 'completed') {
        await updateStatus('CONCLUDED', {
          conclusion: runData.conclusion || 'completed',
          workflow_run_id: boundRunId,
          workflow_run_url: runData.html_url || runData.url
        });
        return {
          claimed: true,
          status: 'CONCLUDED',
          slot_utc,
          workflow_run_id: boundRunId,
          conclusion: runData.conclusion || 'completed'
        };
      }
    }
  }

  await updateStatus('INCOMPLETE_BUDGET_EXHAUSTED', {
    workflow_run_id: boundRunId,
    workflow_run_url: boundRunUrl
  });

  return {
    claimed: true,
    status: 'INCOMPLETE_BUDGET_EXHAUSTED',
    slot_utc,
    workflow_run_id: boundRunId
  };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchPulse(env));
  },
  async fetch(request, env, ctx) {
    return new Response('Hourly Pulse Dispatcher Worker is active.', { status: 200 });
  }
};
