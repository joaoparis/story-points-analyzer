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

_DEFAULT_TITLE_PREFIX = "BuddyBuilders — Story Point Accuracy Report"
_SPACE_KEY = "NWAP"
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


def save_markdown(issues: list[Issue], months: int, output_path: str) -> None:
    """Build the report and write it as a Markdown file."""
    buckets = _compute_buckets(issues)
    content = _build_markdown(buckets, issues, months)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


def publish_to_confluence(issues: list[Issue], months: int, title: str | None = None) -> None:
    """Build the report and publish to Confluence.

    title behaviour:
    - Provided + page exists  → replace that page
    - Provided + no page yet  → create with that title
    - Not provided            → always create a new page with an auto-generated title
    """
    _check_confluence_env()
    buckets = _compute_buckets(issues)
    page_title = title or _auto_title()
    _upsert_confluence_page(_build_wiki(buckets, issues, months), page_title, upsert=title is not None)


def _check_confluence_env() -> None:
    missing = [v for v in ("CONFLUENCE_BASE_URL", "CONFLUENCE_TOKEN", "CONFLUENCE_PARENT_PAGE_ID") if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"Missing required env vars for --publish: {', '.join(missing)}\n"
            "Add them to your .env file or run: story-points-analyzer setup"
        )


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
# Cleaned-buckets computation (outliers removed)
# ---------------------------------------------------------------------------

def _compute_buckets_cleaned(buckets: list[BucketStats], all_issues: list[Issue]) -> list[BucketStats]:
    """Recompute bucket stats after removing the outliers identified in the first pass."""
    outlier_keys: set[str] = {issue.key for b in buckets for issue in b.outliers}
    clean_issues = [i for i in all_issues if i.key not in outlier_keys]

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
        lines.append("_No outliers were identified — this table is identical to the summary above._")
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
    return lines


def _build_cleaned_table_wiki(cleaned_buckets: list[BucketStats], has_outliers: bool) -> list[str]:
    lines: list[str] = []
    lines.append("h2. 📊 Summary by Story Points (Outliers Removed)")
    lines.append("")
    if not has_outliers:
        lines.append("_No outliers were identified — this table is identical to the summary above._")
        lines.append("")
        return lines
    if not cleaned_buckets:
        lines.append("_No buckets with 3+ issues remain after removing outliers._")
        lines.append("")
        return lines
    lines.append("|| SP || Count || Mean days || Std Dev || Median || P75 || P95 ||")
    for b in cleaned_buckets:
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp
        lines.append(
            f"| {sp_label} | {b.count} | {b.mean:.1f} | {b.std_dev:.1f} "
            f"| {b.median:.1f} | {b.p75:.1f} | {b.p95:.1f} |"
        )
    lines.append("")
    lines.append(
        "Column guide: "
        "*SP* — Story Points | "
        "*Count* — Issues remaining after outlier removal | "
        "*Mean days* — Average cycle time | "
        "*Std Dev* — Spread | "
        "*Median* — P50 | "
        "*P75* — 75th percentile | "
        "*P95* — 95th percentile"
    )
    lines.append("")
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
            "_No sprint data found on the fetched issues — chart unavailable._",
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
            cells.append(f"{sum(times)/len(times):.1f}" if times else "—")
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("_Values are mean cycle days. `—` = no issues for that SP in that sprint._")
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
        lines.append(f'    title "SP {sp_label} — Mean Cycle Days per Sprint"')
        lines.append(f'    x-axis [{sprint_labels}]')
        lines.append(f'    y-axis "Mean Cycle Days" 0 --> {y_max}')
        lines.append(f'    bar [{", ".join(values)}]')
        lines.append("```")
        lines.append("")

    return lines


def _build_sprint_chart_wiki(issues: list[Issue]) -> list[str]:
    sprint_issues = [i for i in issues if i.sprint]
    if not sprint_issues:
        return [
            "h2. 📈 Mean Cycle Days per Sprint by Story Points",
            "",
            "_No sprint data found on the fetched issues — chart unavailable._",
            "",
        ]

    sprints = sorted({i.sprint for i in sprint_issues}, key=_sprint_sort_key)  # type: ignore[arg-type]
    sp_buckets_present = sorted({i.story_points for i in sprint_issues})

    matrix: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for issue in sprint_issues:
        matrix[issue.sprint][issue.story_points].append(issue.cycle_days)  # type: ignore[index]

    lines: list[str] = []
    lines.append("h2. 📈 Mean Cycle Days per Sprint by Story Points")
    lines.append("")

    datasets: list[tuple[str, list[float]]] = []
    sprint_labels = [_sprint_display_label(s) for s in sprints]
    for sp in sp_buckets_present:
        sp_label = int(sp) if sp == int(sp) else sp
        vals = []
        for sprint in sprints:
            times = matrix[sprint].get(sp, [])
            vals.append(sum(times) / len(times) if times else 0.0)
        datasets.append((f"SP {sp_label}", vals))

    svg = _svg_grouped_bar_chart(
        sprint_labels,
        datasets,
        title="Mean Cycle Days per Sprint by Story Points",
        y_label="Mean Cycle Days",
    )
    lines.append(_html_macro(svg))
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Issue-distribution histogram section builders
# ---------------------------------------------------------------------------

