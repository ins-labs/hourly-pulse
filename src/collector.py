"""LLM-free GitHub issue-count collector.

Reads org-wide issue counts (total/open/closed) through the GitHub search API.
Fails closed: any partial, malformed, or mutually inconsistent response raises;
no stale cache is ever substituted.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
API = "https://api.github.com/search/issues"


def _fetch_count(token, org, state_qualifier, opener):
    query = urllib.parse.urlencode(
        {"q": "org:" + org + " is:issue" + state_qualifier, "per_page": 1}
    )
    request = urllib.request.Request(
        API + "?" + query,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "hourly-pulse-collector",
        },
    )
    with opener(request, timeout=30) as response:
        item = json.load(response)
    count = item.get("total_count")
    if type(count) is not int or count < 0 or item.get("incomplete_results") is not False:
        raise ValueError("unverified count response")
    return count


def _fetch_org_created(token, org, opener):
    request = urllib.request.Request(
        "https://api.github.com/orgs/" + urllib.parse.quote(org, safe=""),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "hourly-pulse-collector",
        },
    )
    with opener(request, timeout=30) as response:
        item = json.load(response)
    created = item.get("created_at")
    if not isinstance(created, str):
        raise ValueError("unverified org response")
    parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    return parsed.isoformat()


def collect_counts(token, org, opener=urllib.request.urlopen, now=None):
    if not token or not org:
        raise ValueError("token and org are required")
    total = _fetch_count(token, org, "", opener)
    opened = _fetch_count(token, org, " is:open", opener)
    closed = _fetch_count(token, org, " is:closed", opener)
    if opened + closed != total:
        raise ValueError("count pages changed during read; retry next cycle")
    return {
        "total": total,
        "open": opened,
        "closed": closed,
        "org": org,
        "org_created_at": _fetch_org_created(token, org, opener),
        "snapshot_at": (now or dt.datetime.now(CAIRO)).isoformat(),
    }
