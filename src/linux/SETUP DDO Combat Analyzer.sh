#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
REQ="$ROOT/_internal/requirements.txt"
START="$ROOT/START_DDO_COMBAT_ANALYZER.sh"

echo "DDO Combat Analyzer - Linux setup"
echo "Target: Linux Mint 22.3 / Cinnamon X11"
echo

SESSION_TYPE="${XDG_SESSION_TYPE:-}"
if [[ -n "$SESSION_TYPE" && "$SESSION_TYPE" != "x11" ]]; then
  echo "WARNING: current session type is '$SESSION_TYPE'."
  echo "This build targets X11. Log out and choose Cinnamon (X11) before using the analyzer."
  echo
fi

if command -v apt-get >/dev/null 2>&1; then
  echo "Installing Linux system dependencies..."
  sudo apt-get update
  sudo apt-get install -y \
    python3 python3-venv python3-pip python3-tk \
    ffmpeg xdotool wmctrl libgl1 libglib2.0-0
else
  echo "ERROR: apt-get was not found. This setup script is intended for Linux Mint/Ubuntu."
  exit 2
fi

echo
echo "Creating Python virtual environment..."
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install -r "$REQ"

echo
echo "Checking Python modules..."
"$VENV/bin/python" - <<'PY'
mods = [
    ("mss", "mss"),
    ("OpenCV", "cv2"),
    ("NumPy", "numpy"),
    ("Indra OCR", "indra_ocr"),
    ("python-xlib", "Xlib"),
]
failed = []
for label, name in mods:
    try:
        __import__(name)
        print(f"  OK  {label}")
    except Exception as exc:
        failed.append((label, str(exc)))
        print(f"  FAIL {label}: {exc}")
if failed:
    raise SystemExit(3)
PY

chmod +x "$START"

# Create one Cinnamon/X11 desktop launcher. The actual START script deliberately
# has no spaces so .desktop Exec parsing remains simple and reliable.
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/ddo-combat-analyzer.desktop"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=DDO Combat Analyzer
Comment=Live DPS and post-combat OCR analysis for DDO
Exec="$START"
Path=$ROOT
Terminal=false
Categories=Game;Utility;
Icon=utilities-system-monitor
StartupNotify=true
EOF
chmod +x "$DESKTOP_FILE"

echo
echo "Setup complete."
echo "Start with:"
echo "  $START"
echo
echo "A menu launcher named 'DDO Combat Analyzer' was also created."
