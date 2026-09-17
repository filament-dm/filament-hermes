#!/usr/bin/env bash
#
# Run install.sh's dependency-read block on its own, under whichever bash
# invokes this script, and print the dependencies it extracted.
#
# It exists because `bash -n install.sh` provably cannot catch a broken block
# there: bash 3.2 (still /bin/bash on macOS, and so what the documented
# `curl | bash` one-liner runs) only trips over a here-doc nested in a
# substitution at *expansion* time, and the failure is non-fatal, so a broken
# installer quietly falls back to its hardcoded dependency list. Actually
# running the block is the only check that sees the difference:
#
#   /bin/bash tests/install-dep-read.sh    # on macOS: bash 3.2.57
#
# Exits non-zero unless the block read at least one dependency.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-python3}"
CLONE_TMP="${CLONE_TMP:-$ROOT}"

HARNESS="$(mktemp)"
trap 'rm -f "$HARNESS"' EXIT

# The block verbatim between its markers, wrapped in the same shell options
# install.sh sets and the two variables it reads. Copying it instead would
# defeat the purpose — the copy would stay correct while install.sh broke.
{
  printf 'set -euo pipefail\n'
  printf 'PY=%s\n' "$(printf '%q' "$PY")"
  printf 'CLONE_TMP=%s\n' "$(printf '%q' "$CLONE_TMP")"
  sed -n '/^# BEGIN dep-read/,/^# END dep-read/p' "$ROOT/install.sh"
  printf 'printf "%%s\\n" ${FCM_DEPS[@]+"${FCM_DEPS[@]}"}\n'
} > "$HARNESS"

if ! grep -q '^# END dep-read' "$HARNESS"; then
  echo "install-dep-read: no complete '# BEGIN/END dep-read' block in install.sh" >&2
  exit 1
fi

DEPS="$("$BASH" "$HARNESS")"
if [ -z "$DEPS" ]; then
  echo "install-dep-read: read no dependencies from $CLONE_TMP/pyproject.toml" >&2
  exit 1
fi
printf '%s\n' "$DEPS"
