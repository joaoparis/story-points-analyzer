"""
Jira REST client.

Single responsibility: talk to Jira.
Fetches completed Stories & Tasks per story-point bucket sequentially.

Two-phase approach per bucket:
  1. JQL search (no changelog) — gets issue keys
  2. Per-issue changelog fetch — one at a time
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import requests

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "")
JIRA_FEATURE_TEAM = os.environ.get("JIRA_FEATURE_TEAM", "")

_SP_BUCKETS = [1, 2, 3, 5, 8, 13]
_SPRINT_FIELD_NAME = "sprint"  # matched case-insensitively against the Jira fields API

_IN_PROGRESS_STATUS = "in progress"
_DONE_STATUSES = {"done", "closed", "resolved"}

_HEADERS = {
    "Authorization": f"Bearer {JIRA_TOKEN}",
    "Content-Type": "application/json",
}


@dataclass
class Issue:
    key: str
    summary: str
    story_points: float
    cycle_days: float
    sprint: str | None = None


def fetch_issues(months: int = 6, verbose: bool = False) -> list[Issue]:
    """Fetch completed Stories & Tasks for each SP bucket sequentially."""
    if not JIRA_PROJECT:
        print("Warning: JIRA_PROJECT is not set. Querying all projects — this may be slow.")

    sprint_field_id = _discover_sprint_field_id()
    if sprint_field_id:
        print(f"  Sprint field detected: {sprint_field_id}", flush=True)
    else:
        print("  Sprint field not found — sprint chart will be unavailable.", flush=True)

    all_issues: list[Issue] = []
    for sp in _SP_BUCKETS:
        t0 = time.time()
        print(f"  SP={sp}: fetching…", flush=True)
        bucket_issues = _fetch_bucket(sp, months, sprint_field_id=sprint_field_id, verbose=verbose)
        elapsed = time.time() - t0
        print(f"  SP={sp}: {len(bucket_issues)} issues ({elapsed:.0f}s).", flush=True)
        all_issues.extend(bucket_issues)
    return all_issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_bucket(sp: int, months: int, sprint_field_id: str | None = None, verbose: bool = False) -> list[Issue]:
    """Phase 1: collect issue keys. Phase 2: fetch changelogs one by one."""
    project_clause = f'AND project = {JIRA_PROJECT} ' if JIRA_PROJECT else ''
    team_clause = f'AND "Feature Team" = "{JIRA_FEATURE_TEAM}" ' if JIRA_FEATURE_TEAM else ''
    jql = (
        f'issuetype in (Story, Task) '
        f'AND status in (Done, Closed, Resolved) '
        f'AND statusCategory = Done '
        f'AND "Story Points" = {sp} '
        f'{project_clause}'
        f'{team_clause}'
        f'AND updated >= -{months * 30}d '
        f'ORDER BY updated DESC'
    )

    raw_issues = list(_paginate_jql(jql, sprint_field_id=sprint_field_id))
    total = len(raw_issues)

    issues: list[Issue] = []
    for idx, raw in enumerate(raw_issues, start=1):
        if verbose:
            print(f"    [{idx}/{total}] {raw['key']}", flush=True)
        histories = _fetch_changelog(raw["key"])
        cycle_days = _compute_cycle_days(histories)
        if cycle_days is not None:
            sprint = (
                _parse_sprint_name(raw.get("fields", {}).get(sprint_field_id))
                if sprint_field_id else None
            )
            issues.append(Issue(
                key=raw["key"],
                summary=raw.get("fields", {}).get("summary", ""),
                story_points=float(sp),
                cycle_days=cycle_days,
                sprint=sprint,
            ))
    return issues


def _get(url: str, params: dict | None = None, retries: int = 5) -> requests.Response:
    """GET with exponential backoff on 429."""
    delay = 2.0
    for attempt in range(retries):
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", delay))
            wait = max(retry_after, delay)
            print(f"    rate-limited, retrying in {wait:.0f}s…", flush=True)
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def _paginate_jql(jql: str, page_size: int = 100, sprint_field_id: str | None = None) -> Iterator[dict]:
    fields = "summary" if not sprint_field_id else f"summary,{sprint_field_id}"
    start = 0
    while True:
        resp = _get(
            f"{JIRA_BASE_URL}/rest/api/2/search",
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": fields,
            },
        )
        data = resp.json()
        issues = data.get("issues", [])
        yield from issues
        start += len(issues)
        if start >= data["total"] or not issues:
            break


def _fetch_changelog(issue_key: str) -> list[dict]:
    resp = _get(
        f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}",
        params={"expand": "changelog", "fields": ""},
    )
    return resp.json().get("changelog", {}).get("histories", [])


def _discover_sprint_field_id() -> str | None:
    """Call the Jira fields API to find the field ID whose name is 'Sprint'."""
    try:
        resp = _get(f"{JIRA_BASE_URL}/rest/api/2/field")
        for field in resp.json():
            if field.get("name", "").strip().lower() == _SPRINT_FIELD_NAME:
                return field["id"]
    except Exception:
        pass
    return None


def _parse_sprint_name(sprint_field) -> str | None:
    """Extract the most recent sprint name from the Jira sprint field value.

    Handles both list-of-dicts (Jira Cloud / modern Data Center) and the
    legacy toString string format (older Jira Server).
    """
    if not sprint_field or not isinstance(sprint_field, list):
        return None
    for item in reversed(sprint_field):
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                return str(name)
        elif isinstance(item, str):
            match = re.search(r'name=([^,\]]+)', item)
            if match:
                return match.group(1).strip()
    return None


def _compute_cycle_days(histories: list[dict]) -> float | None:
    """Cycle time = first transition INTO "In Progress" → first INTO a terminal status."""
    in_progress_at: datetime | None = None
    done_at: datetime | None = None

    for history in histories:
        created = _parse_dt(history["created"])
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            to_status = (item.get("toString") or "").strip().lower()
            if in_progress_at is None and to_status == _IN_PROGRESS_STATUS:
                in_progress_at = created
            if done_at is None and to_status in _DONE_STATUSES:
                done_at = created

    if in_progress_at is None or done_at is None:
        return None
    if done_at < in_progress_at:
        return None

    return (done_at - in_progress_at).total_seconds() / 86_400


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        if len(value) > 5 and value[-5] in ("+", "-") and ":" not in value[-5:]:
            value = value[:-2] + ":" + value[-2:]
        return datetime.fromisoformat(value)
