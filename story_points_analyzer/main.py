"""
CLI entry point.

Single responsibility: parse arguments, load environment, orchestrate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story-points-analyzer",
        description=(
            "Fetches completed Jira stories and tasks, computes cycle times per\n"
            "story-point bucket (mean / median / P75 / P95 / outliers), and\n"
            "writes a Markdown report locally or publishes it to Confluence."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables (always required):\n"
            "  JIRA_BASE_URL              e.g. https://atc.bmwgroup.net/jira\n"
            "  JIRA_TOKEN                 Jira personal access token\n"
            "\n"
            "Environment variables (required only with --publish):\n"
            "  CONFLUENCE_BASE_URL        e.g. https://atc.bmwgroup.net/confluence\n"
            "  CONFLUENCE_TOKEN           Confluence personal access token\n"
            "  CONFLUENCE_PARENT_PAGE_ID  Confluence parent page ID\n"
            "\n"
            "Environment variables (optional):\n"
            "  JIRA_PROJECT               Limit to a single project key (e.g. NWAP)\n"
            '  JIRA_FEATURE_TEAM          Limit to a feature team (e.g. "Buddy Builders")\n'
            "\n"
            "Variables are loaded from a .env file in the current directory if present.\n"
            "Run `story-points-analyzer setup` to create one interactively.\n"
            "\n"
            "Examples:\n"
            "  story-points-analyzer                            write report.md locally\n"
            "  story-points-analyzer --months 3                 last 3 months\n"
            "  story-points-analyzer --output sprint27.md       custom output file\n"
            "  story-points-analyzer --publish                  publish to Confluence\n"
            '  story-points-analyzer --publish --title "Sprint 42"  upsert a named page\n'
            "  story-points-analyzer setup                      guided env-var setup\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "setup",
        help="Interactively create or update the .env file with required credentials",
    )

    parser.add_argument(
        "--months",
        type=int,
        default=6,
        metavar="N",
        help="Number of months of history to analyse (default: 6)",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish to Confluence instead of writing a local Markdown file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="report.md",
        metavar="FILE",
        help="Output file path for the local Markdown report (default: report.md)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress for each issue as it is fetched",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        metavar="TITLE",
        help=(
            "Confluence page title (only used with --publish). "
            "If the page already exists it will be replaced; "
            "if it does not exist it will be created. "
            "Omit to always create a new page with an auto-generated title."
        ),
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "setup":
        from story_points_analyzer.setup_wizard import run_setup
        run_setup()
        return

    try:
        from story_points_analyzer import jira as jira_module
        from story_points_analyzer import report as report_module
    except ImportError as exc:
        sys.exit(f"Import error: {exc}. Run: pip install requests python-dotenv")

    import time
    t0 = time.time()
    print(f"Fetching issues from the last {args.months} months…")
    issues = jira_module.fetch_issues(months=args.months, verbose=args.verbose)
    elapsed = time.time() - t0
    print(f"Fetched {len(issues)} issues in {elapsed:.0f}s.")

    if not issues:
        print("No issues found. Nothing to report.")
        return

    if args.publish:
        report_module.publish_to_confluence(issues, months=args.months, title=args.title)
    else:
        output = Path(args.output)
        report_module.save_markdown(issues, months=args.months, output_path=str(output))
        print(f"Report written to {output.resolve()}")


if __name__ == "__main__":
    main()
