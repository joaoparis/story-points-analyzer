"""
Stats engine + Confluence publisher.

Single responsibility: consume Issue data, compute statistics, produce and
publish the Confluence report page.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from story_points_analyzer.jira import Issue

CONFLUENCE_BASE_URL = os.environ["CONFLUENCE_BASE_URL"]
CONFLUENCE_TOKEN = os.environ["CONFLUENCE_TOKEN"]
CONFLUENCE_PARENT_PAGE_ID = os.environ["CONFLUENCE_PARENT_PAGE_ID"]

_DEFAULT_TITLE_PREFIX = "BuddyBuilders — Story Point Accuracy Report"
_SPACE_KEY = "PROJ"
_MIN_BUCKET_SIZE = 1

_HEADERS = {
    "Authorization": f"Bearer {CONFLUENCE_TOKEN}",
    "Content-Type": "application/json",
}


@dataclass
class BucketStats:
    sp: float
    count: int
    mean: float
    std_dev: float
    median: float
    p75: float
    p95: float
    outlier_threshold: float
    outliers: list[Issue]


def publish(issues: list[Issue], months: int, dry_run: bool = False, title: str | None = None) -> None:
    """Build the report and publish (or print) the Confluence page.

    title behaviour:
    - Provided + page exists  → replace that page
    - Provided + no page yet  → create with that title
    - Not provided            → always create a new page with an auto-generated title
    """
    buckets = _compute_buckets(issues)

    if dry_run:
        print(_build_markdown(buckets, issues, months))
        return

    page_title = title or _auto_title()
    _upsert_confluence_page(_build_wiki(buckets, issues, months), page_title, upsert=title is not None)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _compute_buckets(issues: list[Issue]) -> list[BucketStats]:
    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in issues:
        grouped[issue.story_points].append(issue)

    buckets: list[BucketStats] = []
    for sp in sorted(grouped):
        group = grouped[sp]
        if len(group) < _MIN_BUCKET_SIZE:
            continue
        buckets.append(_bucket_stats(sp, group))
    return buckets


def _bucket_stats(sp: float, issues: list[Issue]) -> BucketStats:
    times = sorted(i.cycle_days for i in issues)
    n = len(times)
    mean = sum(times) / n
    variance = sum((x - mean) ** 2 for x in times) / n
    std_dev = math.sqrt(variance)
    median = _percentile(times, 50)
    p75 = _percentile(times, 75)
    p95 = _percentile(times, 95)
    threshold = mean + std_dev
    outliers = [i for i in issues if i.cycle_days > threshold] if std_dev > 0 else []
    outliers.sort(key=lambda i: i.cycle_days, reverse=True)
    return BucketStats(
        sp=sp,
        count=n,
        mean=mean,
        std_dev=std_dev,
        median=median,
        p75=p75,
        p95=p95,
        outlier_threshold=threshold,
        outliers=outliers,
    )


def _percentile(sorted_values: list[float], pct: int) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_values[-1]
    frac = idx - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


# ---------------------------------------------------------------------------
# Markdown builder (dry-run / local preview)
# ---------------------------------------------------------------------------

def _build_markdown(buckets: list[BucketStats], all_issues: list[Issue], months: int) -> str:
    analysed = sum(b.count for b in buckets)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jira_base = os.environ["JIRA_BASE_URL"]

    lines: list[str] = []

    lines.append("# 📊 BuddyBuilders — Story Point Accuracy Report")
    lines.append("")
    lines.append(f"_Generated: {generated} | Window: last {months} months | Issues analysed: {analysed}_")
    lines.append("")

    # Summary table
    lines.append("## 📊 Summary by Story Points")
    lines.append("")
    lines.append("| SP | Count | Mean days | Std Dev | Median | P75 | P95 | Outliers |")
    lines.append("|----|-------|-----------|---------|--------|-----|-----|----------|")
    for b in buckets:
        outlier_pct = int(round(len(b.outliers) / b.count * 100)) if b.count else 0
        outlier_cell = f"{len(b.outliers)} ({outlier_pct}%)" if b.outliers else "—"
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
        lines.append(
            f"| {sp_label} | {b.count} | {b.mean:.1f} | {b.std_dev:.1f} "
            f"| {b.median:.1f} | {b.p75:.1f} | {b.p95:.1f} | {outlier_cell} |"
        )

    lines.append("")

    # Outliers table
    all_outliers = [(issue, b) for b in buckets for issue in b.outliers]

    if all_outliers:
        lines.append("## ⚠️ Outliers (cycle time > mean + 1σ for their SP group)")
        lines.append("")
        lines.append("| Issue | Summary | SP | Cycle days | Ceiling | Over by |")
        lines.append("|-------|---------|----|------------|---------|---------|")
        for issue, b in sorted(all_outliers, key=lambda t: t[0].cycle_days, reverse=True):
            over_by = issue.cycle_days - b.outlier_threshold
            sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
            issue_url = f"{jira_base}/browse/{issue.key}"
            lines.append(
                f"| [{issue.key}]({issue_url}) | {issue.summary} | {sp_label} "
                f"| {issue.cycle_days:.1f} | {b.outlier_threshold:.1f} d | +{over_by:.1f} d |"
            )
        lines.append("")
    else:
        lines.append("## ✅ No outliers detected")
        lines.append("")

    # Methodology
    lines.append("## 📝 Methodology")
    lines.append("")
    lines.append(
        "Cycle time = calendar days from first _In Progress_ transition to first "
        "_Done/Closed/Resolved_ transition."
    )
    lines.append(
        "Outlier = cycle time > mean + 1 standard deviation within the same SP bucket."
    )
    lines.append(
        "All SP buckets are included regardless of size."
    )
    lines.append(
        "Issues without story points or without an _In Progress_ transition are excluded."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence wiki builder
# ---------------------------------------------------------------------------

def _build_wiki(buckets: list[BucketStats], all_issues: list[Issue], months: int) -> str:
    analysed = sum(b.count for b in buckets)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jira_base = os.environ["JIRA_BASE_URL"]

    lines: list[str] = []

    lines.append(
        f"Generated: {generated} | Window: last {months} months | Issues analysed: {analysed}"
    )
    lines.append("")

    # Summary table
    lines.append("h2. 📊 Summary by Story Points")
    lines.append("")
    lines.append(
        "|| SP || Count || Mean days || Std Dev || Median || P75 || P95 || Outliers ||"
    )
    for b in buckets:
        outlier_pct = int(round(len(b.outliers) / b.count * 100)) if b.count else 0
        outlier_cell = f"{len(b.outliers)} ({outlier_pct}%)" if b.outliers else "—"
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
        lines.append(
            f"| {sp_label} | {b.count} | {b.mean:.1f} | {b.std_dev:.1f} "
            f"| {b.median:.1f} | {b.p75:.1f} | {b.p95:.1f} | {outlier_cell} |"
        )

    lines.append("")
    lines.append(
        "Column guide: "
        "*SP* — Story Points | "
        "*Count* — Issues in bucket | "
        "*Mean days* — Average cycle time | "
        "*Std Dev* — Spread of cycle times | "
        "*Median* — P50 cycle time | "
        "*P75* — 75th percentile | "
        "*P95* — 95th percentile | "
        "*Outliers* — cycle time > mean + 1σ"
    )
    lines.append("")

    # Outliers table
    all_outliers = [
        (issue, b)
        for b in buckets
        for issue in b.outliers
    ]

    if all_outliers:
        lines.append("h2. ⚠️ Outliers (cycle time > mean + 1σ for their SP group)")
        lines.append("")
        lines.append("|| Issue || Summary || SP || Cycle days || Ceiling || Over by ||")
        for issue, b in sorted(all_outliers, key=lambda t: t[0].cycle_days, reverse=True):
            over_by = issue.cycle_days - b.outlier_threshold
            sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
            issue_url = f"{jira_base}/browse/{issue.key}"
            lines.append(
                f"| [{issue.key}|{issue_url}] | {issue.summary} | {sp_label} "
                f"| {issue.cycle_days:.1f} | {b.outlier_threshold:.1f} d | +{over_by:.1f} d |"
            )
        lines.append("")
        lines.append(
            "Column guide: "
            "*Issue* — Jira issue key (hyperlinked) | "
            "*Cycle days* — Actual duration | "
            "*Ceiling* — mean + 1σ for its SP bucket | "
            "*Over by* — Days beyond the ceiling"
        )
        lines.append("")
    else:
        lines.append("h2. ✅ No outliers detected")
        lines.append("")

    # Methodology
    lines.append("h2. 📝 Methodology")
    lines.append("")
    lines.append(
        "Cycle time = calendar days from first _In Progress_ transition to first "
        "_Done/Closed/Resolved_ transition."
    )
    lines.append(
        "Outlier = cycle time > mean + 1 standard deviation within the same SP bucket."
    )
    lines.append(
        "All SP buckets are included regardless of size."
    )
    lines.append(
        "Issues without story points or without an _In Progress_ transition are excluded."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence page upsert
# ---------------------------------------------------------------------------

def _auto_title() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{_DEFAULT_TITLE_PREFIX} — {ts}"


# ---------------------------------------------------------------------------
# Confluence page upsert
# ---------------------------------------------------------------------------

def _upsert_confluence_page(markup: str, title: str, upsert: bool) -> None:
    existing = _find_existing_page(title) if upsert else None
    if existing:
        _update_page(existing["id"], existing["version"]["number"] + 1, markup, title)
        print(f"Updated Confluence page: {_page_url(existing['id'])}")
    else:
        page_id = _create_page(markup, title)
        print(f"Created Confluence page: {_page_url(page_id)}")


def _find_existing_page(title: str) -> dict | None:
    resp = requests.get(
        f"{CONFLUENCE_BASE_URL}/rest/api/content",
        headers=_HEADERS,
        params={
            "title": title,
            "spaceKey": _SPACE_KEY,
            "expand": "version",
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def _create_page(markup: str, title: str) -> str:
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": _SPACE_KEY},
        "ancestors": [{"id": CONFLUENCE_PARENT_PAGE_ID}],
        "body": {
            "wiki": {"value": markup, "representation": "wiki"}
        },
    }
    resp = requests.post(
        f"{CONFLUENCE_BASE_URL}/rest/api/content",
        headers=_HEADERS,
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _update_page(page_id: str, new_version: int, markup: str, title: str) -> None:
    payload = {
        "type": "page",
        "title": title,
        "version": {"number": new_version},
        "body": {
            "wiki": {"value": markup, "representation": "wiki"}
        },
    }
    resp = requests.put(
        f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}",
        headers=_HEADERS,
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


def _page_url(page_id: str) -> str:
    return f"{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={page_id}"
