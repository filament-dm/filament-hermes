#!/bin/sh
# Rebuild vendor/ from our firebase-messaging fork and PyPI.
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

# firebase-messaging comes from our fork, not PyPI. Upstream sliced the
# Crypto-Key and Encryption headers by label length instead of parsing them,
# which fed the trailing parameters to the base64 decoder. That was harmless
# until CPython gh-145264 stopped the decoder discarding data after the first
# padded quad; from 3.13.x/3.14.4 on it raises "Incorrect padding", and because
# the client treats any error from a payload as fatal, one bad push killed the
# whole receiver and no notification arrived again.
#
# Carry fixes on filament/integration and upstream them from there. The branch
# is the source of truth; the pin below is the commit vendored into this tree,
# so bump it when the branch moves.
FIREBASE_MESSAGING_REPO=https://github.com/filament-dm/firebase-messaging.git
FIREBASE_MESSAGING_BRANCH=filament/integration
FIREBASE_MESSAGING_COMMIT=81a7249a9510569e74f9e210a8b7d4873485cfac

# Keep in sync with [project.dependencies] in pyproject.toml. Exact pins here
# (the constraints there are ranges) so the committed tree is reproducible.
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
    "firebase-messaging @ git+${FIREBASE_MESSAGING_REPO}@${FIREBASE_MESSAGING_COMMIT}" \
    "http-ece==${HTTP_ECE_VERSION}" \
    "structlog==${STRUCTLOG_VERSION}"

# The fork keeps upstream's version number, so the tree cannot be told apart
# from a PyPI build by version alone. Record what was vendored, because "which
# fork commit is in here" is the first question when a push stops arriving.
cat > "$VENDOR_DIR/FIREBASE_MESSAGING_SOURCE" <<EOF
repo   ${FIREBASE_MESSAGING_REPO}
branch ${FIREBASE_MESSAGING_BRANCH}
commit ${FIREBASE_MESSAGING_COMMIT}
EOF

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
