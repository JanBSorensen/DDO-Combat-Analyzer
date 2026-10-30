#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/_internal/startup_console.log"

show_error() {
  local msg="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="DDO Combat Analyzer" --text="$msg" 2>/dev/null || true
  else
    printf '%s\n' "$msg" >&2
  fi
}

if [[ ! -x "$PY" ]]; then
  show_error "DDO Combat Analyzer is not set up yet. Run 'SETUP DDO Combat Analyzer.sh' first."
  exit 2
fi

SESSION_TYPE="${XDG_SESSION_TYPE:-}"
if [[ -n "$SESSION_TYPE" && "$SESSION_TYPE" != "x11" ]]; then
  show_error "This Linux build currently targets X11. Log out and choose Cinnamon (X11), then try again."
  exit 3
fi

cd "$ROOT/_internal" || exit 4
exec "$PY" launch_gui.py >>"$LOG" 2>&1
