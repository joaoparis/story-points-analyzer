"""
Stats engine + Confluence publisher.

Single responsibility: consume Issue data, compute statistics, produce and
publish the Confluence report page.
"""

from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from story_points_analyzer.jira import Issue

_DEFAULT_TITLE_PREFIX = "Story Point Accuracy Report"
_MIN_BUCKET_SIZE = 1
_MIN_CLEANED_BUCKET_SIZE = 3


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


def save_markdown(issues: list[Issue], window_label: str, output_path: str) -> None:
    """Build the report and write it as a Markdown file."""
    buckets = _compute_buckets(issues)
    content = _build_markdown(buckets, issues, window_label)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


def publish_to_confluence(issues: list[Issue], window_label: str, title: str | None = None) -> None:
    """Build the report and publish to Confluence.

    title behaviour:
    - Provided + page exists  → replace that page
    - Provided + no page yet  → create with that title
    - Not provided            → always create a new page with an auto-generated title
    """
    _check_confluence_env()
    buckets = _compute_buckets(issues)
    page_title = title or _auto_title()
    _upsert_confluence_page(_build_storage(buckets, issues, window_label), page_title, upsert=title is not None)


def _check_confluence_env() -> None:
    missing = [v for v in ("CONFLUENCE_BASE_URL", "CONFLUENCE_TOKEN", "CONFLUENCE_PARENT_PAGE_ID", "CONFLUENCE_SPACE_KEY") if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"Missing required env vars for --publish: {', '.join(missing)}\n"
            "Add them to your .env file or run: story-points-analyzer setup"
        )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _compute_buckets(issues: list[Issue]) -> list[BucketStats]:
    """Group issues into SP buckets and compute stats.

    Re-estimated carry-overs are excluded: their cycle time spans work done
    under a *different* SP value than their final one, so they cannot be a
    clean data point for any single bucket. They are reported separately
    (see _carryover_issues) instead of being silently dropped.
    """
    clean_issues = [i for i in issues if not i.reestimated]
    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in clean_issues:
        grouped[issue.story_points].append(issue)

    buckets: list[BucketStats] = []
    for sp in sorted(grouped):
        group = grouped[sp]
        if len(group) < _MIN_BUCKET_SIZE:
            continue
        buckets.append(_bucket_stats(sp, group))
    return buckets


def _carryover_issues(issues: list[Issue]) -> list[Issue]:
    """Re-estimated carry-overs, sorted by cycle time descending."""
    carryovers = [i for i in issues if i.reestimated]
    carryovers.sort(key=lambda i: i.cycle_days, reverse=True)
    return carryovers


def _consistency_label(b: BucketStats) -> str:
    """Plain-language read on cycle-time spread, for non-technical readers.

    Compares the two numbers already shown in "At a Glance" — Typical time
    (median) and Most finish within (P75) — as an absolute day gap. This
    stays meaningful even for fast buckets: a coefficient-of-variation
    (std_dev / mean) approach blows up for a bucket with a tiny median
    (e.g. 0.6 days), flagging it "Unpredictable" even though the real-world
    spread is only a few days.
    """
    gap = b.p75 - b.median
    if gap <= 1:
        return "Consistent"
    if gap <= 3:
        return "Mixed"
    return "Unpredictable"


def _sp_label(sp: float | None) -> str:
    if sp is None:
        return "N/A"
    return str(int(sp) if sp == int(sp) else sp)


# ---------------------------------------------------------------------------
# Column glossaries — plain-language "what does this column mean" explanations,
# shown as a collapsible block under each table so the page stays scannable
# for a quick look, but every term is one click away for anyone who needs it.
# ---------------------------------------------------------------------------

_GLOSSARY_GLANCE = [
    ("SP", "Story Points, the size estimate given to the work."),
    ("Count", "How many completed issues of this size are in the selected time window."),
    ("Typical time", "The median cycle time: half of the issues finished faster than this, half slower. "
                      "A more honest \"typical\" than an average, since a few very slow issues can't skew it."),
    ("Most finish within", "75% of issues finished at or under this many days, a realistic upper bound "
                            "for planning, beyond the typical case."),
    ("Consistency", "How predictable cycle time is for this size. Consistent = most issues take a similar "
                    "amount of time. Mixed = some spread, but no big surprises. Unpredictable = wide "
                    "variation, hard to give a reliable estimate for this size."),
]

_GLOSSARY_DETAILED = [
    ("SP", "Story Points bucket."),
    ("Count", "Number of completed issues in this bucket."),
    ("Mean days", "The average cycle time, can be pulled higher by just one or two very slow issues."),
    ("Std Dev", "Standard deviation, how spread out cycle times are around the average. "
                "Bigger number = less predictable."),
    ("Median", "Same as \"Typical time\" in At a Glance, the middle value (P50)."),
    ("P75", "Same as \"Most finish within\" in At a Glance, 75th percentile."),
    ("P95", "95th percentile: 95% of issues finished at or under this many days. Shows the worst-case tail."),
    ("Outliers", "Issues that took unusually long compared to the rest of their bucket "
                 "(cycle time above the average plus one standard deviation). Listed in "
                 "\"Tasks That Took Much Longer Than Usual\" below."),
]

_GLOSSARY_OUTLIERS = [
    ("Issue", "Link to the Jira ticket."),
    ("Summary", "Ticket title."),
    ("SP", "Story Points assigned to this ticket."),
    ("Cycle days", "Working days from start (\"In Progress\") to finish, with any blocked time "
                   "already subtracted."),
    ("Blocked days", "How many of those days the ticket was flagged as blocked, useful context for "
                     "why it may have taken longer."),
    ("Ceiling", "The cutoff for this SP size (average plus one standard deviation) above which an issue "
                "is considered unusually slow."),
    ("Over by", "How many days over that cutoff this issue took."),
]

