# BuddyBuilders — Story Point Accuracy Report

Detects stories and tasks that took disproportionately long relative to their
story point estimate. Uses statistical outlier detection (cycle time > mean + 1σ
within each SP bucket) rather than a rigid "1 SP = N days" rule.

---

## Installation

### Option A — Homebrew (recommended)

```bash
brew tap joaoparis/scrum-tools
brew install story-points-analyzer
```

### Option B — pip (from source)

```bash
python3 -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
pip install .
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

---

## Usage

```bash
# Publish report to Confluence (last 6 months, default)
story-points-analyzer

# Analyse a different time window
story-points-analyzer --months 3

# Dry run — prints Markdown to stdout, no page written
story-points-analyzer --dry-run
story-points-analyzer --months 12 --dry-run

# Upsert a named Confluence page (replaces it if it already exists)
story-points-analyzer --title "BuddyBuilders Sprint 42"

# Show all options
story-points-analyzer --help
```

---

## File structure

| Path | Responsibility |
|------|---------------|
| `story_points_analyzer/jira.py` | Jira REST client: JQL queries, changelog parsing, cycle time computation |
| `story_points_analyzer/report.py` | Stats engine (mean / σ / percentiles / outliers) + Confluence page builder & publisher |
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
For each story-point bucket (minimum 3 issues):

```
threshold = mean_cycle_days + population_std_dev
```

In a normal distribution this marks the slowest ~16% — enough to surface
genuinely slow issues without flagging half the team every sprint.

### Buckets with `std_dev = 0`
When every issue in a bucket has the same cycle time the standard deviation is
zero. No outliers are flagged for that bucket (there is nothing to distinguish).

---

## Confluence output

- **Space:** NWAP  
- **Parent page:** `1948993578` (12 Buddy Builders Sprint Retrospective)  
- **Page title:** `BuddyBuilders — Story Point Accuracy Report`  
- The script creates the page on first run and updates it on subsequent runs.
