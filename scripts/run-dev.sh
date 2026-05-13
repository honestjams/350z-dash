#!/usr/bin/env bash
# =====================================================================
# 350Z Dash — laptop dev mode
#
# Creates a venv if needed, installs deps, and runs the server in
# simulator mode (no cable required). Open http://localhost:8080 in
# your browser.
# =====================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "${SCRIPT_DIR}")"

# First-time setup
if [[ ! -d .venv ]]; then
  echo "==> Creating .venv (first-time setup)..."
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Pass through any extra args, but default to --simulator so dev mode
# doesn't poke at random serial ports on your laptop.
if [[ $# -eq 0 ]]; then
  set -- --simulator
fi

echo
echo "==> Starting dash server..."
echo "==> Open http://localhost:8080 in your browser."
echo "==> Ctrl+C to stop."
echo
python -m server.main "$@"