_GLOSSARY_CARRYOVERS = [
    ("Issue", "Link to the Jira ticket."),
    ("Summary", "Ticket title."),
    ("Original SP → Final SP", "The story points the ticket had before, versus what it was changed to "
                               "after work had already started (e.g. re-scoped as a carry-over into a "
                               "new sprint)."),
    ("Cycle days", "Working days from the very first start (\"In Progress\") to finish, including time "
                   "spent under the original, larger scope, so it isn't a clean data point for the "
                   "final SP bucket."),
    ("Days since re-estimated", "Working days from the moment the Story Points changed to finish, i.e. "
                                "how long the final, re-scoped estimate's work actually took, with the "
                                "earlier work under the original estimate excluded."),
    ("Blocked days", "Days flagged as blocked during the full cycle (from the original start to finish)."),
]


def _glossary_markdown(title: str, items: list[tuple[str, str]]) -> list[str]:
    """Render a glossary as a collapsible <details> block (Markdown-friendly HTML)."""
    lines = [f"<details><summary>{title}</summary>", ""]
    for term, desc in items:
        lines.append(f"- **{term}**: {desc}")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return lines


def _glossary_html(title: str, items: list[tuple[str, str]]) -> str:
    """Render a glossary as a Confluence 'expand' macro (collapsed by default)."""
    body = "".join(f"<li><strong>{_esc(term)}</strong>: {_esc(desc)}</li>" for term, desc in items)
    return (
        f'<ac:structured-macro ac:name="expand" ac:schema-version="1">'
        f'<ac:parameter ac:name="title">{_esc(title)}</ac:parameter>'
        f'<ac:rich-text-body><ul>{body}</ul></ac:rich-text-body>'
        f'</ac:structured-macro>'
    )


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
# Cleaned-buckets computation (outliers removed)
# ---------------------------------------------------------------------------

def _compute_buckets_cleaned(buckets: list[BucketStats], all_issues: list[Issue]) -> list[BucketStats]:
    """Recompute bucket stats after removing the outliers identified in the first pass.

    Also excludes re-estimated carry-overs (see _compute_buckets) — they never
    entered the first pass, so without this they'd leak back in here.
    """
    outlier_keys: set[str] = {issue.key for b in buckets for issue in b.outliers}
    clean_issues = [i for i in all_issues if i.key not in outlier_keys and not i.reestimated]

    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in clean_issues:
        grouped[issue.story_points].append(issue)

    cleaned: list[BucketStats] = []
    for sp in sorted(grouped):
        group = grouped[sp]
        if len(group) < _MIN_CLEANED_BUCKET_SIZE:
            continue
        cleaned.append(_bucket_stats(sp, group))
    return cleaned


# ---------------------------------------------------------------------------
# Sprint sort helper
# ---------------------------------------------------------------------------

def _sprint_sort_key(name: str) -> tuple:
    """Sort sprints by last numeric component, then lexicographically."""
    numbers = re.findall(r'\d+', name)
    return (int(numbers[-1]), name) if numbers else (0, name)


# ---------------------------------------------------------------------------
# Cleaned-table section builders
# ---------------------------------------------------------------------------

def _build_cleaned_table_markdown(cleaned_buckets: list[BucketStats], has_outliers: bool) -> list[str]:
    lines: list[str] = []
    lines.append("## 📊 Summary by Story Points (Outliers Removed)")
    lines.append("")
    if not has_outliers:
        lines.append("_No outliers were identified, this table is identical to the summary above._")
        lines.append("")
        return lines
    if not cleaned_buckets:
        lines.append("_No buckets with 3+ issues remain after removing outliers._")
        lines.append("")
        return lines
    lines.append("| SP | Count | Mean days | Std Dev | Median | P75 | P95 |")
    lines.append("|----|-------|-----------|---------|--------|-----|-----|")
    for b in cleaned_buckets:
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
        lines.append(
            f"| {sp_label} | {b.count} | {b.mean:.1f} | {b.std_dev:.1f} "
            f"| {b.median:.1f} | {b.p75:.1f} | {b.p95:.1f} |"
        )
    lines.append("")
    lines.append(
        "_Same columns as Detailed Statistics, but the rare, unusually slow issues "
        "(see \"Tasks That Took Much Longer Than Usual\") are excluded, so these numbers "
        "reflect what's typical day-to-day._"
    )
    lines.append("")
    lines.extend(_glossary_markdown("What do these columns mean?", _GLOSSARY_DETAILED[:-1]))
    return lines


_CHART_COLORS = [
    "#4C9BE8", "#F5A623", "#7ED321", "#E8585A", "#9B59B6", "#1ABC9C",
]


