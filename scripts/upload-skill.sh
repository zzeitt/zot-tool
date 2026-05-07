#!/bin/sh
# Push zot-tool to GitHub
# Remote: git@github.com:zzeitt/zot-tool.git
set -e

# Use relative path: this script is in scripts/, repo root is one level up
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

if [ -z "$(git status --short)" ]; then
    echo "Nothing to commit.")
    exit 0
fi

echo "📤 Pushing to GitHub..."
git add -A
git commit -m "Update $(date '+%Y-%m-%d %H:%M')" || echo "Nothing to commit"
git push
echo "✅ Done."
