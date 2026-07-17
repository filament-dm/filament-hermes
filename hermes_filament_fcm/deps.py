"""Runtime dependency check (stdlib-only, unit-testable).

This plugin is installed as a *directory plugin* (git-cloned into
``~/.hermes/plugins/``); its Python dependencies are installed separately by
``install.sh`` and are NOT re-installed by ``hermes plugins update`` (which only
git-pulls the plugin code). Most releases are code-only, so that's fine — but a
release that bumps a dependency needs the dep refreshed out of band.

``dep_problem()`` makes that legible: it verifies ``firebase-messaging`` is
importable and within the required version range, and returns a human-readable
remediation string when it isn't (or ``None`` when all is well). The plugin
wires this into ``check_requirements`` so a stale/missing dep surfaces as an
actionable message instead of a raw ``ImportError`` at gateway start.

Importing ``firebase_messaging`` here also pulls its compiled stack
(aiohttp / cryptography / protobuf), so a single guarded import covers the
whole dependency set — if any piece is missing, the import fails and we report
it, rather than crashing later.

Keep this stdlib-only (no ``packaging``) so it works on the same interpreters
as ``_version`` and never adds a dependency of its own.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# HARD dependencies — the plugin cannot function without these, so a missing
# one makes check_requirements() fail (the platform stays down with an
# actionable message). Keep in sync with [project.dependencies] in
# pyproject.toml. httpx is a Hermes core dependency, so it is always present;
# only the FCM-specific dep is worth checking at runtime.
REQUIRED = {"firebase-messaging": ">=0.4.5,<1"}

# SOFT dependencies — the plugin runs without these but in a degraded mode, so a
# missing one produces a warning nudge rather than taking the platform down.
# structlog powers structured logging (see observability.py, which falls back to
# plain stdlib logging when it's absent).
OPTIONAL = {"structlog": ">=25.5.0,<26"}

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


def optional_dep_warnings() -> list[str]:
    """Return nudges for missing/out-of-range SOFT dependencies.

    Unlike :func:`dep_problem`, these never take the platform down — the plugin
    runs in a degraded mode without them (e.g. plain instead of structured
    logging). Typically triggered after a code-only ``hermes plugins update``
    that pulled code needing a newly-added optional dep.
    """
    warnings: list[str] = []
    for name, spec in OPTIONAL.items():
        module = name.replace("-", "_")
        try:
            __import__(module)
        except Exception:
            warnings.append(
                f"{name} is not installed — running in a degraded mode "
                f"(observability logs will be plain text). To restore: {REFRESH_HINT}."
            )
            continue
        try:
            installed = _dist_version(name)
        except PackageNotFoundError:
            continue
        if not satisfies(installed, spec):
            warnings.append(
                f"{name} {installed} does not satisfy {spec} — some features may "
                f"be degraded. To fix: {REFRESH_HINT}."
            )
    return warnings
