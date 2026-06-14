"""
Interactive setup wizard.

Single responsibility: guide the user through creating or updating a .env file.
"""

from __future__ import annotations

from pathlib import Path

_ENV_FILE = Path(".env")

_VARS: list[tuple[str, str, bool]] = [
    ("JIRA_BASE_URL", "Jira base URL (e.g. https://your-org.example.net/jira)", True),
    ("JIRA_TOKEN", "Jira personal access token", True),
    ("CONFLUENCE_BASE_URL", "Confluence base URL (e.g. https://your-org.example.net/confluence)", True),
    ("CONFLUENCE_TOKEN", "Confluence personal access token", True),
    ("CONFLUENCE_PARENT_PAGE_ID", "Confluence parent page ID (numeric)", True),
    ("JIRA_PROJECT", "Jira project key to limit scope (optional, e.g. PROJ)", False),
    ("JIRA_FEATURE_TEAM", 'Feature team filter (optional, e.g. "Buddy Builders")', False),
]


def _load_existing() -> dict[str, str]:
    """Read key=value pairs from .env if it exists."""
    values: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def _write_env(values: dict[str, str]) -> None:
    lines: list[str] = []
    for key, _desc, required in _VARS:
        val = values.get(key, "")
        if val:
            lines.append(f"{key}={val}")
        elif required:
            lines.append(f"# {key}=")
    _ENV_FILE.write_text("\n".join(lines) + "\n")


def run_setup() -> None:
    """Interactive wizard to create or update .env."""
    existing = _load_existing()

    print()
    print("story-points-analyzer setup")
    print("=" * 40)
    if _ENV_FILE.exists():
        print(f"Updating existing {_ENV_FILE} — press Enter to keep the current value.\n")
    else:
        print(f"Creating {_ENV_FILE}\n")

    values: dict[str, str] = {}
    has_error = False

    for var, description, required in _VARS:
        current = existing.get(var, "")
        label = f"  {var}" + ("" if required else " (optional)")
        print(label)
        print(f"    {description}")
        prompt = f"    [{current}]: " if current else "    : "

        try:
            answer = input(prompt).strip() or current
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            return

        if required and not answer:
            print(f"    ✗ {var} is required.")
            has_error = True

        values[var] = answer
        print()

    if has_error:
        print("⚠  Some required values are missing. The .env file was not written.")
        print("   Re-run `story-points-analyzer setup` to try again.")
        return

    _write_env(values)
    print(f"✓ {_ENV_FILE} written.")
    print()
    print("Next steps:")
    print("  story-points-analyzer --dry-run    # preview the report")
    print("  story-points-analyzer              # publish to Confluence")
