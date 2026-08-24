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
#   FILAMENT_PROFILE     install into this named Hermes profile instead of the
#                        one HERMES_HOME points at, creating it if missing.
#                        Each profile is an independent HERMES_HOME with its
#                        own gateway and FCM identity — how one Hermes
#                        instance hosts several Filament agents.
#
# This plugin installs as a Hermes *directory plugin*: its Python dependencies
# go into the Hermes venv, and the plugin code is git-cloned into
# $HERMES_HOME/plugins/filament so `hermes plugins list/update/enable` work.
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

# Make `hermes` resolvable for everything below (profile creation, the wizard's
# gateway-restart step), without shadowing an existing launcher (the Docker
# shim must stay first on PATH so root `docker exec` sessions keep dropping
# privileges). When PATH is stripped enough that even the shim is missing, put
# the shim dir ahead of $VENV/bin — the raw venv entry point run as root would
# litter $HERMES_HOME with root-owned files and break the supervised gateway.
if ! command -v hermes >/dev/null 2>&1; then
  HERMES_PATH_PREFIX="$HERMES_HOME/bin:$VENV/bin"
  if [ "$VENV" = /opt/hermes/.venv ] && [ -x /opt/hermes/bin/hermes ]; then
    HERMES_PATH_PREFIX="/opt/hermes/bin:$HERMES_PATH_PREFIX"
  fi
  export PATH="$HERMES_PATH_PREFIX:$PATH"
fi

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