def _svg_grouped_bar_chart(
    labels: list[str],
    datasets: list[tuple[str, list[float]]],
    title: str,
    y_label: str = "Value",
    width: int = 820,
    height: int = 460,
) -> str:
    """Produce a self-contained SVG grouped bar chart."""
    n_groups = len(labels)
    n_series = len(datasets)
    if n_groups == 0 or n_series == 0:
        return ""

    m_top, m_right, m_bottom, m_left = 50, 160, 70, 70
    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    all_vals = [v for _, vals in datasets for v in vals if v > 0]
    y_max_val = (max(all_vals) * 1.15) if all_vals else 10

    group_w = plot_w / n_groups
    bar_padding = 0.12
    inner_w = group_w * (1 - bar_padding * 2)
    bar_w = inner_w / n_series

    def x_bar(gi: int, si: int) -> float:
        return m_left + gi * group_w + group_w * bar_padding + si * bar_w

    def y_px(v: float) -> float:
        return m_top + plot_h - (v / y_max_val) * plot_h

    p: list[str] = []
    p.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="font-family:Arial,sans-serif;background:#fff">'
    )

    # Title
    p.append(
        f'<text x="{(m_left + m_left + plot_w) // 2}" y="30" text-anchor="middle" '
        f'font-size="15" font-weight="bold" fill="#222">{title}</text>'
    )

    # Y-axis gridlines + labels
    n_ticks = 5
    for i in range(n_ticks + 1):
        v = y_max_val * i / n_ticks
        y = y_px(v)
        p.append(
            f'<line x1="{m_left}" y1="{y:.1f}" x2="{m_left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e8e8e8" stroke-width="1"/>'
        )
        p.append(
            f'<text x="{m_left - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#666">{v:.1f}</text>'
        )

    # Y-axis label
    cx = 16
    cy = m_top + plot_h // 2
    p.append(
        f'<text transform="rotate(-90,{cx},{cy})" x="{cx}" y="{cy}" '
        f'text-anchor="middle" font-size="12" fill="#555">{y_label}</text>'
    )

    # Axes
    p.append(
        f'<line x1="{m_left}" y1="{m_top}" x2="{m_left}" y2="{m_top + plot_h}" '
        f'stroke="#aaa" stroke-width="1.5"/>'
    )
    p.append(
        f'<line x1="{m_left}" y1="{m_top + plot_h}" x2="{m_left + plot_w}" '
        f'y2="{m_top + plot_h}" stroke="#aaa" stroke-width="1.5"/>'
    )

    # Bars
    for si, (_, values) in enumerate(datasets):
        color = _CHART_COLORS[si % len(_CHART_COLORS)]
        for gi, v in enumerate(values):
            if v <= 0:
                continue
            x = x_bar(gi, si)
            bh = (v / y_max_val) * plot_h
            y = m_top + plot_h - bh
            p.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                f'fill="{color}" opacity="0.87" rx="2"/>'
            )
            # value label on bar if tall enough
            if bh > 16:
                p.append(
                    f'<text x="{x + bar_w / 2:.1f}" y="{y + 12:.1f}" text-anchor="middle" '
                    f'font-size="9" fill="white" font-weight="bold">{v:.1f}</text>'
                )

    # X-axis labels
    for gi, label in enumerate(labels):
        xc = m_left + gi * group_w + group_w / 2
        p.append(
            f'<text x="{xc:.1f}" y="{m_top + plot_h + 18}" text-anchor="middle" '
            f'font-size="12" fill="#444">{label}</text>'
        )

    # Legend
    lx = m_left + plot_w + 16
    for si, (name, _) in enumerate(datasets):
        color = _CHART_COLORS[si % len(_CHART_COLORS)]
        ly = m_top + si * 24
        p.append(f'<rect x="{lx}" y="{ly}" width="14" height="14" fill="{color}" rx="2"/>')
        p.append(
            f'<text x="{lx + 19}" y="{ly + 11}" font-size="12" fill="#333">{name}</text>'
        )

    p.append("</svg>")
    return "\n".join(p)


def _svg_bar_chart(
    labels: list[str],
    values: list[float],
    title: str,
    x_label: str = "",
    y_label: str = "Value",
    width: int = 720,
    height: int = 380,
) -> str:
    """Produce a self-contained SVG single-series bar chart."""
    if not labels or not values:
        return ""

    m_top, m_right, m_bottom, m_left = 50, 30, 60, 60
    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    y_max_val = (max(values) * 1.15) if values else 1
    bar_w = plot_w / len(labels) * 0.7
    bar_gap = plot_w / len(labels) * 0.3

    def x_bar(i: int) -> float:
        slot = plot_w / len(labels)
        return m_left + i * slot + slot * 0.15

    def y_px(v: float) -> float:
        return m_top + plot_h - (v / y_max_val) * plot_h

    p: list[str] = []
    p.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="font-family:Arial,sans-serif;background:#fff">'
    )

    p.append(
        f'<text x="{(m_left + m_left + plot_w) // 2}" y="30" text-anchor="middle" '
        f'font-size="14" font-weight="bold" fill="#222">{title}</text>'
    )

    n_ticks = 4
    for i in range(n_ticks + 1):
        v = y_max_val * i / n_ticks
        y = y_px(v)
        p.append(
            f'<line x1="{m_left}" y1="{y:.1f}" x2="{m_left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e8e8e8" stroke-width="1"/>'
        )
        p.append(
            f'<text x="{m_left - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#666">{int(v) if v == int(v) else v:.1f}</text>'
        )

    cx, cy = 16, m_top + plot_h // 2
    p.append(
        f'<text transform="rotate(-90,{cx},{cy})" x="{cx}" y="{cy}" '
        f'text-anchor="middle" font-size="12" fill="#555">{y_label}</text>'
    )

    p.append(
        f'<line x1="{m_left}" y1="{m_top}" x2="{m_left}" y2="{m_top + plot_h}" '
        f'stroke="#aaa" stroke-width="1.5"/>'
    )
    p.append(
        f'<line x1="{m_left}" y1="{m_top + plot_h}" x2="{m_left + plot_w}" '
        f'y2="{m_top + plot_h}" stroke="#aaa" stroke-width="1.5"/>'
    )

    color = _CHART_COLORS[0]
    for i, v in enumerate(values):
        x = x_bar(i)
        bh = (v / y_max_val) * plot_h
        y = m_top + plot_h - bh
        p.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{color}" opacity="0.85" rx="2"/>'
        )
        if bh > 14:
            p.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y + 12:.1f}" text-anchor="middle" '
                f'font-size="10" fill="white" font-weight="bold">{int(v) if v == int(v) else v}</text>'
            )

    for i, label in enumerate(labels):
        slot = plot_w / len(labels)
        xc = m_left + i * slot + slot / 2
        p.append(
            f'<text x="{xc:.1f}" y="{m_top + plot_h + 18}" text-anchor="middle" '
            f'font-size="11" fill="#444">{label}</text>'
        )

    if x_label:
        p.append(
            f'<text x="{m_left + plot_w // 2}" y="{height - 6}" text-anchor="middle" '
            f'font-size="12" fill="#555">{x_label}</text>'
        )

    p.append("</svg>")
    return "\n".join(p)


