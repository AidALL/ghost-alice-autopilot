#!/usr/bin/env bash
# Self-contained installer for the autopilot-mode addon hooks. It wires the
# autopilot Stop + UserPromptSubmit hooks into the platform config via
# scripts/manage_hooks.py. It does NOT modify the Ghost-ALICE core installer.
#
# Run AFTER installing the skill with the core installer:
#   bash <ghost-alice>/install.sh --addon-source <this-repo> --platform claude
#   bash install-hooks.sh --platform claude
#
# Usage: bash install-hooks.sh [--platform claude|codex]   (default: both detected)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

resolve_py() {
  for py in "${GHOST_ALICE_PYTHON:-}" python3 python \
      /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 /bin/python3; do
    [ -n "$py" ] || continue
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
      if "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        echo "$py"; return 0
      fi
    fi
  done
  echo "autopilot-mode hooks require Python 3.11+" >&2; return 1
}

PY="$(resolve_py)"
platforms=()
if [ "${1:-}" = "--platform" ] && [ -n "${2:-}" ]; then
  platforms=("$2")
else
  [ -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" ] && platforms+=("claude")
  [ -d "${CODEX_HOME:-$HOME/.codex}" ] && platforms+=("codex")
fi
if [ ${#platforms[@]} -eq 0 ]; then
  echo "No Claude Code (~/.claude) or Codex (~/.codex) config dir detected." >&2
  exit 1
fi
for plat in "${platforms[@]}"; do
  "$PY" "$HERE/scripts/manage_hooks.py" install --platform "$plat"
done
echo "autopilot-mode hooks installed. They take effect in NEW sessions (hooks load at session start)."