# A writable site-packages is not enough: a venv can be writable at the top
# while individual package directories inside it are not. Agent37's
# agent37-hermes image is built that way — ~22 packages ship their
# __pycache__ as root:root 0755 inside a tree the runtime user owns
# otherwise. Upgrading such a package means *removing* its old __pycache__,
# which fails EACCES and takes the whole install down. Probe one glob level
# for a __pycache__ we can't write, and treat that venv as sealed too.
# POSIX-only ([ -w ] rather than find -writable) so this holds on macOS.
if [ "$SEALED" = 0 ] && [ -n "$SITE" ]; then
  for _pycache in "$SITE"/*/__pycache__; do
    [ -d "$_pycache" ] || continue
    if [ ! -w "$_pycache" ]; then
      info "Hermes venv at $VENV has unwritable package dirs (e.g. $_pycache)."
      SEALED=1
      break
    fi
  done
fi

# The image sets HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages; default
# to that under a stripped environment. The supervised gateway activates the
# dir from its own (image) environment, so packages installed there are seen.
LAZY_TARGET="${HERMES_LAZY_INSTALL_TARGET:-}"
if [ -z "$LAZY_TARGET" ] && [ "$VENV" = /opt/hermes/.venv ] && [ -d /opt/data ]; then
  LAZY_TARGET=/opt/data/lazy-packages
fi
# Images that seal their venv without advertising a lazy target (agent37's,
# via the probe above) leave us to pick one. $HERMES_HOME is the durable,
# user-owned tree the gateway already reads its config and .env from.
if [ -z "$LAZY_TARGET" ] && [ "$SEALED" = 1 ]; then
  LAZY_TARGET="$HERMES_HOME/lazy-packages"
fi
TARGET_ARGS=()
PYPATH_PREFIX=""
if [ "$SEALED" = 1 ] && [ -n "$LAZY_TARGET" ]; then
  mkdir -p "$LAZY_TARGET"
  info "Hermes venv at $VENV is sealed — installing dependencies into $LAZY_TARGET ..."
  TARGET_ARGS=(--target "$LAZY_TARGET")
  PYPATH_PREFIX="$LAZY_TARGET"
  # PYPATH_PREFIX only reaches the setup wizard we run below (via PYTHONPATH).
  # The *gateway* starts from its own environment, so unless the image
  # activates the dir itself — which the ones setting HERMES_LAZY_INSTALL_TARGET
  # do, and the ones we guessed a target for do not — it starts without our
  # dependencies and loads no messaging platform. A .pth in site-packages
  # covers that: it needs site-packages writable, which is exactly the case
  # this branch now also serves (writable venv, unwritable packages).
  #
  # An "import"-prefixed .pth line is executed, so this *prepends* — plain
  # path lines are appended, which would let a stale copy of a dependency
  # already in the venv win over the version we just installed.
  if [ -w "$SITE" ]; then
    if printf 'import sys; sys.path.insert(0, %s)\n' "\"$LAZY_TARGET\"" \
        > "$SITE/zzz-filament-fcm-lazy-packages.pth" 2>/dev/null; then
      info "Put $LAZY_TARGET on the gateway's import path."
    else
      warn "could not write $SITE/zzz-filament-fcm-lazy-packages.pth — the \
gateway may not see the dependencies; set HERMES_LAZY_INSTALL_TARGET to a dir \
it already activates."
    fi
  fi
elif [ "$SEALED" = 1 ]; then
  err "Hermes venv at $VENV is sealed (read-only or lazy installs disabled) and HERMES_LAZY_INSTALL_TARGET is not set — nowhere to install."
else
  info "Installing dependencies into $VENV ..."
fi

# --- Optional: install into a named Hermes profile ----------------------------
# FILAMENT_PROFILE targets a named profile (creating it if missing) instead of
# the profile HERMES_HOME already points at. Each profile is an independent
# HERMES_HOME under <root>/profiles/<name> — own config.yaml, .env, gateway,
# and (via the plugin's HERMES_HOME-derived state dir) its own FCM identity —
# which is how one Hermes instance hosts several Filament agents.
#
# Everything below (plugin clone into $HERMES_HOME/plugins, setup wizard, s6
# restart) keys off HERMES_HOME, so re-pointing it here is the whole
# mechanism. Deliberately AFTER the lazy-target computation above: venv deps
# (and the lazy-target .pth on sealed images) are shared machine-wide, and a
# per-profile lazy dir would clobber the previous profile's .pth on every
# attach.
if [ -n "${FILAMENT_PROFILE:-}" ] && [ "$FILAMENT_PROFILE" != default ]; then
  case "$FILAMENT_PROFILE" in
    [!a-z0-9]* | *[!a-z0-9_-]*)
      err "FILAMENT_PROFILE must match [a-z0-9][a-z0-9_-]* (got '$FILAMENT_PROFILE')."
      ;;
  esac
  if [ ! -d "$HERMES_HOME/profiles/$FILAMENT_PROFILE" ]; then
    command -v hermes >/dev/null 2>&1 \
      || err "hermes CLI not found — needed to create profile '$FILAMENT_PROFILE'."
    info "Creating Hermes profile '$FILAMENT_PROFILE' ..."
    # --clone: the new agent starts as a copy of the instance's DEFAULT
    # profile. Under the Filament-provisioned flow the default profile is
    # never itself a connected agent — it is the instance's baseline (the
    # image's managed provisioning: metered provider, Composio/Brave MCP
    # surfaces, the managed plugins that teach the agent to use them, plus
    # any operator customization) — so cloning inherits instance-level
    # defaults, not another agent's identity. The setup step below
    # overwrites the cloned Filament identity slots with this agent's own.
    # --no-alias: no shell wrapper script; this profile is driven by its
    # supervised gateway, not interactively. On s6 images, creation also
    # registers the profile's gateway-<name> service, which the restart at
    # the end of this script then bounces.
    hermes profile create "$FILAMENT_PROFILE" --no-alias --clone \
      || err "could not create Hermes profile '$FILAMENT_PROFILE'."
  fi
  ROOT_HERMES_HOME="$HERMES_HOME"
  HERMES_HOME="$HERMES_HOME/profiles/$FILAMENT_PROFILE"
  export HERMES_HOME
  info "Installing into Hermes profile '$FILAMENT_PROFILE' ($HERMES_HOME)."
fi

# --- Install the plugin (as a Hermes directory plugin) -----------------------
# Clone the plugin into $HERMES_HOME/plugins/$PLUGIN_ID, where Hermes discovers
# it via its plugin.yaml + __init__.py. A real clone (not a copy) leaves a git
# remote, so `hermes plugins update $PLUGIN_ID` can `git pull` later — the whole
# point of installing this way.
#
# Clone into a temp dir first and only swap it into place once complete, so a
# failed clone/checkout never leaves the machine with the old plugin removed and
# nothing to replace it.
GIT="$(command -v git 2>/dev/null || true)"
[ -n "$GIT" ] || err "git not found — needed to install the plugin."
# Keep in sync with `name` in plugin.yaml and PLUGIN_ID in setup_cli.py.
PLUGIN_ID=filament
LEGACY_PLUGIN_ID=filament-fcm
PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_ID"
LEGACY_PLUGIN_DIR="$HERMES_HOME/plugins/$LEGACY_PLUGIN_ID"
mkdir -p "$HERMES_HOME/plugins"
CLONE_TMP="$(mktemp -d "$HERMES_HOME/plugins/.$PLUGIN_ID.XXXXXX")" \
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

# The entry point is committed as of 0.8.0 and no longer generated here, so a ref
# from before that has none. Only FILAMENT_FCM_REF reaches this. Stop rather than
# install a tree the loader cannot enter, which would surface much later as "No
# messaging platforms enabled".
[ -f "$CLONE_TMP/__init__.py" ] || err \
  "ref '${PLUGIN_REF:-default}' has no plugin entry point (__init__.py), so it \
predates 0.8.0. Install an older ref with that ref's own installer instead: \
curl -fsSL https://raw.githubusercontent.com/filament-dm/filament-hermes/\
${PLUGIN_REF:-main}/install.sh | CONNECT_TOKEN=... bash"

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
  #
  # firebase-messaging must stay the fork here too. Installing the stock package
  # would put it ahead of the vendored fork on sys.path, and the agent would
  # connect, look healthy and never receive a push. Keep the ref in step with
  # pyproject.toml and scripts/vendor-deps.sh.
  warn "could not read dependencies from pyproject.toml; using built-in defaults."
  FCM_DEPS=(
    "firebase-messaging @ git+https://github.com/filament-dm/firebase-messaging.git@filament/integration"
    "httpx>=0.24"
    "structlog>=25.5.0,<26"
  )
fi
"$UV" pip install --upgrade ${TARGET_ARGS[@]+"${TARGET_ARGS[@]}"} "${FCM_DEPS[@]}"

# The directory-plugin entry point ($PLUGIN_DIR/__init__.py, which Hermes loads
# to call register()) is committed to the repo, so the clone already has it —
# this script used to generate it. Do not write one here: the committed shim
# also puts the plugin's vendored dependencies on sys.path, and overwriting it
# with a bare re-export would break the plugin on any host where those vendored
# copies are what satisfy the import.

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

# An install made under the old plugin id is retired by the setup step below,
# not here. It rewrites plugins.enabled onto the new id and then removes the old
# directory, in that order — so if setup never gets that far, config still names
# the old id and the old directory is still there to serve it. Removing the
# directory here instead would leave a failed setup with nothing loadable: new
# tree installed but disabled, old tree gone.
#
# That removal also clears the stale entry point an older install left behind:
# this script used to generate $LEGACY_PLUGIN_DIR/__init__.py, leaving it
# untracked, and `hermes plugins update` is `git pull --ff-only`, which refuses to
# overwrite an untracked file now that the file is committed. Nobody has to delete
# it by hand.

"$UV" pip uninstall hermes-filament-fcm >/dev/null 2>&1 || true
if [ -n "$LAZY_TARGET" ] && [ -d "$LAZY_TARGET" ]; then
  rm -rf "$LAZY_TARGET"/hermes_filament_fcm "$LAZY_TARGET"/hermes_filament_fcm-*.dist-info 2>/dev/null || true
fi

info "Connecting to Filament ..."
# Run the setup wizard with the venv Python and the plugin dir (plus any durable
# dep target) on PYTHONPATH, so `hermes_filament_fcm` imports from the clone.
# The package is not pip-installed, so there is no console script to run.
run_setup() {
  # In the profile (hosted) flow this script owns the gateway restart — an
  # s6 bounce or the direct spawn below — so the wizard's own restart would
  # only spend two extra CLI startups getting replaced moments later.
  PYTHONPATH="$PLUGIN_DIR${PYPATH_PREFIX:+:$PYPATH_PREFIX}${PYTHONPATH:+:$PYTHONPATH}" \
    FILAMENT_SETUP_SKIP_RESTART="${FILAMENT_PROFILE:+1}" \
    "$PY" -m hermes_filament_fcm.setup_cli "$@"
}

# Re-attach the terminal so the setup wizard's prompts work even when this
# script is piped from curl straight into bash, where stdin is the download
# pipe rather than your keyboard.
if [ -t 1 ] && [ -r /dev/tty ]; then
  run_setup < /dev/tty
else
  run_setup
fi

# For a cloned profile, repair the config after setup: enabling the plugin
# rewrites config.yaml through Hermes's config layer, which has been seen to
# drop cloned keys it doesn't own (e.g. custom_providers). Merge back any
# root-config key the profile lost and union plugins.enabled with the root's
# (the managed agent37-* plugins carry the guidance for the instance's
# integrations). No-op when the rewrite preserved everything.
if [ -n "${FILAMENT_PROFILE:-}" ] && [ -n "${ROOT_HERMES_HOME:-}" ]; then
  "$PY" - "$ROOT_HERMES_HOME" "$HERMES_HOME" <<'PYEOF' \
    || warn "could not merge the default profile's config into '$FILAMENT_PROFILE' — \
its managed integrations may be missing; compare config.yaml against the root profile's."
import sys

import yaml

root, prof = sys.argv[1], sys.argv[2]
try:
    with open(f"{root}/config.yaml") as f:
        root_cfg = yaml.safe_load(f) or {}
except OSError:
    root_cfg = {}
path = f"{prof}/config.yaml"
try:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
except OSError:
    cfg = {}
changed = False
# Per-profile state the clone-repair must never overwrite from the root.
for key in root_cfg:
    if key in ("plugins", "onboarding"):
        continue
    if key not in cfg:
        cfg[key] = root_cfg[key]
        changed = True
root_enabled = (root_cfg.get("plugins") or {}).get("enabled") or []
plugins = cfg.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
for name in root_enabled:
    if name not in enabled:
        enabled.append(name)
        changed = True
if changed:
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print("merged root config keys the setup rewrite had dropped")
PYEOF
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
  # -u first: the slot a fresh `hermes profile create` registers is down
  # until told to start, and -t alone is a no-op on a down service. On an
  # already-running slot -u is the no-op and -t does the restart.
  "$S6_SVC" -u "$1" 2>/dev/null || true
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

# --- Start the profile gateway (non-s6 images) --------------------------------
# The wizard's restart was skipped above (FILAMENT_SETUP_SKIP_RESTART): with
# no supervisor, `hermes gateway restart` is two CLI startups (restart, then
# run) where one will do. Spawn the gateway directly, detached from this
# session; --replace hands over cleanly if one is somehow already up.
if [ -z "$S6_SVC" ] && [ -n "${FILAMENT_PROFILE:-}" ]; then
  SETSID="$(command -v setsid 2>/dev/null || true)"
  info "Starting the gateway ..."
  # shellcheck disable=SC2086  # $SETSID intentionally word-splits away when absent
  $SETSID nohup hermes gateway run --replace \
    > "$HERMES_HOME/logs/gateway-detached.log" 2>&1 < /dev/null &
  disown 2>/dev/null || true
fi

# --- Keep profile gateways alive across restarts (non-s6 images) -------------
# Without s6 there is no supervisor for a named profile's gateway: the setup
# wizard starts it as a plain background process, which dies with the
# container (restart, sleep/wake, update) while the image's entrypoint only
# revives the DEFAULT profile's gateway. Agent37's image runs
# ~/.agent37/hooks/post-restart.sh on every container start for exactly this —
# install one managed block there that revives the gateway of every Hermes
# profile connected to Filament. The loop covers all such profiles, so one
# block serves every agent ever attached; other non-s6 hosts (no hooks dir)
# are skipped — their operators own gateway supervision.
AGENT37_HOOK="${AGENT37_HOOKS_DIR:-$HOME/.agent37/hooks}/post-restart.sh"
if [ -z "$S6_SVC" ] && [ -n "${FILAMENT_PROFILE:-}" ] \
    && [ -d "$(dirname "$AGENT37_HOOK")" ] \
    && ! grep -q "filament-hermes profile gateways" "$AGENT37_HOOK" 2>/dev/null; then
  cat >> "$AGENT37_HOOK" <<'HOOKEOF'

# >>> filament-hermes profile gateways (managed block; do not edit)
# Revive the gateway of every Hermes profile connected to Filament: their
# background gateways die with the container, and the entrypoint only
# supervises the default profile's.
command -v hermes >/dev/null 2>&1 \
  || PATH="/usr/local/bin:/usr/local/lib/hermes/hermes-agent/venv/bin:$PATH"
for _fil_plugin_dir in "$HOME"/.hermes/profiles/*/plugins/filament; do
  [ -d "$_fil_plugin_dir" ] || continue
  HERMES_HOME="$(dirname "$(dirname "$_fil_plugin_dir")")" \
    hermes gateway restart >/dev/null 2>&1 || true
done
# <<< filament-hermes profile gateways
HOOKEOF
  info "Installed post-restart hook: profile gateways revive on container restarts."
fi
