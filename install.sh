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
#   FILAMENT_FCM_REPO    clone the plugin from a different repo URL
#                        (default: https github main)
#   FILAMENT_FCM_REF     clone a specific branch/tag/commit (default: repo's
#                        default branch — used to test unreleased plugin changes)
#   VIRTUAL_ENV          Hermes venv (default: auto-detected, see below)
#   HERMES_HOME          Hermes home (default: ~/.hermes)
#
# This plugin installs as a Hermes *directory plugin*: its Python dependencies
# go into the Hermes venv, and the plugin code is git-cloned into
# $HERMES_HOME/plugins/filament-fcm so `hermes plugins list/update/enable` work.
# `hermes plugins update` refreshes the code only; a dependency bump (rare)
# means re-running this command, which the plugin's dep-check will prompt for.
set -euo pipefail

# Runtime Python dependencies are read from the cloned pyproject.toml below
# ([project.dependencies]) — the single source of truth — so a dependency added
# there is never silently missed by this installer.

# Where to clone the plugin from. FILAMENT_FCM_REPO accepts either a plain git
# URL or a pip-style "git+<url>[@<ref>]" requirement — the Filament app and some
# tooling set it in the pip form. Strip a leading git+, and (unless
# FILAMENT_FCM_REF is set) treat a trailing "@<ref>" as the branch/tag/commit —
# whether or not the URL carries the optional ".git" suffix.
_repo_spec="${FILAMENT_FCM_REPO:-https://github.com/filament-dm/filament-hermes.git}"
_repo_spec="${_repo_spec#git+}"
PLUGIN_REF="${FILAMENT_FCM_REF:-}"
# Only URLs with a scheme (https://, ssh://, ...) can carry a "@<ref>" suffix we
# split on; the "@" then reliably sits after "://host/path", not in a
# scp-style "git@host:owner/repo" address (which has no scheme and is left
# whole). The ref may itself contain "/" (e.g. a "user/branch" name), so split
# on the LAST "@".
case "$_repo_spec" in
  *://*@*)
    [ -n "$PLUGIN_REF" ] || PLUGIN_REF="${_repo_spec##*@}"
    PLUGIN_REPO_URL="${_repo_spec%@*}"
    ;;
  *)
    PLUGIN_REPO_URL="$_repo_spec"
    ;;
esac
HERMES_HOME_DEFAULTED=0
[ -n "${HERMES_HOME:-}" ] || HERMES_HOME_DEFAULTED=1
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
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

# --- Install dependencies ----------------------------------------------------
# Only the Python dependencies go into the venv here — NOT the plugin package
# (which is cloned as a directory plugin below). Sealed images (Docker / cloud)
# mount the venv read-only; Hermes redirects runtime installs to a writable dir
# it puts on sys.path at startup (HERMES_LAZY_INSTALL_TARGET, e.g.
# /opt/data/lazy-packages). Install deps there so the gateway can import them.
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
  info "Hermes venv at $VENV is sealed — installing dependencies into $LAZY_TARGET ..."
  TARGET_ARGS=(--target "$LAZY_TARGET")
  PYPATH_PREFIX="$LAZY_TARGET"
elif [ "$SEALED" = 1 ]; then
  err "Hermes venv at $VENV is sealed (read-only or lazy installs disabled) and HERMES_LAZY_INSTALL_TARGET is not set — nowhere to install."
else
  info "Installing dependencies into $VENV ..."
fi

# --- Install the plugin (as a Hermes directory plugin) -----------------------
# Clone the plugin into $HERMES_HOME/plugins/filament-fcm, where Hermes
# discovers it via its plugin.yaml + __init__.py. A real clone (not a copy)
# leaves a git remote, so `hermes plugins update filament-fcm` can `git pull`
# later — the whole point of installing this way.
#
# Clone into a temp dir first and only swap it into place once complete, so a
# failed clone/checkout never leaves the machine with the old plugin removed and
# nothing to replace it.
GIT="$(command -v git 2>/dev/null || true)"
[ -n "$GIT" ] || err "git not found — needed to install the plugin."
PLUGIN_DIR="$HERMES_HOME/plugins/filament-fcm"
mkdir -p "$HERMES_HOME/plugins"
CLONE_TMP="$(mktemp -d "$HERMES_HOME/plugins/.filament-fcm.XXXXXX")" \
  || err "could not create a temp dir under $HERMES_HOME/plugins."
