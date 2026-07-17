"""Tests for the runtime dependency check (``deps.py``).

Loaded standalone (no Hermes, no firebase) like the other tests — ``deps.py``
is stdlib-only and has no relative imports, so it loads directly.
"""

import importlib.util
import sys
import types
from pathlib import Path

_DEPS_PATH = Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "deps.py"


def _load_deps():
    spec = importlib.util.spec_from_file_location("_fcm_deps_undertest", _DEPS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deps = _load_deps()


# ── satisfies() — the version-range checker ──────────────────────────


def test_satisfies_range_ok():
    assert deps.satisfies("0.4.5", ">=0.4.5,<1")
    assert deps.satisfies("0.9.9", ">=0.4.5,<1")
    assert deps.satisfies("0.4.6", ">=0.4.5,<1")


def test_satisfies_below_min():
    assert not deps.satisfies("0.4.4", ">=0.4.5,<1")
    assert not deps.satisfies("0.3.0", ">=0.4.5,<1")


def test_satisfies_at_or_above_max():
    assert not deps.satisfies("1.0.0", ">=0.4.5,<1")
    assert not deps.satisfies("2.1.0", ">=0.4.5,<1")


def test_satisfies_ignores_suffix():
    # leading numeric components only — a suffix compares equal to its release
    assert deps.satisfies("0.4.5rc1", ">=0.4.5,<1")


def test_satisfies_unparseable_installed_fails_closed():
    assert not deps.satisfies("garbage", ">=0.4.5,<1")


def test_satisfies_equality():
    assert deps.satisfies("1.2.3", "==1.2.3")
    assert not deps.satisfies("1.2.4", "==1.2.3")


# ── dep_problem() — the actionable message ───────────────────────────


# Satisfying versions for every REQUIRED dep, so a test that isn't probing a
# specific failure gets a clean pass regardless of what's installed.
_OK_VERSIONS = {"firebase-messaging": "0.4.5", "structlog": "25.5.0"}


def test_dep_problem_missing_import(monkeypatch):
    # No firebase_messaging importable → a message pointing at the refresh path.
    # Setting sys.modules[name] = None forces `import firebase_messaging` to
    # raise ImportError even when the package is really installed in the test
    # env (a bare delitem would just let it re-import).
    monkeypatch.setitem(sys.modules, "firebase_messaging", None)
    problem = deps.dep_problem()
    assert problem is not None
    assert "firebase-messaging" in problem
    assert deps.REFRESH_HINT in problem


def test_dep_problem_present_but_no_metadata(monkeypatch):
    # firebase importable (stubbed) but no dist-info → still flagged, since we
    # can't confirm the version satisfies the requirement.
    stub = types.ModuleType("firebase_messaging")
    monkeypatch.setitem(sys.modules, "firebase_messaging", stub)

    def _no_metadata(name: str) -> str:
        raise deps.PackageNotFoundError(name)

    monkeypatch.setattr(deps, "_dist_version", _no_metadata)
    problem = deps.dep_problem()
    assert problem is not None
    assert deps.REFRESH_HINT in problem


def test_dep_problem_ok(monkeypatch):
    # firebase importable AND a satisfying version for every REQUIRED dep → clean.
    stub = types.ModuleType("firebase_messaging")
    monkeypatch.setitem(sys.modules, "firebase_messaging", stub)
    monkeypatch.setattr(deps, "_dist_version", lambda name: _OK_VERSIONS[name])
    assert deps.dep_problem() is None


def test_dep_problem_flags_structlog_out_of_range(monkeypatch):
    # Regression guard for the install.sh dependency drift: structlog is a
    # REQUIRED dep in its own right (observability.py imports it), NOT covered by
    # the guarded firebase_messaging import. An out-of-range structlog must be
    # reported so a stale install surfaces legibly instead of ImportError-ing.
    stub = types.ModuleType("firebase_messaging")
    monkeypatch.setitem(sys.modules, "firebase_messaging", stub)
    versions = {"firebase-messaging": "0.4.5", "structlog": "24.0.0"}
    monkeypatch.setattr(deps, "_dist_version", lambda name: versions[name])
    problem = deps.dep_problem()
    assert problem is not None
    assert "structlog" in problem
    assert deps.REFRESH_HINT in problem
