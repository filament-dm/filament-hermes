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
#   VIRTUAL_ENV          Hermes venv (default: auto-detected, see below)
#   HERMES_HOME          Hermes home (default: ~/.hermes)
#
set -euo pipefail

REPO="${FILAMENT_FCM_REPO:-git+https://github.com/filament-dm/filament-hermes.git}"
HERMES_HOME_DEFAULTED=0
[ -n "${HERMES_HOME:-}" ] || HERMES_HOME_DEFAULTED=1
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

[ -n "${CONNECT_TOKEN:-}" ] || err \
  "CONNECT_TOKEN is not set. Use the connect command shown in the Filament app."
export CONNECT_TOKEN

# --- Locate the Hermes venv --------------------------------------------------
# Hermes puts its venv in different places depending on the install layout
# (see scripts/install.sh and the Dockerfile in NousResearch/hermes-agent):
#
#   $HERMES_HOME/hermes-agent/venv    user-scoped installs (the default)
#   /usr/local/lib/hermes-agent/venv  root installs on Linux (FHS layout)
#   /opt/hermes/.venv                 Docker / cloud images (sealed read-only)
#   $HERMES_INSTALL_DIR/venv          installs made with an explicit --dir
#
# $VIRTUAL_ENV always wins when set, so users can point us anywhere.

is_venv() { [ -x "$1/bin/python" ]; }

VENV=""
if [ -n "${VIRTUAL_ENV:-}" ]; then
  is_venv "$VIRTUAL_ENV" || err \
    "VIRTUAL_ENV=$VIRTUAL_ENV doesn't look like a venv (no bin/python)."
  VENV="$VIRTUAL_ENV"
else
  CANDIDATES=(
    "$HERMES_HOME/hermes-agent/venv"
    /usr/local/lib/hermes-agent/venv
    /opt/hermes/.venv
  )
  if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    CANDIDATES=("$HERMES_INSTALL_DIR/venv" "${CANDIDATES[@]}")
  fi
  for CANDIDATE in "${CANDIDATES[@]}"; do
    if is_venv "$CANDIDATE"; then VENV="$CANDIDATE"; break; fi
  done
fi

# Last resort: follow the `hermes` launcher on PATH. Both the user-install
# shim and the Docker exec shim run the real venv entry point by absolute
# path, so that path tells us where the venv is.
if [ -z "$VENV" ]; then
  HERMES_CMD="$(command -v hermes 2>/dev/null || true)"
  if [ -n "$HERMES_CMD" ]; then
    RESOLVED="$(readlink -f "$HERMES_CMD" 2>/dev/null || echo "$HERMES_CMD")"
    SHIM_TARGET="$(grep -oE '/[^"[:space:]]+/bin/hermes' "$RESOLVED" 2>/dev/null | head -n1 || true)"
    for CANDIDATE in "${RESOLVED%/bin/hermes}" "${SHIM_TARGET%/bin/hermes}"; do
      if [ -n "$CANDIDATE" ] && is_venv "$CANDIDATE"; then VENV="$CANDIDATE"; break; fi
    done
  fi
fi

[ -n "$VENV" ] || err "Hermes venv not found — install/start Hermes Agent first. \
Checked \$VIRTUAL_ENV, $HERMES_HOME/hermes-agent/venv, /usr/local/lib/hermes-agent/venv, \
/opt/hermes/.venv, and the 'hermes' command on PATH. If your venv lives elsewhere, \
re-run with VIRTUAL_ENV=/path/to/venv."

# Docker/cloud images keep the data tree at /opt/data (the image sets
# HERMES_HOME=/opt/data itself, so this only matters under a stripped
# environment). $HOME/.hermes would be the wrong tree there — config.yaml
# and .env must land where the supervised gateway reads them.
if [ "$HERMES_HOME_DEFAULTED" = 1 ] && [ "$VENV" = /opt/hermes/.venv ] && [ -d /opt/data ]; then
  HERMES_HOME=/opt/data
fi

export VIRTUAL_ENV="$VENV"
export HERMES_HOME="$HERMES_HOME"
PY="$VENV/bin/python"