def _html_macro(html: str) -> str:
    """Wrap HTML content in the Confluence wiki HTML macro."""
    return f"{{html}}\n{html}\n{{html}}"


def _sprint_display_label(name: str) -> str:
    """Return a short sprint label.

    'Sprint 2538 | Name of sprint' → '2538'
    Falls back to the first number found, then the full name.
    """
    tokens = name.split()
    if len(tokens) >= 2 and tokens[1].rstrip("|").isdigit():
        return tokens[1].rstrip("|")
    match = re.search(r'\d+', name)
    return match.group() if match else name


# ---------------------------------------------------------------------------
# Sprint chart section builders
# ---------------------------------------------------------------------------

def _build_sprint_chart_markdown(issues: list[Issue]) -> list[str]:
    sprint_issues = [i for i in issues if i.sprint]
    if not sprint_issues:
        return [
            "## 📈 Mean Cycle Days per Sprint by Story Points",
            "",
            "_No sprint data found on the fetched issues, chart unavailable._",
            "",
        ]

    sprints = sorted({i.sprint for i in sprint_issues}, key=_sprint_sort_key)  # type: ignore[arg-type]
    sp_buckets_present = sorted({i.story_points for i in sprint_issues})

    matrix: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for issue in sprint_issues:
        matrix[issue.sprint][issue.story_points].append(issue.cycle_days)  # type: ignore[index]

    sprint_labels = ", ".join(f'"{_sprint_display_label(s)}"' for s in sprints)

    lines: list[str] = []
    lines.append("## 📈 Mean Cycle Days per Sprint by Story Points")
    lines.append("")
    lines.append(
        "_**How to read this:** each line follows one story point size across sprints, "
        "showing how long it typically took to finish work of that size in that sprint. "
        "A line trending upward means that size is taking longer than it used to, worth "
        "digging into why. A flat line means delivery time for that size is stable._"
    )
    lines.append("")
    lines.append(
        "> ⚠️ **Mermaid limitation:** grouped/clustered bar charts (all SP values side-by-side "
        "per sprint) are not supported in Mermaid. The full grouped chart is available in the "
        "Confluence report (`--publish`). The table below shows all values; "
        "the per-SP charts follow."
    )
    lines.append("")

    # Summary data matrix table
    sp_header = " | ".join(f"SP {int(sp) if sp == int(sp) else sp}" for sp in sp_buckets_present)
    lines.append(f"| Sprint | {sp_header} |")
    lines.append("|--------|" + "--------|" * len(sp_buckets_present))
    for sprint in sprints:
        label = _sprint_display_label(sprint)
        cells = []
        for sp in sp_buckets_present:
            times = matrix[sprint].get(sp, [])
            cells.append(f"{sum(times)/len(times):.1f}" if times else "N/A")
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("_Values are mean cycle days. `N/A` = no issues for that SP in that sprint._")
    lines.append("")

    for sp in sp_buckets_present:
        sp_label = int(sp) if sp == int(sp) else sp
        values: list[str] = []
        all_sp_means: list[float] = []
        for sprint in sprints:
            times = matrix[sprint].get(sp, [])
            mean = sum(times) / len(times) if times else 0.0
            values.append(f"{mean:.1f}")
            if times:
                all_sp_means.append(mean)

        if not all_sp_means:
            continue

        y_max = math.ceil(max(all_sp_means) * 1.1) or 10

        lines.append(f"### SP {sp_label}")
        lines.append("")
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append(f'    title "SP {sp_label} Mean Cycle Days per Sprint"')
        lines.append(f'    x-axis [{sprint_labels}]')
        lines.append(f'    y-axis "Mean Cycle Days" 0 --> {y_max}')
        lines.append(f'    bar [{", ".join(values)}]')
        lines.append("```")
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Cycle-time breakdown section builders (replaces the old per-day histograms)
# ---------------------------------------------------------------------------

# Canonical, human-friendly cycle-time ranges, in display order. Ranges are
# chosen so every bucket is easy to say out loud in a retro ("same day",
# "a couple of days", "up to a week", ...) rather than a raw day count.
_CYCLE_RANGE_ORDER = ["Same day", "1-2 days", "3-5 days", "6-10 days", "11-20 days", "21+ days"]


def _cycle_range_label(rounded_days: int) -> str:
    """Map a rounded cycle-day count to a human-friendly range label."""
    if rounded_days <= 0:
        return "Same day"
    if rounded_days <= 2:
        return "1-2 days"
    if rounded_days <= 5:
        return "3-5 days"
    if rounded_days <= 10:
        return "6-10 days"
    if rounded_days <= 20:
        return "11-20 days"
    return "21+ days"


def _cycle_time_breakdown(group: list[Issue]) -> list[tuple[str, list[Issue]]]:
    """Group issues into cycle-time ranges, sorted fastest-to-slowest within each range.

    Returns only the ranges that actually have issues in them, in canonical order.
    """
    range_map: dict[str, list[Issue]] = defaultdict(list)
    for issue in group:
        range_map[_cycle_range_label(round(issue.cycle_days))].append(issue)

    result: list[tuple[str, list[Issue]]] = []
    for label in _CYCLE_RANGE_ORDER:
        issues = range_map.get(label)
        if issues:
            result.append((label, sorted(issues, key=lambda i: i.cycle_days)))
    return result


