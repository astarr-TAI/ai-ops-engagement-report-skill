#!/usr/bin/env bash
# One-time bootstrap: clone + symlink + schedule auto-pull
set -euo pipefail

REPO_DIR="$HOME/.treasure-work/td-work-skills"
SKILLS_DIR="$HOME/.treasure-work/.claude/skills"
PLIST="$HOME/Library/LaunchAgents/ai.treasure.td-work-skills-update.plist"
REPO_URL="${1:-}"

if [[ -z "$REPO_URL" ]]; then
  echo "Usage: install.sh <git-repo-url>"
  echo "Example: install.sh https://github.com/your-org/td-work-skills.git"
  exit 1
fi

# 1. Clone
if [[ -d "$REPO_DIR/.git" ]]; then
  echo "Repo already cloned at $REPO_DIR — skipping clone."
else
  echo "Cloning $REPO_URL → $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

# 2. Symlink each skill directory
mkdir -p "$SKILLS_DIR"
for skill_dir in "$REPO_DIR"/*/; do
  skill_name="$(basename "$skill_dir")"
  # Skip non-skill entries (no SKILL.md)
  [[ -f "$skill_dir/SKILL.md" ]] || continue
  target="$SKILLS_DIR/$skill_name"
  if [[ -L "$target" ]]; then
    echo "Symlink already exists: $target — skipping."
  elif [[ -d "$target" ]]; then
    echo "WARNING: $target is a real directory (not a symlink). Remove it manually and re-run."
  else
    ln -s "$skill_dir" "$target"
    echo "Linked: $target → $skill_dir"
  fi
done

# 3. Install launchd plist for hourly auto-pull
mkdir -p "$HOME/.treasure-work/logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.treasure.td-work-skills-update</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO_DIR}/update.sh</string>
  </array>
  <key>StartInterval</key>
  <integer>18000</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${HOME}/.treasure-work/logs/td-work-skills-update.log</string>
  <key>StandardErrorPath</key>
  <string>${HOME}/.treasure-work/logs/td-work-skills-update.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "launchd job installed — skills will auto-update every hour."
echo ""
echo "Done. Skills linked:"
for skill_dir in "$REPO_DIR"/*/; do
  skill_name="$(basename "$skill_dir")"
  [[ -f "$skill_dir/SKILL.md" ]] && echo "  • $skill_name"
done

# Post-link setup: install vendored deps for skills that need them
echo ""
echo "Installing vendored dependencies..."
ZOOM_SKILL="$SKILLS_DIR/auto-pull-zoom-transcript"
if [[ -L "$ZOOM_SKILL" || -d "$ZOOM_SKILL" ]]; then
  vendor_dir="$REPO_DIR/auto-pull-zoom-transcript/vendor"
  if [[ ! -d "$vendor_dir" ]]; then
    echo "  Installing curl-cffi for auto-pull-zoom-transcript..."
    pip3 install curl-cffi --target "$vendor_dir" -q && echo "  Done." || echo "  WARNING: pip3 install failed — install curl-cffi manually into $vendor_dir"
  else
    echo "  auto-pull-zoom-transcript vendor deps already present."
  fi
fi
