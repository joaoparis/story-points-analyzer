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
from datetime import datetime, timedelta
from typing import Iterator

import requests

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "")
JIRA_FEATURE_TEAM = os.environ.get("JIRA_FEATURE_TEAM", "")

_SP_BUCKETS = [1, 2, 3, 5, 8, 13]
_SPRINT_FIELD_NAME = "sprint"  # matched case-insensitively against the Jira fields API

# A single sprint is only a few weeks; scanning the full --months window when
# --last-sprint is used would fetch (and discard) months of irrelevant issues.
_LAST_SPRINT_LOOKBACK_MONTHS = 2

_IN_PROGRESS_STATUS = "in progress"
_DONE_STATUSES = {"done", "closed", "resolved"}

# Blocked time is tracked via Jira's standard "Flagged" impediment field, not a
# status column. Turning the flag ON sets toString="Impediment"; turning it OFF
# sets toString="" (empty). Verified against live Jira changelog data.
_FLAGGED_CHANGELOG_FIELD = "Flagged"
_FLAGGED_ON_VALUE = "impediment"

# Field name as it appears in the Jira changelog "items" (not the customfield ID).
_STORY_POINTS_CHANGELOG_FIELD = "Story Points"

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
    blocked_days: float = 0.0
    reestimated: bool = False
    original_sp: float | None = None
    days_since_reestimated: float | None = None
    sprint: str | None = None


def fetch_issues(
    months: int = 6,
    sprint: str | None = None,
    last_sprint: bool = False,
    verbose: bool = False,
) -> list[Issue]:
    """Fetch completed Stories & Tasks for each SP bucket sequentially.

    Time-window selection (mutually exclusive):
      - sprint="NAME"   → analyse exactly that sprint
      - last_sprint=True → analyse only the most recent completed sprint
                           (searched within a short lookback window, not the
                           full ``months`` window, since a sprint is only a
                           few weeks)
      - otherwise        → issues completed within the last ``months`` months
    """
    if not JIRA_PROJECT:
        print("Warning: JIRA_PROJECT is not set. Querying all projects — this may be slow.")

    sprint_field_id = _discover_sprint_field_id()
    if sprint_field_id:
        print(f"  Sprint field detected: {sprint_field_id}", flush=True)
    else:
        print("  Sprint field not found — sprint chart will be unavailable.", flush=True)

    if (sprint or last_sprint) and not sprint_field_id:
        raise SystemExit(
            "Sprint filtering was requested but the Jira 'Sprint' field could not be "
            "found. Remove --sprint/--last-sprint or check your Jira configuration."
        )

    effective_months = _LAST_SPRINT_LOOKBACK_MONTHS if last_sprint else months
    window_clause = _build_window_clause(effective_months, sprint)

    all_issues: list[Issue] = []
    for sp in _SP_BUCKETS:
        t0 = time.time()
        print(f"  SP={sp}: fetching…", flush=True)
        bucket_issues = _fetch_bucket(
            sp, window_clause, sprint_field_id=sprint_field_id, verbose=verbose
        )
        elapsed = time.time() - t0
        print(f"  SP={sp}: {len(bucket_issues)} issues ({elapsed:.0f}s).", flush=True)
        all_issues.extend(bucket_issues)

    if last_sprint:
        all_issues = _filter_latest_sprint(all_issues)

    return all_issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_window_clause(months: int, sprint: str | None) -> str:
    """JQL fragment selecting the time window.

    A named sprint defines its own window, so the completion filter is dropped.
    Otherwise we filter on *completion* (transition into a terminal status)
    rather than 'updated', so the window means 'finished in this period'.
    """
    if sprint:
        return f'AND sprint = "{sprint}" '
    return f'AND status changed to (Done, Closed, Resolved) after -{months * 30}d '


def _fetch_bucket(
    sp: int,
    window_clause: str,
    sprint_field_id: str | None = None,
    verbose: bool = False,
) -> list[Issue]:
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
        f'{window_clause}'
        f'ORDER BY updated DESC'
    )

    raw_issues = list(_paginate_jql(jql, sprint_field_id=sprint_field_id))
    total = len(raw_issues)

    issues: list[Issue] = []
    for idx, raw in enumerate(raw_issues, start=1):
        if verbose:
            print(f"    [{idx}/{total}] {raw['key']}", flush=True)
        histories = _fetch_changelog(raw["key"])
        analysis = _analyze_changelog(histories)
        if analysis is not None:
            sprint = (
                _parse_sprint_name(raw.get("fields", {}).get(sprint_field_id))
                if sprint_field_id else None
            )
            issues.append(Issue(
                key=raw["key"],
                summary=raw.get("fields", {}).get("summary", ""),
                story_points=float(sp),
                cycle_days=analysis.cycle_days,
                blocked_days=analysis.blocked_days,
                reestimated=analysis.reestimated,
                original_sp=analysis.original_sp,
                days_since_reestimated=analysis.days_since_reestimated,
                sprint=sprint,
            ))
    return issues


def _filter_latest_sprint(issues: list[Issue]) -> list[Issue]:
    """Keep only issues belonging to the most recent sprint among the results."""
    named = [i for i in issues if i.sprint]
    if not named:
        print("  --last-sprint: no sprint data on completed issues; nothing to analyse.", flush=True)
        return []
    latest = max((i.sprint for i in named), key=_sprint_sort_key)
    filtered = [i for i in issues if i.sprint == latest]
    print(f"  --last-sprint: analysing sprint '{latest}' ({len(filtered)} issues).", flush=True)
    return filtered