def _build_cycle_breakdown_markdown(
    buckets: list[BucketStats], all_issues: list[Issue], jira_base: str
) -> list[str]:
    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        grouped[issue.story_points].append(issue)

    lines: list[str] = []
    lines.append("## 📊 Cycle Time Breakdown by Story Point")
    lines.append("")
    lines.append(
        "_**How to read this:** for each Story Point size, issues are grouped into "
        "cycle-time ranges (see the \"Cycle days\" explanation at the top of this "
        "report). The Count column shows how many issues landed in each range, and the "
        "Issues column lists exactly which tickets those were, click any of them to open "
        "it in Jira. If most issues sit in the fast ranges (\"Same day\", \"1-2 days\") "
        "with only one or two in the slower ranges, that size is predictable. If issues "
        "are spread across many ranges, that size is less predictable and worth "
        "discussing in retro._"
    )
    lines.append("")

    for b in buckets:
        group = grouped.get(b.sp, [])
        if not group:
            continue
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp

        lines.append(f"### SP {sp_label}")
        lines.append("")
        lines.append("| Cycle time | Count | Issues |")
        lines.append("|------------|-------|--------|")
        for range_label, issues in _cycle_time_breakdown(group):
            issue_links = ", ".join(f"[{i.key}]({jira_base}/browse/{i.key})" for i in issues)
            lines.append(f"| {range_label} | {len(issues)} | {issue_links} |")
        lines.append("")

    return lines


# ---------------------------------------------------------------------------
# Markdown builder (dry-run / local preview)
# ---------------------------------------------------------------------------