def _build_histograms_markdown(buckets: list[BucketStats], all_issues: list[Issue]) -> list[str]:
    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        grouped[issue.story_points].append(issue)

    lines: list[str] = []
    lines.append("## 📊 Issue Distribution by Cycle Days")
    lines.append("")
    lines.append(
        "_One chart per Story Point bucket — X axis: cycle days (rounded), Y axis: number of issues._"
    )
    lines.append("")

    for b in buckets:
        group = grouped.get(b.sp, [])
        if not group:
            continue

        day_counts: dict[int, int] = defaultdict(int)
        for issue in group:
            day_counts[round(issue.cycle_days)] += 1

        days_sorted = sorted(day_counts.keys())
        x_labels = ", ".join(f'"{d}"' for d in days_sorted)
        y_values = ", ".join(str(day_counts[d]) for d in days_sorted)
        y_max = max(day_counts.values()) + 1
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp

        lines.append(f"### SP {sp_label}")
        lines.append("")
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append(f'    title "SP {sp_label} — Issues by Cycle Days"')
        lines.append(f'    x-axis [{x_labels}]')
        lines.append(f'    y-axis "Issues" 0 --> {y_max}')
        lines.append(f'    bar [{y_values}]')
        lines.append("```")
        lines.append("")

    return lines


def _build_histograms_wiki(buckets: list[BucketStats], all_issues: list[Issue]) -> list[str]:
    grouped: dict[float, list[Issue]] = defaultdict(list)
    for issue in all_issues:
        grouped[issue.story_points].append(issue)

    lines: list[str] = []
    lines.append("h2. 📊 Issue Distribution by Cycle Days")
    lines.append("")
    lines.append(
        "_One chart per Story Point bucket — X axis: cycle days (rounded), Y axis: number of issues._"
    )
    lines.append("")

    for b in buckets:
        group = grouped.get(b.sp, [])
        if not group:
            continue

        day_counts: dict[int, int] = defaultdict(int)
        for issue in group:
            day_counts[round(issue.cycle_days)] += 1

        days_sorted = sorted(day_counts.keys())
        sp_label = int(b.sp) if b.sp == int(b.sp) else b.sp

        lines.append(f"h3. SP {sp_label}")
        lines.append("")
        svg = _svg_bar_chart(
            labels=[str(d) for d in days_sorted],
            values=[float(day_counts[d]) for d in days_sorted],
            title=f"SP {sp_label} — Issues by Cycle Days",
            x_label="Cycle Days",
            y_label="Issues",
        )
        lines.append(_html_macro(svg))
        lines.append("")

    return lines


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

    # Cleaned summary table (outliers removed)
    has_outliers = any(b.outliers for b in buckets)
    cleaned_buckets = _compute_buckets_cleaned(buckets, all_issues)
    lines.extend(_build_cleaned_table_markdown(cleaned_buckets, has_outliers))

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

    # Sprint chart
    lines.extend(_build_sprint_chart_markdown(all_issues))

    # Issue-distribution histograms
    lines.extend(_build_histograms_markdown(buckets, all_issues))

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

    # Cleaned summary table (outliers removed)
    has_outliers = any(b.outliers for b in buckets)
    cleaned_buckets = _compute_buckets_cleaned(buckets, all_issues)
    lines.extend(_build_cleaned_table_wiki(cleaned_buckets, has_outliers))

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

    # Sprint chart
    lines.extend(_build_sprint_chart_wiki(all_issues))

    # Issue-distribution histograms
    lines.extend(_build_histograms_wiki(buckets, all_issues))

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
        params={"title": title, "spaceKey": _SPACE_KEY, "expand": "version"},
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
        "ancestors": [{"id": os.environ["CONFLUENCE_PARENT_PAGE_ID"]}],
        "body": {"wiki": {"value": markup, "representation": "wiki"}},
    }
    resp = requests.post(
        f"{os.environ['CONFLUENCE_BASE_URL']}/rest/api/content",
        headers=_confluence_headers(),
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
        "body": {"wiki": {"value": markup, "representation": "wiki"}},
    }
    resp = requests.put(
        f"{os.environ['CONFLUENCE_BASE_URL']}/rest/api/content/{page_id}",
        headers=_confluence_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


def _page_url(page_id: str) -> str:
    return f"{os.environ['CONFLUENCE_BASE_URL']}/pages/viewpage.action?pageId={page_id}"