def _sprint_sort_key(name: str) -> tuple:
    """Sort sprints by last numeric component, then lexicographically."""
    numbers = re.findall(r'\d+', name)
    return (int(numbers[-1]), name) if numbers else (0, name)


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


@dataclass
class _ChangelogAnalysis:
    cycle_days: float
    blocked_days: float
    reestimated: bool
    original_sp: float | None
    days_since_reestimated: float | None


def _analyze_changelog(histories: list[dict]) -> _ChangelogAnalysis | None:
    """Single pass over the changelog:

    - Cycle time = first transition INTO "In Progress" → first INTO a terminal status.
    - Blocked time = weekday time spent with the "Flagged" impediment field ON,
      clipped to the [in_progress, done] window, subtracted from cycle time.
    - Re-estimated carry-over = "Story Points" changed after the first In Progress
      transition. When true, the issue's cycle time doesn't cleanly belong to any
      single SP bucket (part of it was worked under the original estimate), so
      callers exclude it from bucket stats and report it separately.
    - Days since re-estimated = cycle time measured from the re-estimation event
      itself (not the original In Progress) to done, so carry-overs can be judged
      on how long the *final* estimate's scope actually took, separate from the
      full elapsed span that also includes work done under the original estimate.
    """
    in_progress_at: datetime | None = None
    done_at: datetime | None = None
    flag_events: list[tuple[datetime, bool]] = []
    sp_changes: list[tuple[datetime, str | None, str | None]] = []

    for history in sorted(histories, key=lambda h: h["created"]):
        created = _parse_dt(history["created"])
        for item in history.get("items", []):
            field = item.get("field")
            if field == "status":
                to_status = (item.get("toString") or "").strip().lower()
                if in_progress_at is None and to_status == _IN_PROGRESS_STATUS:
                    in_progress_at = created
                if done_at is None and to_status in _DONE_STATUSES:
                    done_at = created
            elif field == _FLAGGED_CHANGELOG_FIELD:
                is_on = (item.get("toString") or "").strip().lower() == _FLAGGED_ON_VALUE
                flag_events.append((created, is_on))
            elif field == _STORY_POINTS_CHANGELOG_FIELD:
                sp_changes.append((created, item.get("fromString"), item.get("toString")))

    if in_progress_at is None or done_at is None:
        return None
    if done_at < in_progress_at:
        return None

    raw_days = _weekday_days(in_progress_at, done_at)
    blocked_days = _compute_blocked_days(flag_events, in_progress_at, done_at)
    cycle_days = max(0.0, raw_days - blocked_days)

    reestimated = False
    original_sp: float | None = None
    reestimated_at: datetime | None = None
    for ts, from_str, _to_str in sp_changes:
        if ts > in_progress_at:
            reestimated = True
            reestimated_at = ts
            if from_str is not None:
                try:
                    original_sp = float(from_str)
                except ValueError:
                    original_sp = None
            break

    days_since_reestimated: float | None = None
    if reestimated_at is not None:
        raw_since = _weekday_days(reestimated_at, done_at)
        blocked_since = _compute_blocked_days(flag_events, reestimated_at, done_at)
        days_since_reestimated = max(0.0, raw_since - blocked_since)

    return _ChangelogAnalysis(
        cycle_days=cycle_days,
        blocked_days=blocked_days,
        reestimated=reestimated,
        original_sp=original_sp,
        days_since_reestimated=days_since_reestimated,
    )


def _compute_blocked_days(
    flag_events: list[tuple[datetime, bool]],
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Sum weekday time spent flagged ON, clipped to [window_start, window_end].

    Handles multiple block/unblock cycles and an unresolved flag still ON at
    the end of the changelog (clipped to window_end).
    """
    total = 0.0
    open_at: datetime | None = None
    for ts, is_on in sorted(flag_events, key=lambda e: e[0]):
        if is_on:
            if open_at is None:
                open_at = ts
        else:
            if open_at is not None:
                span_start = max(open_at, window_start)
                span_end = min(ts, window_end)
                if span_end > span_start:
                    total += _weekday_days(span_start, span_end)
                open_at = None
    if open_at is not None:
        span_start = max(open_at, window_start)
        if window_end > span_start:
            total += _weekday_days(span_start, window_end)
    return total


def _weekday_days(start: datetime, end: datetime) -> float:
    """Count Mon–Fri days between two datetimes, excluding weekend days entirely."""
    if end <= start:
        return 0.0

    d = start.date()
    end_d = end.date()
    count = 0

    # Count complete weekdays in [start.date(), end.date())
    while d < end_d:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        d += timedelta(days=1)

    # Add the fraction of the final day elapsed (only if it's a weekday)
    if end_d.weekday() < 5:
        count += (end.hour * 3600 + end.minute * 60 + end.second) / 86_400

    # Subtract the fraction already elapsed on the start day (only if it's a weekday)
    if start.date().weekday() < 5:
        count -= (start.hour * 3600 + start.minute * 60 + start.second) / 86_400

    return max(0.0, float(count))


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        if len(value) > 5 and value[-5] in ("+", "-") and ":" not in value[-5:]:
            value = value[:-2] + ":" + value[-2:]
        return datetime.fromisoformat(value)