def _build_markdown(buckets: list[BucketStats], all_issues: list[Issue], window_label: str) -> str:
    analysed = sum(b.count for b in buckets)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jira_base = os.environ["JIRA_BASE_URL"]

    lines: list[str] = []

    lines.append("# 📊 BuddyBuilders: Story Point Accuracy Report")
    lines.append("")
    lines.append(f"_Generated: {generated} | Window: {window_label} | Issues analysed: {analysed}_")
    lines.append("")
    lines.append(
        "> 💡 **What is \"Cycle days\"?** The number of working days (Mon-Fri) it took "
        "from when work started on a ticket (\"In Progress\") until it was marked Done, "
        "not counting any days it was blocked. Example: a ticket that moved to \"In "
        "Progress\" on a Monday and was marked Done the following Monday (one weekend "
        "in between, not counted) has a cycle time of 5 days. This is the main number "
        "behind every stat and chart below."
    )
    lines.append("")

    # Headline summary — plain language, for non-technical readers
    lines.append("## 📊 At a Glance")
    lines.append("")
    lines.append("| SP | Count | Typical time | Most finish within | Consistency |")
    lines.append("|----|-------|--------------|---------------------|-------------|")
    for b in buckets:
        lines.append(
            f"| {_sp_label(b.sp)} | {b.count} | {b.median:.1f} days "
            f"| {b.p75:.1f} days | {_consistency_label(b)} |"
        )
    lines.append("")
    lines.extend(_glossary_markdown("What do these columns mean?", _GLOSSARY_GLANCE))
    lines.append("")

    # Detailed statistics (for analysis) — full stats, moved out of the headline
    lines.append("## 📐 Detailed Statistics")
    lines.append("")
    lines.append("| SP | Count | Mean days | Std Dev | Median | P75 | P95 | Outliers |")
    lines.append("|----|-------|-----------|---------|--------|-----|-----|----------|")
    for b in buckets:
        outlier_pct = int(round(len(b.outliers) / b.count * 100)) if b.count else 0
        outlier_cell = f"{len(b.outliers)} ({outlier_pct}%)" if b.outliers else "N/A"
        lines.append(
            f"| {_sp_label(b.sp)} | {b.count} | {b.mean:.1f} | {b.std_dev:.1f} "
            f"| {b.median:.1f} | {b.p75:.1f} | {b.p95:.1f} | {outlier_cell} |"
        )

    lines.append("")
    lines.extend(_glossary_markdown("What do these columns mean?", _GLOSSARY_DETAILED))
    lines.append("")

    # Cleaned summary table (outliers removed)
    has_outliers = any(b.outliers for b in buckets)
    cleaned_buckets = _compute_buckets_cleaned(buckets, all_issues)
    lines.extend(_build_cleaned_table_markdown(cleaned_buckets, has_outliers))

    # Outliers table
    all_outliers = [(issue, b) for b in buckets for issue in b.outliers]

    if all_outliers:
        lines.append("## ⚠️ Tasks That Took Much Longer Than Usual")
        lines.append("")
        lines.append("| Issue | Summary | SP | Cycle days | Blocked days | Ceiling | Over by |")
        lines.append("|-------|---------|----|------------|--------------|---------|---------|")
        for issue, b in sorted(all_outliers, key=lambda t: t[0].cycle_days, reverse=True):
            over_by = issue.cycle_days - b.outlier_threshold
            sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
            issue_url = f"{jira_base}/browse/{issue.key}"
            lines.append(
                f"| [{issue.key}]({issue_url}) | {issue.summary} | {sp_label} "
                f"| {issue.cycle_days:.1f} | {issue.blocked_days:.1f} | {b.outlier_threshold:.1f} d | +{over_by:.1f} d |"
            )
        lines.append("")
        lines.extend(_glossary_markdown("What do these columns mean?", _GLOSSARY_OUTLIERS))
        lines.append("")
    else:
        lines.append("## ✅ No outliers detected")
        lines.append("")

    # Re-estimated carry-overs (excluded from bucket stats above)
    carryovers = _carryover_issues(all_issues)
    if carryovers:
        lines.append("## 🔄 Re-estimated Carry-overs")
        lines.append("")
        lines.append(
            "_These issues had their Story Points changed after work already started. "
            "Their cycle time spans work done under a different estimate, so they are "
            "excluded from the bucket statistics above._"
        )
        lines.append("")
        lines.append("| Issue | Summary | Original SP → Final SP | Cycle days | Days since re-estimated | Blocked days |")
        lines.append("|-------|---------|------------------------|------------|--------------------------|--------------|")
        for issue in carryovers:
            issue_url = f"{jira_base}/browse/{issue.key}"
            orig = issue.original_sp
            orig_label = int(orig) if orig is not None and orig == int(orig) else orig
            final_label = int(issue.story_points) if issue.story_points == int(issue.story_points) else issue.story_points
            since_label = f"{issue.days_since_reestimated:.1f}" if issue.days_since_reestimated is not None else "N/A"
            lines.append(
                f"| [{issue.key}]({issue_url}) | {issue.summary} | {orig_label} → {final_label} "
                f"| {issue.cycle_days:.1f} | {since_label} | {issue.blocked_days:.1f} |"
            )
        lines.append("")
        lines.extend(_glossary_markdown("What do these columns mean?", _GLOSSARY_CARRYOVERS))
        lines.append("")

    # Sprint chart
    lines.extend(_build_sprint_chart_markdown(all_issues))

    # Cycle-time breakdown by story point
    lines.extend(_build_cycle_breakdown_markdown(buckets, all_issues, jira_base))

    # Methodology
    lines.append("## 📝 Methodology")
    lines.append("")
    lines.append(
        "Cycle time = working days (Mon-Fri) from first _In Progress_ transition to first "
        "_Done/Closed/Resolved_ transition, minus any time flagged as blocked (Jira "
        "impediment flag)."
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
    lines.append(
        "Issues whose Story Points changed after work started (re-estimated carry-overs) "
        "are excluded from bucket stats and listed separately above."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Confluence storage format builder (XHTML)
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape text for embedding in XHTML storage format."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _xhtml_table(headers: list[str], rows: list[list[str]], raw_cells: bool = False) -> str:
    """Build a Confluence storage-format table. Set raw_cells=True to skip escaping cell content."""
    lines = ["<table><tbody>"]
    if headers:
        lines.append("<tr>" + "".join(f"<th><strong>{_esc(h)}</strong></th>" for h in headers) + "</tr>")
    for row in rows:
        cells = "".join(f"<td>{c if raw_cells else _esc(c)}</td>" for c in row)
        lines.append(f"<tr>{cells}</tr>")
    lines.append("</tbody></table>")
    return "\n".join(lines)


# High-contrast palette for multi-series charts. Chosen so adjacent story-point
# series are never visually confusable (e.g. no two similar greens/blues in a row),
# unlike the Confluence chart macro's default palette. The chart macro (JFreeChart)
# requires hex codes WITH the leading '#' (e.g. "#4472C4"), or it throws
# "Invalid color" and fails to render.
_SERIES_PALETTE = [
    "#4472C4",  # blue
    "#C00000",  # red
    "#ED7D31",  # orange
    "#7030A0",  # purple
    "#2E8B57",  # sea green
    "#FFC000",  # gold
    "#E83E8C",  # magenta
    "#595959",  # gray
]


def _series_colors(count: int) -> str:
    """Return a comma-separated hex color list for the chart macro's `seriesColors` param."""
    return ",".join(_SERIES_PALETTE[i % len(_SERIES_PALETTE)] for i in range(count))


def _xhtml_chart_macro(
    params: dict[str, str],
    headers: list[str],
    rows: list[list[str]],
) -> str:
    """Build a Confluence storage-format chart macro with an embedded data table."""
    param_xml = "".join(
        f'<ac:parameter ac:name="{k}">{_esc(v)}</ac:parameter>'
        for k, v in params.items()
    )
    header_row = "<tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>"
    data_rows = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        f'<ac:structured-macro ac:name="chart" ac:schema-version="1">'
        f"{param_xml}"
        f"<ac:rich-text-body><table><tbody>"
        f"{header_row}{data_rows}"
        f"</tbody></table></ac:rich-text-body>"
        f"</ac:structured-macro>"
    )


def _build_storage(buckets: list[BucketStats], all_issues: list[Issue], window_label: str) -> str:
    """Build the report as Confluence storage format (XHTML)."""
    analysed = sum(b.count for b in buckets)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    jira_base = os.environ["JIRA_BASE_URL"]

    parts: list[str] = []

    parts.append(
        f"<p><em>Generated: {_esc(generated)} | Window: {_esc(window_label)} "
        f"| Issues analysed: {analysed}</em></p>"
    )
    parts.append(
        '<ac:structured-macro ac:name="info" ac:schema-version="1">'
        '<ac:rich-text-body>'
        '<p><strong>What is "Cycle days"?</strong> The number of working days (Mon-Fri) '
        'it took from when work started on a ticket ("In Progress") until it was marked '
        'Done, not counting any days it was blocked. Example: a ticket that moved to '
        '"In Progress" on a Monday and was marked Done the following Monday (one weekend '
        'in between, not counted) has a cycle time of 5 days. This is the main number '
        'behind every stat and chart below.</p>'
        '</ac:rich-text-body>'
        '</ac:structured-macro>'
    )

    # ---- Headline summary — plain language, for non-technical readers -----
    parts.append("<h2>📊 At a Glance</h2>")
    glance_headers = ["SP", "Count", "Typical time", "Most finish within", "Consistency"]
    glance_rows = []
    for b in buckets:
        glance_rows.append([
            _sp_label(b.sp), str(b.count), f"{b.median:.1f} days",
            f"{b.p75:.1f} days", _consistency_label(b),
        ])
    parts.append(_xhtml_table(glance_headers, glance_rows))
    parts.append(_glossary_html("What do these columns mean?", _GLOSSARY_GLANCE))

    # ---- Detailed statistics (for analysis) --------------------------------
    parts.append("<h2>📐 Detailed Statistics</h2>")
    headers = ["SP", "Count", "Mean days", "Std Dev", "Median", "P75", "P95", "Outliers"]
    rows = []
    for b in buckets:
        outlier_pct = int(round(len(b.outliers) / b.count * 100)) if b.count else 0
        outlier_cell = f"{len(b.outliers)} ({outlier_pct}%)" if b.outliers else "N/A"
        rows.append([
            _sp_label(b.sp), str(b.count), f"{b.mean:.1f}", f"{b.std_dev:.1f}",
            f"{b.median:.1f}", f"{b.p75:.1f}", f"{b.p95:.1f}", outlier_cell,
        ])
    parts.append(_xhtml_table(headers, rows))
    parts.append(_glossary_html("What do these columns mean?", _GLOSSARY_DETAILED))

    # ---- Cleaned summary table ---------------------------------------------
    has_outliers = any(b.outliers for b in buckets)
    cleaned_buckets = _compute_buckets_cleaned(buckets, all_issues)
    parts.append("<h2>📊 Summary by Story Points (Outliers Removed)</h2>")
    if not has_outliers:
        parts.append("<p><em>No outliers were identified, this table is identical to the summary above.</em></p>")
    elif not cleaned_buckets:
        parts.append("<p><em>No buckets with 3+ issues remain after removing outliers.</em></p>")
    else:
        clean_headers = ["SP", "Count", "Mean days", "Std Dev", "Median", "P75", "P95"]
        clean_rows = []
        for b in cleaned_buckets:
            sp_label = str(int(b.sp) if b.sp == int(b.sp) else b.sp)
            clean_rows.append([
                sp_label, str(b.count), f"{b.mean:.1f}", f"{b.std_dev:.1f}",
                f"{b.median:.1f}", f"{b.p75:.1f}", f"{b.p95:.1f}",
            ])
        parts.append(_xhtml_table(clean_headers, clean_rows))
        parts.append(
            "<p><em>Same columns as Detailed Statistics, but the rare, unusually slow issues "
            "(see \"Tasks That Took Much Longer Than Usual\") are excluded, so these numbers "
            "reflect what's typical day-to-day.</em></p>"
        )
        parts.append(_glossary_html("What do these columns mean?", _GLOSSARY_DETAILED[:-1]))

    # ---- Outliers table ----------------------------------------------------
    all_outliers = [(issue, b) for b in buckets for issue in b.outliers]
    if all_outliers:
        parts.append("<h2>⚠️ Tasks That Took Much Longer Than Usual</h2>")
        o_headers = ["Issue", "Summary", "SP", "Cycle days", "Blocked days", "Ceiling", "Over by"]
        o_rows = []
        for issue, b in sorted(all_outliers, key=lambda t: t[0].cycle_days, reverse=True):
            over_by = issue.cycle_days - b.outlier_threshold
            sp_label = str(int(b.sp) if b.sp == int(b.sp) else b.sp)
            issue_url = f"{jira_base}/browse/{issue.key}"
            o_rows.append([
                f'<a href="{_esc(issue_url)}">{_esc(issue.key)}</a>',
                _esc(issue.summary),
                sp_label,
                f"{issue.cycle_days:.1f}",
                f"{issue.blocked_days:.1f}",
                f"{b.outlier_threshold:.1f} d",
                f"+{over_by:.1f} d",
            ])
        parts.append(_xhtml_table(o_headers, o_rows, raw_cells=True))
        parts.append(_glossary_html("What do these columns mean?", _GLOSSARY_OUTLIERS))
    else:
        parts.append("<h2>✅ No outliers detected</h2>")

    # ---- Re-estimated carry-overs (excluded from bucket stats above) ------
    carryovers = _carryover_issues(all_issues)
    if carryovers:
        parts.append("<h2>🔄 Re-estimated Carry-overs</h2>")
        parts.append(
            "<p><em>These issues had their Story Points changed after work already started. "
            "Their cycle time spans work done under a different estimate, so they are "
            "excluded from the bucket statistics above.</em></p>"
        )
        c_headers = ["Issue", "Summary", "Original SP → Final SP", "Cycle days", "Days since re-estimated", "Blocked days"]
        c_rows = []
        for issue in carryovers:
            issue_url = f"{jira_base}/browse/{issue.key}"
            orig = issue.original_sp
            orig_label = str(int(orig) if orig is not None and orig == int(orig) else orig)
            final_label = str(int(issue.story_points) if issue.story_points == int(issue.story_points) else issue.story_points)
            since_label = f"{issue.days_since_reestimated:.1f}" if issue.days_since_reestimated is not None else "N/A"
            c_rows.append([
                f'<a href="{_esc(issue_url)}">{_esc(issue.key)}</a>',
                _esc(issue.summary),
                f"{orig_label} → {final_label}",
                f"{issue.cycle_days:.1f}",
                since_label,
                f"{issue.blocked_days:.1f}",
            ])
        parts.append(_xhtml_table(c_headers, c_rows, raw_cells=True))
        parts.append(_glossary_html("What do these columns mean?", _GLOSSARY_CARRYOVERS))

    # ---- Sprint chart ------------------------------------------------------
    sprint_issues = [i for i in all_issues if i.sprint]
    parts.append("<h2>📈 Mean Cycle Days per Sprint by Story Points</h2>")
    parts.append(
        "<p><em>How to read this: each colored series follows one story point size across "
        "sprints, showing how long it typically took to finish work of that size in that "
        "sprint. A series trending upward means that size is taking longer than it used to, "
        "worth digging into why. A flat series means delivery time for that size is stable.</em></p>"
    )
    if not sprint_issues:
        parts.append("<p><em>No sprint data found on the fetched issues, chart unavailable.</em></p>")
    else:
        sprints = sorted({i.sprint for i in sprint_issues}, key=_sprint_sort_key)  # type: ignore[arg-type]
        sp_buckets_present = sorted({i.story_points for i in sprint_issues})
        matrix: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
        for issue in sprint_issues:
            matrix[issue.sprint][issue.story_points].append(issue.cycle_days)  # type: ignore[index]
        sprint_labels = [_sprint_display_label(s) for s in sprints]

        chart_headers = [""] + [f"SP {int(sp) if sp == int(sp) else sp}" for sp in sp_buckets_present]
        chart_rows = []
        for i, sprint in enumerate(sprints):
            row = [sprint_labels[i]]
            for sp in sp_buckets_present:
                times = matrix[sprint].get(sp, [])
                row.append(f"{sum(times)/len(times):.1f}" if times else "0")
            chart_rows.append(row)

        parts.append(_xhtml_chart_macro(
            params={
                "type": "bar",
                "title": "Mean Cycle Days per Sprint by Story Points",
                "yLabel": "Mean Cycle Days",
                "legend": "true",
                "dataOrientation": "vertical",
                "width": "900",
                "height": "500",
                "colors": _series_colors(len(sp_buckets_present)),
                "seriesColors": _series_colors(len(sp_buckets_present)),
            },
            headers=chart_headers,
            rows=chart_rows,
        ))

    # ---- Cycle-time breakdown ------------------------------------------
    parts.append("<h2>📊 Cycle Time Breakdown by Story Point</h2>")
    parts.append(
        "<p><em>How to read this: for each Story Point size, issues are grouped into "
        "cycle-time ranges (see the \"Cycle days\" explanation at the top of this "
        "report). The Count column shows how many issues landed in each range, and the "
        "Issues column lists exactly which tickets those were, click any of them to open "
        "it in Jira. If most issues sit in the fast ranges (\"Same day\", \"1-2 days\") "
        "with only one or two in the slower ranges, that size is predictable. If issues "
        "are spread across many ranges, that size is less predictable and worth "
        "discussing in retro.</em></p>"
    )
    grouped_by_sp: dict[float, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        grouped_by_sp[issue.story_points].append(issue)
    for b in buckets:
        group = grouped_by_sp.get(b.sp, [])
        if not group:
            continue
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
        parts.append(f"<h3>SP {sp_label}</h3>")
        breakdown_rows = []
        for range_label, issues in _cycle_time_breakdown(group):
            issue_links = ", ".join(
                f'<a href="{_esc(f"{jira_base}/browse/{i.key}")}">{_esc(i.key)}</a>' for i in issues
            )
            breakdown_rows.append([range_label, str(len(issues)), issue_links])
        parts.append(_xhtml_table(["Cycle time", "Count", "Issues"], breakdown_rows, raw_cells=True))

    # ---- Methodology -------------------------------------------------------
    parts.append("<h2>📝 Methodology</h2>")
    parts.append(
        "<p>Cycle time = working days (Mon-Fri) from first <em>In Progress</em> transition to first "
        "<em>Done/Closed/Resolved</em> transition, minus any time flagged as blocked "
        "(Jira impediment flag).</p>"
    )
    parts.append("<p>Outlier = cycle time &gt; mean + 1 standard deviation within the same SP bucket.</p>")
    parts.append("<p>All SP buckets are included regardless of size.</p>")
    parts.append("<p>Issues without story points or without an <em>In Progress</em> transition are excluded.</p>")
    parts.append(
        "<p>Issues whose Story Points changed after work started (re-estimated carry-overs) "
        "are excluded from bucket stats and listed separately above.</p>"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Confluence page upsert
# ---------------------------------------------------------------------------

def _auto_title() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{_DEFAULT_TITLE_PREFIX} ({ts})"


# ---------------------------------------------------------------------------
# Confluence page upsert
# ---------------------------------------------------------------------------

def _confluence_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['CONFLUENCE_TOKEN']}",
        "Content-Type": "application/json",
    }


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
        f"{os.environ['CONFLUENCE_BASE_URL']}/rest/api/content",
        headers=_confluence_headers(),
        params={"title": title, "spaceKey": os.environ["CONFLUENCE_SPACE_KEY"], "expand": "version"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def _create_page(markup: str, title: str) -> str:
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": os.environ["CONFLUENCE_SPACE_KEY"]},
        "ancestors": [{"id": os.environ["CONFLUENCE_PARENT_PAGE_ID"]}],
        "body": {"storage": {"value": markup, "representation": "storage"}},
    }
    resp = requests.post(
        f"{os.environ['CONFLUENCE_BASE_URL']}/rest/api/content",
        headers=_confluence_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _update_page(page_id: str, new_version: int, markup: str, title: str) -> None:
    payload = {
        "type": "page",
        "title": title,
        "version": {"number": new_version},
        "body": {"storage": {"value": markup, "representation": "storage"}},
    }
    resp = requests.put(
        f"{os.environ['CONFLUENCE_BASE_URL']}/rest/api/content/{page_id}",
        headers=_confluence_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def _page_url(page_id: str) -> str:
    return f"{os.environ['CONFLUENCE_BASE_URL']}/pages/viewpage.action?pageId={page_id}"
