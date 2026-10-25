"""LLM-free GitHub issue-activity collector.

Per the owner's 2026-10-25 definitions (04:10:23 + 04:12:21): every window
row counts issues OPENED (created) in the window (that is also the period
total, whatever their current state), issues created in the window and still
open now, and DISTINCT issues CLOSED in the window regardless of when they
were opened. Windows: org lifetime, last 7 days, last 24 hours, last 3
hours; all windows are anchored to one snapshot timestamp.

Closed counts come from the per-repo issue-events API, never from search:
the search `closed:` qualifier indexes only the CURRENT closed_at and misses
issues closed then reopened (measured in this org's real history: reopen
events exist), so search would silently undercount exactly the metric the
owner defined. The scan pages each repo's full event history once per run
(newest first, pull-request events excluded) and keeps every close event's
timestamp per distinct issue; an issue closed and reopened within a window
still counts as closed there. Measured whole-org cost ~85 pages/run at the
current ~6.7k events, inside the hourly core rate budget; a hard page cap
fails the run closed if volume ever explodes.

Opened/still-open counts come from the search API (created:>= anchored to
the same snapshot). Fails closed: any partial, malformed, or inconsistent
response raises; no stale cache is ever substituted.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
SEARCH_API = "https://api.github.com/search/issues"
ORG_API = "https://api.github.com/orgs/"
MAX_EVENT_PAGES = 500  # hard budget guard; measured whole-org need is ~85

# (Arabic row label, window length or None for org lifetime). Labels are the
# owner's exact wording; ordering is the owner's example order.
WINDOWS = [
    ("منذ البداية", None),
    ("آخر ٧ أيام", dt.timedelta(days=7)),
    ("آخر ٢٤ ساعة", dt.timedelta(hours=24)),
    ("آخر ٣ ساعات", dt.timedelta(hours=3)),
]


def _get_json(url, token, opener):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "hourly-pulse-collector",
        },
    )
    with opener(request, timeout=30) as response:
        return json.load(response)


def _fetch_count(token, org, extra_qualifier, opener):
    query = urllib.parse.urlencode(
        {"q": "org:" + org + " is:issue" + extra_qualifier, "per_page": 1}
    )
    item = _get_json(SEARCH_API + "?" + query, token, opener)
    count = item.get("total_count")
    if type(count) is not int or count < 0 or item.get("incomplete_results") is not False:
        raise ValueError("unverified count response")
    return count


def _fetch_org_created(token, org, opener):
    item = _get_json(ORG_API + urllib.parse.quote(org, safe=""), token, opener)
    created = item.get("created_at")
    if not isinstance(created, str):
        raise ValueError("unverified org response")
    parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    return parsed.isoformat()


def _list_repos(token, org, opener):
    repos, page = [], 1
    while True:
        batch = _get_json(
            ORG_API + urllib.parse.quote(org, safe="") + "/repos?per_page=100&page=" + str(page),
            token, opener,
        )
        if not isinstance(batch, list):
            raise ValueError("unverified repos response")
        for repo in batch:
            name = repo.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("unverified repo entry")
            repos.append(name)
        if len(batch) < 100:
            return repos
        page += 1


def _parse_moment(value):
    if not isinstance(value, str):
        raise ValueError("event timestamp missing")
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("event timestamp requires timezone")
    return moment


def _scan_close_events(token, org, repos, snapshot, opener):
    """Map (repo, issue_id) -> list of close-event moments at/before snapshot."""
    close_events = {}
    total_pages = 0
    for repo in repos:
        page = 0
        while True:
            page += 1
            total_pages += 1
            if total_pages > MAX_EVENT_PAGES:
                raise ValueError("event page budget exceeded")
            batch = _get_json(
                ORG_API + urllib.parse.quote(org, safe="") + "/repos/"
                + urllib.parse.quote(repo, safe="")
                + "/issues/events?per_page=100&page=" + str(page),
                token, opener,
            )
            if not isinstance(batch, list):
                raise ValueError("unverified events response")
            for event in batch:
                if event.get("event") != "closed":
                    continue
                issue = event.get("issue")
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                issue_id = issue.get("id")
                if type(issue_id) is not int:
                    raise ValueError("unverified close event")
                moment = _parse_moment(event.get("created_at"))
                if moment <= snapshot:
                    close_events.setdefault((repo, issue_id), []).append(moment)
            if len(batch) < 100:
                break
    return close_events


def collect_counts(token, org, opener=urllib.request.urlopen, now=None):
    if not token or not org:
        raise ValueError("token and org are required")
    snapshot = now or dt.datetime.now(CAIRO)
    if snapshot.tzinfo is None:
        raise ValueError("snapshot requires timezone")

    repos = _list_repos(token, org, opener)
    close_events = _scan_close_events(token, org, repos, snapshot, opener)

    rows = []
    for label, span in WINDOWS:
        start_iso = None if span is None else (snapshot - span).isoformat()
        opened = _fetch_count(token, org, "" if start_iso is None else " created:>=" + start_iso, opener)
        still_open = _fetch_count(token, org, " is:open" if start_iso is None else " created:>=" + start_iso + " is:open", opener)
        if still_open > opened:
            raise ValueError("still-open exceeds opened; retry next cycle")
        start = None if span is None else snapshot - span
        closed = sum(
            1
            for moments in close_events.values()
            if any((start is None or m >= start) and m <= snapshot for m in moments)
        )
        rows.append({"label": label, "opened": opened, "still_open": still_open, "closed": closed})

    lifetime = rows[0]
    for row in rows[1:]:
        if row["opened"] > lifetime["opened"] or row["closed"] > lifetime["closed"]:
            raise ValueError("window count exceeds lifetime count; retry next cycle")
    return {
        "rows": rows,
        "org": org,
        "org_created_at": _fetch_org_created(token, org, opener),
        "snapshot_at": snapshot.isoformat(),
    }
