"""Work-rows source: two interchangeable backends, fail-closed on any gap.

Backends:
  snapshot - encrypted JSON file in this repo (ciphertext only), decrypted with the text key.
  issues   - live read of a configured private GitHub repository whose issues are the
             approved user work items (title = work label, state labels carry status).

No backend may guess. Missing binding, unknown source, unlabeled issue, or unknown
status label aborts the run before any send.
"""

import json
import os
import urllib.parse
import urllib.request

import crypto_util

STATE_LABELS = {"state:pending": "pending", "state:in_progress": "in_progress", "state:parked": "parked"}
MAX_PAGES = 10


def _gh_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def from_snapshot(text_key, enc_path):
    """Rows from the encrypted snapshot file; snapshot_at comes from the file itself."""
    data = json.loads(crypto_util.decrypt_text(text_key, enc_path.read_text()))
    return {"snapshot_at": data["snapshot_at"], "rows": data["rows"]}


def from_issues(repo, token, fetcher=_gh_json):
    """Rows from a configured work-ledger repository. snapshot_at is set by the caller
    (read time). Every issue must be open and carry exactly one state:* label."""
    if not repo or "/" not in repo:
        raise SystemExit("missing work repository binding")
    rows = []
    for page in range(1, MAX_PAGES + 1):
        url = ("https://api.github.com/repos/" + urllib.parse.quote(repo, safe="")
               + "/issues?state=open&per_page=100&page=" + str(page))
        batch = fetcher(url, token)
        if not isinstance(batch, list):
            raise SystemExit("unexpected work repository response")
        for issue in batch:
            if "pull_request" in issue:
                continue
            labels = [l.get("name", "") for l in issue.get("labels", [])]
            states = [STATE_LABELS[l] for l in labels if l in STATE_LABELS]
            if len(states) != 1:
                raise SystemExit("work issue #" + str(issue.get("number")) + " must carry exactly one state:* label")
            title = issue.get("title", "").strip()
            if not title:
                raise SystemExit("work issue #" + str(issue.get("number")) + " has an empty title")
            rows.append({"label": title, "status": states[0]})
        if len(batch) < 100:
            break
    if not rows:
        raise SystemExit("work repository returned no open work issues")
    return {"snapshot_at": None, "rows": rows}


def load(env=os.environ):
    source = env.get("PULSE_WORK_SOURCE", "").strip()
    if source == "snapshot":
        key = env.get("PULSE_TEXT_KEY", "").strip()
        if not key:
            raise SystemExit("missing text key binding")
        from pathlib import Path
        enc = Path(__file__).resolve().parent.parent / "content" / "work_snapshot.enc"
        if not enc.is_file():
            raise SystemExit("missing encrypted work snapshot file")
        return from_snapshot(key, enc)
    if source == "issues":
        token = env.get("PULSE_GH_TOKEN", "").strip()
        if not token:
            raise SystemExit("missing GitHub token binding")
        data = from_issues(env.get("PULSE_WORK_REPO", "").strip(), token)
        import datetime as dt
        data["snapshot_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return data
    raise SystemExit("unknown or missing PULSE_WORK_SOURCE")