# uv ships with Hermes: $HERMES_HOME/bin/uv on user installs, /usr/local/bin/uv
# on Docker/cloud images. Fall back to one on PATH.
UV="$HERMES_HOME/bin/uv"
[ -x "$UV" ] || UV=/usr/local/bin/uv
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || err "uv not found — install Hermes Agent first (expected $HERMES_HOME/bin/uv)."

# --- Install -----------------------------------------------------------------
# Sealed images (Docker / cloud) mount the venv read-only; Hermes redirects
# runtime installs to a writable dir it puts on sys.path at startup
# (HERMES_LAZY_INSTALL_TARGET, e.g. /opt/data/lazy-packages). Install there
# so the gateway can import the plugin after restart.
#
# The writability test alone can't be trusted here: as root it passes even
# on the sealed image venv, and writes there land in the container's image
# layer — lost on recreate. So /opt/hermes/.venv is sealed by definition,
# and HERMES_DISABLE_LAZY_INSTALLS=1 (set by the image) counts as sealed
# too, for hand-built variants at other paths.
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"
SEALED=0
if [ "${HERMES_DISABLE_LAZY_INSTALLS:-}" = "1" ] || [ "$VENV" = /opt/hermes/.venv ] \
    || { [ -n "$SITE" ] && [ ! -w "$SITE" ]; }; then
  SEALED=1
fi

# The image sets HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages; default
# to that under a stripped environment. The supervised gateway activates the
# dir from its own (image) environment, so packages installed there are seen.
LAZY_TARGET="${HERMES_LAZY_INSTALL_TARGET:-}"
if [ -z "$LAZY_TARGET" ] && [ "$VENV" = /opt/hermes/.venv ] && [ -d /opt/data ]; then
  LAZY_TARGET=/opt/data/lazy-packages
fi
TARGET_ARGS=()
PYPATH_PREFIX=""
if [ "$SEALED" = 1 ] && [ -n "$LAZY_TARGET" ]; then
  mkdir -p "$LAZY_TARGET"
  info "Hermes venv at $VENV is sealed — installing hermes-filament-fcm into $LAZY_TARGET ..."
  TARGET_ARGS=(--target "$LAZY_TARGET")
  PYPATH_PREFIX="$LAZY_TARGET"
elif [ "$SEALED" = 1 ]; then
  err "Hermes venv at $VENV is sealed (read-only or lazy installs disabled) and HERMES_LAZY_INSTALL_TARGET is not set — nowhere to install."
else
  info "Installing hermes-filament-fcm into $VENV ..."
fi
"$UV" pip install ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} "$REPO"

# Make `hermes` resolvable so the wizard's gateway-restart step works, without
# shadowing an existing launcher (the Docker shim must stay first on PATH so
# root `docker exec` sessions keep dropping privileges). When PATH is stripped
# enough that even the shim is missing, put the shim dir ahead of $VENV/bin —
# the raw venv entry point run as root would litter $HERMES_HOME with
# root-owned files and break the supervised gateway.
if ! command -v hermes >/dev/null 2>&1; then
  HERMES_PATH_PREFIX="$HERMES_HOME/bin:$VENV/bin"
  if [ "$VENV" = /opt/hermes/.venv ] && [ -x /opt/hermes/bin/hermes ]; then
    HERMES_PATH_PREFIX="/opt/hermes/bin:$HERMES_PATH_PREFIX"
  fi
  export PATH="$HERMES_PATH_PREFIX:$PATH"
fi

info "Connecting to Filament ..."
run_setup() {
  if [ -n "$PYPATH_PREFIX" ]; then
    # Durable-target install: the sealed venv can't see the package, and the
    # console script's shebang points at the wrong interpreter. Run the module
    # with the venv Python and the target dir on PYTHONPATH.
    PYTHONPATH="$PYPATH_PREFIX${PYTHONPATH:+:$PYTHONPATH}" \
      "$PY" -m hermes_filament_fcm.setup_cli "$@"
  else
    # --active makes `uv run` use the activated $VIRTUAL_ENV (the Hermes venv
    # where we just installed the package) instead of discovering/creating
    # its own.
    "$UV" run --active filament-fcm-setup "$@"
  fi
}

# Re-attach the terminal so the setup wizard's prompts work even under
# 'curl | bash', where stdin is the download pipe rather than your keyboard.
if [ -t 1 ] && [ -r /dev/tty ]; then
  run_setup < /dev/tty
else
  run_setup
fi
