#!/usr/bin/env bash
# setup.sh — set up the FreeCAD MCP server environment.
# Usage:
#   bash setup.sh
#
# After setup, start the server with:
#   DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib \
#     ./venv/bin/python server.py [--host 0.0.0.0] [--port 8000]
set -euo pipefail

FREECAD_PYTHON="/Applications/FreeCAD.app/Contents/Resources/bin/python"
VENV_DIR="$(dirname "$0")/venv"

# ── Sanity checks ──────────────────────────────────────────────────────────────
if [[ ! -x "$FREECAD_PYTHON" ]]; then
  echo "ERROR: FreeCAD not found at /Applications/FreeCAD.app"
  echo "       Download FreeCAD 1.0+ from https://www.freecad.org/downloads.php"
  exit 1
fi

echo "Using FreeCAD Python: $($FREECAD_PYTHON --version 2>&1)"

# ── Create venv from FreeCAD's Python ─────────────────────────────────────────
if [[ -d "$VENV_DIR" ]]; then
  echo "venv already exists at $VENV_DIR — skipping creation."
else
  echo "Creating venv at $VENV_DIR …"
  "$FREECAD_PYTHON" -m venv "$VENV_DIR"
fi

# ── Install Python dependencies ────────────────────────────────────────────────
echo "Installing requirements …"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$(dirname "$0")/requirements.txt"

echo ""
echo "Setup complete."
echo ""
echo "Start the server with:"
echo "  DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib \\"
echo "    ./venv/bin/python server.py"
