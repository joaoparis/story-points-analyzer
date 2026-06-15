#!/usr/bin/env bash
# release.sh — bump version, tag, publish a GitHub release, and update the Homebrew formula.
#
# Usage: ./release.sh <version>
# Example: ./release.sh 1.1.0

set -euo pipefail

VERSION="${1:-}"
TOOL_REPO="joaoparis/story-points-analyzer"
TAP_DIR="../homebrew-scrum-tools"
FORMULA="$TAP_DIR/Formula/story-points-analyzer.rb"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

if [[ -z "$VERSION" ]]; then
  echo "Usage: ./release.sh <version>  (e.g. ./release.sh 1.1.0)"
  exit 1
fi

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: version must be in MAJOR.MINOR.PATCH format (e.g. 1.1.0)"
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: working tree is not clean. Commit or stash your changes first."
  exit 1
fi

if [[ ! -f "$FORMULA" ]]; then
  echo "Error: formula not found at $FORMULA"
  echo "Make sure homebrew-scrum-tools is checked out alongside this repo."
  exit 1
fi

TAG="v$VERSION"

if git rev-parse "$TAG" &>/dev/null; then
  echo "Error: tag $TAG already exists."
  exit 1
fi

# ---------------------------------------------------------------------------
# Bump version in source files
# ---------------------------------------------------------------------------

echo "Bumping version to ${VERSION}..."

# pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml

# main.py --version string
sed -i '' "s/version=\"%(prog)s .*\"/version=\"%(prog)s $VERSION\"/" story_points_analyzer/main.py

git add pyproject.toml story_points_analyzer/main.py
git commit -m "chore: release $TAG

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"

# ---------------------------------------------------------------------------
# Tag and push
# ---------------------------------------------------------------------------

echo "Tagging $TAG and pushing..."
git tag -a "$TAG" -m "$TAG"
git push origin main
git push origin "$TAG"

# ---------------------------------------------------------------------------
# Create GitHub release
# ---------------------------------------------------------------------------

echo "Creating GitHub release..."
gh release create "$TAG" \
  --title "$TAG" \
  --generate-notes

# ---------------------------------------------------------------------------
# Compute tarball SHA256
# ---------------------------------------------------------------------------

TARBALL_URL="https://github.com/$TOOL_REPO/archive/refs/tags/$TAG.tar.gz"

echo "Computing SHA256 for $TARBALL_URL..."
SHA256=$(curl -sL "$TARBALL_URL" | shasum -a 256 | awk '{print $1}')
echo "SHA256: $SHA256"

# ---------------------------------------------------------------------------
# Update Homebrew formula
# ---------------------------------------------------------------------------

echo "Updating formula..."

sed -i '' "s|url \"https://github.com/$TOOL_REPO/archive/refs/tags/v.*\.tar\.gz\"|url \"$TARBALL_URL\"|" "$FORMULA"
sed -i '' "\|url \"$TARBALL_URL\"|{n;s/sha256 \".*\"/sha256 \"$SHA256\"/;}" "$FORMULA"
sed -i '' "s/assert_match \"story-points-analyzer .*/assert_match \"story-points-analyzer $VERSION\", shell_output(\"#{bin}\/story-points-analyzer --version\")/" "$FORMULA"

# Bump the formula version comment if present (optional, best-effort)
cd "$TAP_DIR"
git add Formula/story-points-analyzer.rb
git commit -m "chore: update story-points-analyzer to $TAG

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push origin main
cd - > /dev/null

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "OK: Released $TAG"
echo "  GitHub release : https://github.com/$TOOL_REPO/releases/tag/$TAG"
echo "  Formula        : https://github.com/joaoparis/homebrew-scrum-tools/blob/main/Formula/story-points-analyzer.rb"
echo ""
echo "Users will get the update on their next: brew upgrade story-points-analyzer"
