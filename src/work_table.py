"""PROPOSAL pending approved content contract: GitHub-only work table: per-repository open-issue counts plus issues
closed in the last 24 hours. SEMANTIC GAP: if the approved work-table source
of truth is not GitHub issues (e.g., an external task store), this is a
deterministic substitution and the gap must be reported, not hidden.
Deterministic and LLM-free; every number is a
fresh API read, validated like the collector counts; any failure raises.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request

SEARCH = "https://api.github.com/search/issues"
REPOS = "https://api.github.com/orgs/{org}/repos?per_page=100&type=all"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "hourly-pulse-collector",
}


def _get(url, token, opener):
    request = urllib.request.Request(
        url, headers=dict(HEADERS, Authorization="Bearer " + token)
    )
    with opener(request, timeout=30) as response:
        return json.load(response)


def _count(token, query, opener):
    item = _get(SEARCH + "?" + urllib.parse.urlencode({"q": query, "per_page": 1}), token, opener)
    count = item.get("total_count")
    if type(count) is not int or count < 0 or item.get("incomplete_results") is not False:
        raise ValueError("unverified count response")
    return count


def collect_work_table(token, org, opener=urllib.request.urlopen, now=None):
    if not token or not org:
        raise ValueError("token and org are required")
    now = now or dt.datetime.now(dt.timezone.utc)
    repos = _get(REPOS.format(org=urllib.parse.quote(org)), token, opener)
    if not isinstance(repos, list):
        raise ValueError("unverified repository listing")
    names = []
    for entry in repos:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or entry.get("archived") is not False:
            if entry.get("archived"):
                continue
            raise ValueError("unverified repository entry")
        names.append(name)
    if not names:
        raise ValueError("empty repository universe")
    since = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for name in sorted(names):
        opened = _count(token, "org:" + org + " repo:" + org + "/" + name + " is:issue is:open", opener)
        closed_24h = _count(
            token,
            "org:" + org + " repo:" + org + "/" + name + " is:issue is:closed closed:>" + since,
            opener,
        )
        rows.append({"repo": name, "open": opened, "closed_24h": closed_24h})
    return {"rows": rows, "window_since": since, "snapshot_at": now.isoformat()}
