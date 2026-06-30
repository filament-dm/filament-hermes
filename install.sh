#!/usr/bin/env bash
#
# Install the Filament FCM gateway plugin into an existing Hermes Agent and
# connect it, using the agent token from the Filament app.
#
# Run the one-liner the Filament app gives you:
#
#   curl -fsSL https://raw.githubusercontent.com/filament-dm/filament-hermes/main/install.sh | CONNECT_TOKEN=fmcp_... bash
#
# (Equivalently, to keep your terminal attached for the prompts:
#   CONNECT_TOKEN=fmcp_... bash <(curl -fsSL https://raw.githubusercontent.com/filament-dm/filament-hermes/main/install.sh)  )
#
# Optional environment overrides:
#   FILAMENT_MCP_URL     point at staging/local instead of production
#   FILAMENT_FCM_REPO    install from a different repo/branch (default: https main)
#   VIRTUAL_ENV          Hermes venv (default: ~/.hermes/hermes-agent/venv)
#   HERMES_HOME          Hermes home (default: ~/.hermes)
#
set -euo pipefail

REPO="${FILAMENT_FCM_REPO:-git+https://github.com/filament-dm/filament-hermes.git}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV="${VIRTUAL_ENV:-$HERMES_HOME/hermes-agent/venv}"

err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -n "${CONNECT_TOKEN:-}" ] || err \
  "CONNECT_TOKEN is not set. Use the connect command shown in the Filament app."
export CONNECT_TOKEN

# uv ships with Hermes; fall back to one on PATH.
UV="$HERMES_HOME/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || err "uv not found — install Hermes Agent first (expected $HERMES_HOME/bin/uv)."

[ -d "$VENV" ] || err "Hermes venv not found at $VENV — install/start Hermes Agent first."
export VIRTUAL_ENV="$VENV"
export HERMES_HOME="$HERMES_HOME"

info "Installing hermes-filament-fcm into $VENV ..."
"$UV" pip install "$REPO"

info "Connecting to Filament ..."
# Re-attach the terminal so the setup wizard's prompts work even under
# 'curl | bash', where stdin is the download pipe rather than your keyboard.
# --active makes `uv run` use the activated $VIRTUAL_ENV (the Hermes venv where
# we just installed the package) instead of discovering/creating its own.
if [ -t 1 ] && [ -r /dev/tty ]; then
  "$UV" run --active filament-fcm-setup < /dev/tty
else
  "$UV" run --active filament-fcm-setup
fi
