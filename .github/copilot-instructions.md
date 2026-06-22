# Copilot Instructions — Story Points Analyzer

## Project purpose
A Python CLI tool for the **BuddyBuilders** team at BMW. It fetches completed Jira stories/tasks, computes cycle-time statistics per story-point bucket, flags statistical outliers, and optionally publishes a report to Confluence.

---

## Stack
- **Language:** Python 3.10+
- **Dependencies:** `requests`, `python-dotenv` (see `pyproject.toml`)
- **Build backend:** `hatchling`
- **Entry point:** `story-points-analyzer` → `story_points_analyzer.main:main`
- **No test suite exists yet.** Do not run `pytest` — it is not installed.

---

## Architecture — four files, single responsibility each

| File | Responsibility |
|------|---------------|
| `story_points_analyzer/jira.py` | Jira REST client: paginated JQL fetch, changelog parsing, cycle time computation |
| `story_points_analyzer/report.py` | Stats engine (mean / σ / percentiles / outliers) + Markdown builder + Confluence publisher |
| `story_points_analyzer/main.py` | CLI argument parsing and orchestration only |
| `story_points_analyzer/setup_wizard.py` | Interactive `.env` setup wizard |

Keep this separation strict. Do not add Jira logic to `report.py` or stats logic to `jira.py`.

---

## Jira specifics

| Concept | Value |
|---------|-------|
| Story points field | `customfield_10016` |
| Feature team field | `customfield_11400` |
| BuddyBuilders team ID | `59047` |
| Target project | `NWAP` |
| Jira base URL | `https://atc.bmwgroup.net/jira` |

---

## Core algorithm

### Cycle time
Calendar days from **first transition into "In Progress"** → **first transition into "Done" / "Closed" / "Resolved"** from the issue changelog.

Exclusions:
- No story points or SP = 0
- No "In Progress" transition in changelog

### Outlier detection (per SP bucket)
```
threshold = mean_cycle_days + population_std_dev
```
Buckets with fewer than 3 issues are skipped entirely.

When `std_dev = 0` (all identical cycle times), no outliers are flagged.

---

## Environment variables

Loaded from `.env` via `python-dotenv`. **Never commit `.env`.**

| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_BASE_URL` | Always | e.g. `https://atc.bmwgroup.net/jira` |
| `JIRA_TOKEN` | Always | Jira personal access token |
| `JIRA_PROJECT` | Optional | Limit to one project key, e.g. `NWAP` |
| `JIRA_FEATURE_TEAM` | Optional | Filter by feature team name |
| `CONFLUENCE_BASE_URL` | `--publish` only | e.g. `https://atc.bmwgroup.net/wiki` |
| `CONFLUENCE_TOKEN` | `--publish` only | Confluence personal access token |
| `CONFLUENCE_PARENT_PAGE_ID` | `--publish` only | Numeric page ID of the parent page |

---

## CLI flags

```
story-points-analyzer [--months N] [--output FILE] [--publish] [--title "..."] [--verbose]
```

- Default: writes `report.md` locally.
- `--publish`: creates or upserts a Confluence page under `CONFLUENCE_PARENT_PAGE_ID`.
- `--publish --title "X"`: upserts a named page (stable URL / living dashboard).

---

## Code conventions
- Prefer small, focused functions with a single responsibility.
- Use constants for magic strings (status names, field IDs).
- No commented-out code; no dead code.
- Only add comments when the code alone cannot convey intent.
- `requests.Session` is used for HTTP — reuse sessions within a run.

---

## Team context
- **Team:** BuddyBuilders
- **Jira project:** NWAP
- **Confluence parent page ID:** `1948993578` (12 Buddy Builders Sprint Retrospective)