cleanup_clone_tmp() { rm -rf "$CLONE_TMP" 2>/dev/null || true; }
trap cleanup_clone_tmp EXIT

info "Cloning plugin from $PLUGIN_REPO_URL${PLUGIN_REF:+ (ref: $PLUGIN_REF)} ..."
if [ -z "$PLUGIN_REF" ]; then
  "$GIT" clone --depth 1 "$PLUGIN_REPO_URL" "$CLONE_TMP" || err "git clone failed."
elif "$GIT" clone --depth 1 --branch "$PLUGIN_REF" "$PLUGIN_REPO_URL" "$CLONE_TMP" 2>/dev/null; then
  : # ref was a branch or tag
else
  # `git clone --branch` only accepts branch/tag names, so a commit SHA lands
  # here. Fetch the ref explicitly and check it out — GitHub serves reachable
  # commit SHAs, so this one path covers branch, tag, and commit uniformly.
  rm -rf "$CLONE_TMP" && mkdir -p "$CLONE_TMP"
  "$GIT" -C "$CLONE_TMP" init -q || err "git init failed."
  "$GIT" -C "$CLONE_TMP" remote add origin "$PLUGIN_REPO_URL" || err "git remote add failed."
  "$GIT" -C "$CLONE_TMP" fetch --depth 1 origin "$PLUGIN_REF" \
    || err "could not fetch ref '$PLUGIN_REF' from $PLUGIN_REPO_URL (branch, tag, or commit)."
  "$GIT" -C "$CLONE_TMP" checkout -q --detach FETCH_HEAD || err "git checkout of '$PLUGIN_REF' failed."
fi

# Install the plugin's declared runtime dependencies, read straight from the
# cloned pyproject.toml ([project.dependencies]) so this installer never drifts
# from what the code needs. Done before the swap below, so a failed dep install
# leaves any previously-working plugin in place.
#
# --upgrade so re-running the install command is the way to pull dependency
# updates: each dep is brought to the newest version satisfying its pyproject
# constraint (a fresh install just installs; a re-run upgrades). `hermes plugins
# update` only pulls code, so this installer is the dependency-refresh path.
info "Installing/upgrading plugin dependencies ..."
FCM_DEPS=()
while IFS= read -r _dep; do
  [ -n "$_dep" ] && FCM_DEPS+=("$_dep")
done < <("$PY" - "$CLONE_TMP/pyproject.toml" <<'PYEOF'
import sys

# Parse pyproject.toml properly so requirement extras (e.g. "httpx[socks]") and
# other bracket content inside requirement strings don't confuse extraction.
# tomllib is stdlib on 3.11+; fall back to tomli, then to nothing (the bash
# caller substitutes a safe built-in dependency set when this prints empty).
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

deps = []
if tomllib is not None:
    try:
        with open(sys.argv[1], "rb") as f:
            deps = tomllib.load(f).get("project", {}).get("dependencies", []) or []
    except Exception:
        deps = []
print("\n".join(d for d in deps if isinstance(d, str)))
PYEOF
)
if [ "${#FCM_DEPS[@]}" -eq 0 ]; then
  # A pyproject parse hiccup must never leave the plugin without its hard
  # dependency — fall back to the essential set.
  warn "could not read dependencies from pyproject.toml; using built-in defaults."
  FCM_DEPS=("firebase-messaging>=0.4.5,<1" "httpx>=0.24" "structlog>=25.5.0,<26")
fi
"$UV" pip install --upgrade ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} "${FCM_DEPS[@]}"

# Write the directory-plugin entry point. Hermes loads $PLUGIN_DIR/__init__.py
# and calls register(); the real code lives in the nested hermes_filament_fcm
# package, so this shim just re-exports it. It is generated here (not committed
# to the repo) so the repo root stays a normal project tree — a committed root
# __init__.py would make the repo a package and break pytest collection. It is
# untracked in the clone, so `hermes plugins update` (git pull) leaves it in
# place.
cat > "$CLONE_TMP/__init__.py" <<'PYSHIM'
"""Directory-plugin entry point (generated by install.sh).

Re-exports register() from the nested hermes_filament_fcm package so Hermes'
directory-plugin loader can find it.
"""

from .hermes_filament_fcm import register

__all__ = ["register"]
PYSHIM

