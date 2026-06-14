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


def fetch_issues(months: int = 6, verbose: bool = False) -> list[Issue]:
    """Fetch completed Stories & Tasks for each SP bucket sequentially."""
    if not JIRA_PROJECT:
        print("Warning: JIRA_PROJECT is not set. Querying all projects — this may be slow.")

    all_issues: list[Issue] = []
    for sp in _SP_BUCKETS:
        t0 = time.time()
        print(f"  SP={sp}: fetching…", flush=True)
        bucket_issues = _fetch_bucket(sp, months, verbose=verbose)
        elapsed = time.time() - t0
        print(f"  SP={sp}: {len(bucket_issues)} issues ({elapsed:.0f}s).", flush=True)
        all_issues.extend(bucket_issues)
    return all_issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_bucket(sp: int, months: int, verbose: bool = False) -> list[Issue]:
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

    raw_issues = list(_paginate_jql(jql))
    total = len(raw_issues)

    issues: list[Issue] = []
    for idx, raw in enumerate(raw_issues, start=1):
        if verbose:
            print(f"    [{idx}/{total}] {raw['key']}", flush=True)
        histories = _fetch_changelog(raw["key"])
        cycle_days = _compute_cycle_days(histories)
        if cycle_days is not None:
            issues.append(Issue(
                key=raw["key"],
                summary=raw.get("fields", {}).get("summary", ""),
                story_points=float(sp),
                cycle_days=cycle_days,
            ))
    return issues


def _paginate_jql(jql: str, page_size: int = 100) -> Iterator[dict]:
    start = 0
    while True:
        resp = requests.get(
            f"{JIRA_BASE_URL}/rest/api/2/search",
            headers=_HEADERS,
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": "summary",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", [])
        yield from issues
        start += len(issues)
        if start >= data["total"] or not issues:
            break


def _fetch_changelog(issue_key: str) -> list[dict]:
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}",
        headers=_HEADERS,
        params={"expand": "changelog", "fields": ""},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("changelog", {}).get("histories", [])


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
