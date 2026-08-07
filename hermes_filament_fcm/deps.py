"""Runtime dependency check (stdlib-only, unit-testable).

This plugin is installed as a *directory plugin* (git-cloned into
``~/.hermes/plugins/``) and carries its Python dependencies in its own
``vendor/`` tree, which the root ``__init__.py`` puts on ``sys.path``. So
``hermes plugins update`` — a git pull of the plugin tree — refreshes code and
dependencies together, and there is no separate dependency step to forget.

The root ``__init__.py`` prepends that tree, so the vendored copy wins over
anything already installed. What can still go wrong is the tree being missing
outright, from a partial clone or a hand-assembled plugin directory.

Winning is deliberate: these packages are ours alone, and a stale copy left in
site-packages by an older install used to shadow them. ``vendor_shadow_warnings()``
reports the one case where winning is the wrong call — the environment holding a
*different* version, which is what it would look like if Hermes started shipping
one of them.

``firebase-messaging`` is a special case, because we vendor our own fork
(``filament/integration`` — see ``scripts/vendor-deps.sh``) and it keeps
upstream's version number. A stock PyPI build therefore shadows the fork without
failing any version check, and the symptom is an agent that connects, looks
healthy and never wakes: upstream cannot decode a push header on current
CPython, and treats that failure as fatal to the whole receiver.
``fork_warning()`` looks for the fix itself rather than a version, so that case
says so instead of going quiet.

``dep_problem()`` makes either case legible: it verifies ``firebase-messaging``
is importable and within the required version range, and returns a
human-readable remediation string when it isn't (or ``None`` when all is well).
The plugin wires this into ``check_requirements`` so a stale/missing dep
surfaces as an actionable message instead of a raw ``ImportError`` at gateway
start.

Importing ``firebase_messaging`` here also pulls its compiled stack
(aiohttp / cryptography / protobuf), so a single guarded import covers the
whole dependency set — if any piece is missing, the import fails and we report
it, rather than crashing later.

Keep this stdlib-only (no ``packaging``) so it works on the same interpreters
as ``_version`` and never adds a dependency of its own.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, DistributionFinder, PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

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

# How the operator gets back to a good state. `plugins update` re-pulls the
# plugin tree, vendor/ included, which is the fix when the tree is incomplete —
# and since vendor/ is prepended, that is enough. Nothing has to be uninstalled.
#
# The plugin id is spelled out rather than imported: this module is deliberately
# stdlib-only (see the docstring) and setup_cli, which owns PLUGIN_ID, pulls in
# Hermes and PyYAML. Keep it in step with `name` in plugin.yaml — a
# no-stale-command test guards the pair.
REFRESH_HINT = (
    "run `hermes plugins update filament` (this pulls the plugin's "
    "vendored dependencies too) and restart the gateway"
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


def _vendor_dir() -> Path:
    """The plugin's ``vendor/`` tree, which the root ``__init__.py`` prepends."""
    return Path(__file__).resolve().parent.parent / "vendor"


def _normalize(name: str) -> str:
    return name.replace("_", "-").strip().lower()


def vendored_distributions() -> dict[str, str]:
    """``{distribution: version}`` from ``vendor/``'s .dist-info directories.

    Read off the tree rather than listed here, so adding or dropping a vendored
    package needs no second edit.
    """
    out: dict[str, str] = {}
    vendor = _vendor_dir()
    if not vendor.is_dir():
        return out
    for entry in vendor.glob("*.dist-info"):
        stem = entry.name[: -len(".dist-info")]
        name, _, version = stem.rpartition("-")
        if name and version:
            out[_normalize(name)] = version
    return out


def vendor_shadow_warnings() -> list[str]:
    """Warn when vendor/ is shadowing a *different* build of what it carries.

    vendor/ is prepended, so ours wins. That is right while these packages are
    ours alone, and wrong the day Hermes ships one, because we would then be
    forcing our pin on the rest of the process. Path order cannot express that
    difference and would never tell us reality had changed; this does.

    Only a version mismatch is reported. Our own install.sh pip-installs these
    same distributions into the engine venv, so "present outside vendor/" is the
    normal case and warning on it would be constant noise — which is how a real
    signal gets ignored. A different version is the case worth a human look.
    """
    ours = vendored_distributions()
    if not ours:
        return []

    vendor = str(_vendor_dir())
    elsewhere = [p for p in sys.path if p and p != vendor]
    try:
        context = DistributionFinder.Context(path=elsewhere)
        found = Distribution.discover(context=context)
        outside = {}
        for dist in found:
            name = _normalize((dist.metadata or {}).get("Name") or "")
            if name and name not in outside:
                outside[name] = dist.version
    except Exception:
        return []

    warnings: list[str] = []
    for name, mine in sorted(ours.items()):
        theirs = outside.get(name)
        if theirs and theirs != mine:
            warnings.append(
                f"vendor/ ships {name} {mine} and the environment has {theirs}, "
                f"which the vendored copy is shadowing for the whole process. If "
                f"{theirs} now comes from Hermes, stop vendoring {name} and widen "
                f"its range in pyproject.toml rather than overriding it."
            )
    return warnings


def fork_warning() -> str | None:
    """Warn when firebase-messaging is a stock build rather than our fork.

    Detects the fix by looking for the header parser it introduced, because the
    fork carries upstream's version number and no version check can separate the
    two (see the module docstring).

    A warning rather than a hard failure: this reads a private name, so a future
    fork could rename it, and taking the platform down over a heuristic is worse
    than the thing it guards. Returns ``None`` when the fix is present, and also
    when firebase_messaging cannot be imported at all — ``dep_problem`` owns that
    case and reporting it twice helps nobody.
    """
    try:
        from firebase_messaging import fcmpushclient  # noqa: PLC0415
    except Exception:
        return None

    if hasattr(fcmpushclient, "_header_param"):
        return None

    return (
        "firebase-messaging looks like a stock build, not the Filament fork: it "
        "has no Crypto-Key/Encryption header parser, so on current CPython every "
        "push fails to decode and the receiver shuts down — this agent will "
        "connect and then never wake. A separately installed copy takes "
        "precedence over the vendored one, so uninstall it; if the vendored tree "
        f"itself is stale, {REFRESH_HINT}."
    )


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
