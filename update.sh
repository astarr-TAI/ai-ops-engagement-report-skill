#!/usr/bin/env bash
# Pull latest skill updates. Called hourly by launchd; safe to run manually.
set -euo pipefail

REPO_DIR="$HOME/.treasure-work/td-work-skills"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "ERROR: Repo not found at $REPO_DIR. Run install.sh first."
  exit 1
fi

cd "$REPO_DIR"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pulling latest skills..."
git pull --ff-only
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done."
