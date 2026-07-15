# Story Point Accuracy Analyzer

Fetches completed Jira stories and tasks for your team, then computes cycle
time statistics per story-point bucket — mean, median, P75, P95 and outliers.
Uses statistical outlier detection (cycle time > mean + 1σ within each SP
bucket) rather than a rigid "1 SP = N days" rule.

---

## Installation

### Option A — pip (from source)

```bash
python3 -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
pip install .
```

### Option B — Homebrew (if published)

```bash
brew tap joaoparis/scrum-tools
brew install joaoparis/scrum-tools/story-points-analyzer
```

---

## Setup

Run the interactive setup wizard to create your `.env` file:

```bash
story-points-analyzer setup
```

The wizard prompts for all required and optional credentials and writes them
to a `.env` file in the current directory. The `.env` file is gitignored — never
commit it.

Alternatively, copy the template and fill in values manually:

```bash
cp .env.example .env
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `JIRA_BASE_URL` | Always | e.g. `https://your-org.atlassian.net` |
| `JIRA_TOKEN` | Always | Jira personal access token |
| `JIRA_PROJECT` | Optional | Limit to one project key, e.g. `MYPROJ` (strongly recommended — queries span the whole instance otherwise) |
| `JIRA_FEATURE_TEAM` | Optional | Filter by feature team name, e.g. `Buddy Builders` |
| `CONFLUENCE_BASE_URL` | `--publish` only | e.g. `https://your-org.atlassian.net/wiki` |
| `CONFLUENCE_TOKEN` | `--publish` only | Confluence personal access token |
| `CONFLUENCE_PARENT_PAGE_ID` | `--publish` only | Numeric page ID of the parent page |
| `CONFLUENCE_SPACE_KEY` | `--publish` only | Confluence space key the page lives in |

---

## Usage

```bash
# Write report.md locally (default)
story-points-analyzer

# Different time window
story-points-analyzer --months 3

# Custom output filename
story-points-analyzer --output sprint42.md

# Verbose: print progress per issue + per-bucket timing
story-points-analyzer --verbose

# Publish to Confluence (requires Confluence env vars)
story-points-analyzer --publish

# Publish and upsert a named Confluence page (replaces it if it already exists)
story-points-analyzer --publish --title "Team Sprint 42"

# Show all options
story-points-analyzer --help
```

---

## Sample output

Running the tool produces a Markdown report like this:

```
# Story Point Accuracy Report
Period: last 6 months | Issues analysed: 47 | Generated: 2026-06-15
```

### Per-bucket summary table

| SP | Count | Mean (days) | Median | P75 | P95 | Std Dev | Outliers |
|----|-------|-------------|--------|-----|-----|---------|----------|
| 1 | 18 | 2.4 | 2.0 | 3.0 | 5.0 | 1.1 | 0 |
| 2 | 14 | 4.1 | 3.5 | 5.0 | 9.0 | 2.3 | 2 |
| 3 | 8 | 5.8 | 5.5 | 7.0 | 11.0 | 2.9 | 1 |
| 5 | 4 | 9.2 | 8.5 | 11.0 | — | 3.4 | 1 |
| 8 | 2 | 14.0 | 14.0 | — | — | 0.0 | 0 |
| 13 | 1 | 21.0 | — | — | — | 0.0 | 0 |

Outlier issues (those with cycle time > mean + 1σ for their bucket) are listed
beneath the table with their key, summary, and actual cycle days.

---

## File structure

| Path | Responsibility |
|------|---------------|
| `story_points_analyzer/jira.py` | Jira REST client: JQL queries, changelog parsing, cycle time computation |
| `story_points_analyzer/report.py` | Stats engine (mean / σ / percentiles / outliers) + Markdown builder + Confluence publisher |
| `story_points_analyzer/main.py` | CLI argument parsing and orchestration only |
| `story_points_analyzer/setup_wizard.py` | Interactive `.env` setup wizard |
| `pyproject.toml` | Package metadata, dependencies, and `story-points-analyzer` entry point |

---

## Algorithm

### What is cycle time?

Calendar days from the **first transition into "In Progress"** to the **first
transition into "Done" / "Closed" / "Resolved"** in the issue's changelog.

Issues are excluded when:
- They have no story points (or SP = 0)
- They were never transitioned to "In Progress" (e.g. closed directly from backlog)

### Outlier threshold

For each story-point bucket:

```
threshold = mean_cycle_days + population_std_dev
```

In a normal distribution this marks the slowest ~16% — enough to surface
genuinely slow issues without flagging half the team every sprint.

### Buckets with `std_dev = 0`

When every issue in a bucket has the same cycle time the standard deviation is
zero. No outliers are flagged for that bucket (there is nothing to distinguish).

---

## Publishing to Confluence

When you run with `--publish`, the tool creates (or upserts) a page under the
parent you configure via `CONFLUENCE_PARENT_PAGE_ID`, in the space given by
`CONFLUENCE_SPACE_KEY`.

Title behaviour:
- **`--publish`** with no `--title` → creates a new page with an auto-generated
  timestamp title each run.
- **`--publish --title "My Page"`** → creates the page if it doesn't exist;
  replaces it in-place if it does. Use this for a living dashboard that stays
  at the same URL.