# The new plugin is complete — swap it into place (replacing any prior install),
# then migrate off an earlier pip/entry-point install so it can't shadow this
# directory plugin (entry points win the loader's dedup, so leaving one would
# make `hermes plugins update` refresh code that never runs). Both steps happen
# only after a successful clone, so a failure above leaves the old plugin intact.
if [ -d "$PLUGIN_DIR" ]; then
  info "Replacing existing plugin at $PLUGIN_DIR ..."
  rm -rf "$PLUGIN_DIR"
fi
mv "$CLONE_TMP" "$PLUGIN_DIR" || err "could not move the plugin into $PLUGIN_DIR."
trap - EXIT

"$UV" pip uninstall hermes-filament-fcm >/dev/null 2>&1 || true
if [ -n "$LAZY_TARGET" ] && [ -d "$LAZY_TARGET" ]; then
  rm -rf "$LAZY_TARGET"/hermes_filament_fcm "$LAZY_TARGET"/hermes_filament_fcm-*.dist-info 2>/dev/null || true
fi

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
# Run the setup wizard with the venv Python and the plugin dir (plus any durable
# dep target) on PYTHONPATH, so `hermes_filament_fcm` imports from the clone.
# The package is not pip-installed, so there is no console script to run.
run_setup() {
  PYTHONPATH="$PLUGIN_DIR${PYPATH_PREFIX:+:$PYPATH_PREFIX}${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m hermes_filament_fcm.setup_cli "$@"
}

# Re-attach the terminal so the setup wizard's prompts work even under
# 'curl | bash', where stdin is the download pipe rather than your keyboard.
if [ -t 1 ] && [ -r /dev/tty ]; then
  run_setup < /dev/tty
else
  run_setup
fi

# --- Force a supervised restart ------------------------------------------
# On Docker/cloud images the gateway runs under an s6 supervisor
# (s6-supervise gateway-<profile>). The setup wizard's `hermes gateway
# restart` can't reliably cycle it there: the wizard runs inside the
# gateway's own process tree, so it can neither SIGTERM itself cleanly nor
# reach the supervisor — s6 just keeps (or respawns) the OLD process, which
# started before the plugin was installed and never loads the Filament
# adapter. Ask the supervisor directly to bounce the service so the new
# process picks up the plugin and the saved .env.
#
# s6-overlay keeps its binaries in /command, which is rarely on PATH.
# -t sends SIGTERM and the supervisor respawns the service — the same
# action as upstream's S6ServiceManager.restart. No-op outside s6 images.
S6_SVC="$(command -v s6-svc 2>/dev/null || true)"
if [ -z "$S6_SVC" ] && [ -x /command/s6-svc ]; then
  S6_SVC=/command/s6-svc
fi

# Restart a live service slot. Returns false only when no live slot exists
# (a control FIFO is absent) — that's what gates the caller's naming-mismatch
# fallback. An s6-svc failure still counts as "slot found": falling back to
# other profiles' slots can't fix it (same s6-svc, same permissions) and
# would only bounce gateways the wizard never touched — so warn instead.
restart_slot() {
  [ -d "$1" ] && [ -p "$1/supervise/control" ] || return 1
  info "Restarting supervised gateway ($(basename "$1")) so the plugin loads ..."
  "$S6_SVC" -t "$1" \
    || warn "could not restart $(basename "$1") — restart it manually: $S6_SVC -t $1"
}

if [ -n "$S6_SVC" ]; then
  # Each profile is an independent HERMES_HOME (the default profile at the
  # root, named ones under <root>/profiles/<name>), and the wizard only
  # configured this one — leave other profiles' gateways alone.
  if [ "$(basename "$(dirname "$HERMES_HOME")")" = profiles ]; then
    HERMES_PROFILE="$(basename "$HERMES_HOME")"
  else
    HERMES_PROFILE=default
  fi

  RESTARTED=0
  for SVCDIR in "/run/service/gateway-$HERMES_PROFILE" "/run/service/hermes-gateway-$HERMES_PROFILE"; do
    if restart_slot "$SVCDIR"; then RESTARTED=1; fi
  done
  if [ "$RESTARTED" = 0 ]; then
    # This provider names its slots differently — restart every live
    # gateway rather than leave the plugin unloaded.
    for SVCDIR in /run/service/gateway-* /run/service/hermes-gateway-*; do
      restart_slot "$SVCDIR" || true
    done
  fi
fi
