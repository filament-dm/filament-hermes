#!/bin/sh
# Rebuild vendor/ from PyPI.
#
# vendor/ carries the plugin's runtime dependencies inside the plugin
# directory, so `hermes plugins install <repo> --enable` is the whole
# installation: the installer only git-clones a directory and never invokes
# pip or uv, and on the Docker/cloud images the venv the gateway imports from
# is root-owned and read-only to the uid the gateway runs as. See
# ../_vendor.py for how the tree is put on sys.path.
#
# Only the pure-Python part of the dependency tree is vendored:
# firebase-messaging, its http-ece requirement, and structlog.
# firebase-messaging's other three requirements (aiohttp, cryptography,
# protobuf) are core Hermes dependencies already present at satisfying
# versions, and httpx is core too.
#
# --no-deps is what keeps those out. Without it uv pulls compiled wheels for
# the build host's platform and the tree stops being portable; the check at
# the end fails the script if that ever happens.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
VENDOR_DIR="$ROOT/vendor"

# Keep in sync with [project.dependencies] in pyproject.toml. Exact pins here
# (the constraints there are ranges) so the committed tree is reproducible.
FIREBASE_MESSAGING_VERSION=0.4.5
HTTP_ECE_VERSION=1.1.0   # firebase-messaging pins http-ece~=1.1.0
STRUCTLOG_VERSION=25.5.0

if command -v uv >/dev/null 2>&1; then
    INSTALL="uv pip install --target"
elif command -v pip >/dev/null 2>&1; then
    INSTALL="pip install --target"
else
    echo "vendor-deps: need either uv or pip on PATH" >&2
    exit 1
fi

rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"

# shellcheck disable=SC2086
$INSTALL "$VENDOR_DIR" --no-deps \
    "firebase-messaging==${FIREBASE_MESSAGING_VERSION}" \
    "http-ece==${HTTP_ECE_VERSION}" \
    "structlog==${STRUCTLOG_VERSION}"

# A compiled artifact here means --no-deps was bypassed or a dependency
# stopped being pure Python; the tree would silently stop being portable.
if find "$VENDOR_DIR" \( -name '*.so' -o -name '*.pyd' -o -name '*.dylib' \) \
        -print | grep -q .; then
    echo "vendor-deps: compiled extensions found in vendor/ — not portable" >&2
    exit 1
fi

# .dist-info must survive: deps.py verifies the installed versions through
# importlib.metadata, which reads it. bin/ and the installer's lock file are
# build residue with no role at import time.
rm -rf "$VENDOR_DIR/bin" "$VENDOR_DIR/.lock"

echo "vendor-deps: rebuilt $VENDOR_DIR"
