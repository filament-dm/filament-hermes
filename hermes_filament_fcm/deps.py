"""Runtime dependency check (stdlib-only, unit-testable).

This plugin is installed as a *directory plugin* (git-cloned into
``~/.hermes/plugins/``); its Python dependencies are installed separately by
``install.sh`` and are NOT re-installed by ``hermes plugins update`` (which only
git-pulls the plugin code). Most releases are code-only, so that's fine — but a
release that bumps a dependency needs the dep refreshed out of band.

``dep_problem()`` makes that legible: it verifies each ``REQUIRED`` dependency is
present and within its version range, and returns a human-readable remediation
string when it isn't (or ``None`` when all is well). The plugin wires this into
``check_requirements`` so a stale/missing dep surfaces as an actionable message
instead of a raw ``ImportError`` at gateway start.

The guarded ``firebase_messaging`` import also exercises its compiled stack
(aiohttp / cryptography / protobuf). It does NOT cover ``structlog``, which is a
separate top-level dependency (imported by ``observability.py``); that one is
verified through its distribution metadata in the ``REQUIRED`` loop below. Keep
``REQUIRED`` in step with ``[project.dependencies]`` and install.sh's
``FCM_DEPS`` — a dep present in the code but missing from those two installs
fails at import before this check can run.

Keep this stdlib-only (no ``packaging``) so it works on the same interpreters
as ``_version`` and never adds a dependency of its own.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# Single source of truth for the runtime dependency contract. Keep in sync with
# [project.dependencies] in pyproject.toml and the deps install.sh installs.
# httpx is a Hermes core dependency, so it is always present; the FCM-specific
# deps (firebase-messaging and structlog) are the ones worth checking at runtime.
REQUIRED = {
    "firebase-messaging": ">=0.4.5,<1",
    "structlog": ">=25.5.0,<26",
}

# How the operator refreshes deps when a release bumps them (rare). install.sh
# is idempotent and re-installs the current deps.
REFRESH_HINT = (
    "re-run the Filament install command from the app (it refreshes "
    "dependencies), then restart the gateway"
)


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse "0.4.5" → (0, 4, 5); None when nothing numeric leads.

    Mirrors ``_version._version_tuple`` — only leading numeric dot-components
    count, so a suffix like "rc1" compares equal to its release.
    """
    parts: list[int] = []
    for piece in version.strip().split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts) if parts else None


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two version tuples, zero-padding to equal width."""
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


_OP_RE = re.compile(r"(>=|<=|==|>|<)\s*([0-9][0-9.]*)")


def satisfies(installed: str, spec: str) -> bool:
    """True when *installed* meets every comma-separated constraint in *spec*.

    Supports ``>=``, ``>``, ``<=``, ``<``, ``==`` (e.g. ">=0.4.5,<1"). An
    unparseable installed version fails closed (returns False) so a weird
    version string surfaces as a dep problem rather than silently passing.
    """
    iv = _version_tuple(installed)
    if iv is None:
        return False
    for op, rhs in _OP_RE.findall(spec):
        bv = _version_tuple(rhs)
        if bv is None:
            continue
        c = _cmp(iv, bv)
        if op == ">=" and not (c >= 0):
            return False
        if op == ">" and not (c > 0):
            return False
        if op == "<=" and not (c <= 0):
            return False
        if op == "<" and not (c < 0):
            return False
        if op == "==" and c != 0:
            return False
    return True


def dep_problem() -> str | None:
    """Return an actionable message if a required dependency is missing/stale.

    Returns ``None`` when every required dependency is importable and in range.
    """
    # A guarded import of firebase_messaging also exercises its compiled stack
    # (aiohttp/cryptography/protobuf); if any is absent, this fails and we
    # report the whole set as unavailable. Imported lazily (not at module top)
    # so this module — and thus the dep-check itself — never fails to load.
    try:
        import firebase_messaging  # noqa: F401, PLC0415
    except Exception as exc:
        return (
            f"firebase-messaging (and its dependency stack) is not importable "
            f"({exc}). To fix: {REFRESH_HINT}."
        )

    for name, spec in REQUIRED.items():
        try:
            installed = _dist_version(name)
        except PackageNotFoundError:
            return (
                f"{name} is imported but its distribution metadata is missing; "
                f"cannot verify it satisfies {spec}. To fix: {REFRESH_HINT}."
            )
        if not satisfies(installed, spec):
            return (
                f"{name} {installed} does not satisfy {spec}. "
                f"To fix: {REFRESH_HINT}."
            )
    return None
