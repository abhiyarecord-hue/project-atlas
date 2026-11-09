#!/usr/bin/env sh
# project-atlas installer for macOS / Linux / Git Bash
# Usage:  sh install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/skills/project-atlas"
PY=""
for c in python3 python py; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
[ -z "$PY" ] && { echo "Python 3 not found. Install it first."; exit 1; }
mkdir -p "$DEST"
cp -R "$HERE/project-atlas/." "$DEST/"
echo "Skill copied to $DEST"
"$PY" "$DEST/atlas.py" install-hook --root "$HOME"
echo
echo "Done. Open Claude Code in any project and it will be indexed automatically."
