"""Tests for the runtime dependency check (``deps.py``).

Loaded standalone (no Hermes, no firebase) like the other tests — ``deps.py``
is stdlib-only and has no relative imports, so it loads directly.
"""

import importlib.util
import sys
import types
from importlib.metadata import PackageNotFoundError
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


def test_dep_problem_missing_import(monkeypatch):
    # No firebase_messaging importable → a message pointing at the refresh path.
    # firebase-messaging is a real installed dependency, so clearing the import
    # cache is not enough — ``import firebase_messaging`` would just re-import the
    # installed package. Install a meta_path finder that raises for the target so
    # the import fails deterministically regardless of what's installed.
    monkeypatch.delitem(sys.modules, "firebase_messaging", raising=False)

    class _BlockFirebase:
        def find_spec(self, name, path=None, target=None):
            if name == "firebase_messaging":
                raise ModuleNotFoundError(name)
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockFirebase(), *sys.meta_path])
    monkeypatch.setattr(
        deps, "REQUIRED", {"firebase-messaging": ">=0.4.5,<1"}, raising=False
    )
    problem = deps.dep_problem()
    assert problem is not None
    assert "firebase-messaging" in problem
    assert deps.REFRESH_HINT in problem


def test_dep_problem_present_but_no_metadata(monkeypatch):
    # firebase importable (stubbed) but no dist-info → still flagged, since we
    # can't confirm the version satisfies the requirement. Because the real
    # distribution may be installed, force ``_dist_version`` to raise so the
    # "importable but no dist metadata" branch is exercised deterministically.
    stub = types.ModuleType("firebase_messaging")
    monkeypatch.setitem(sys.modules, "firebase_messaging", stub)

    def _no_metadata(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(deps, "_dist_version", _no_metadata)
    problem = deps.dep_problem()
    assert problem is not None
    assert deps.REFRESH_HINT in problem


def test_dep_problem_ok(monkeypatch):
    # firebase importable AND a satisfying version reported → no problem.
    stub = types.ModuleType("firebase_messaging")
    monkeypatch.setitem(sys.modules, "firebase_messaging", stub)
    monkeypatch.setattr(deps, "_dist_version", lambda name: "0.4.5")
    assert deps.dep_problem() is None


# ── optional_dep_warnings() — soft deps (e.g. structlog) ─────────────


def test_optional_warning_when_soft_dep_missing(monkeypatch):
    # A soft dep that isn't importable → a nudge, but never a hard failure.
    monkeypatch.setattr(deps, "OPTIONAL", {"nonexistent-soft-dep": ">=1,<2"})
    monkeypatch.delitem(sys.modules, "nonexistent_soft_dep", raising=False)
    warnings = deps.optional_dep_warnings()
    assert len(warnings) == 1
    assert "nonexistent-soft-dep" in warnings[0]
    assert deps.REFRESH_HINT in warnings[0]


def test_optional_no_warning_when_soft_dep_present(monkeypatch):
    # Importable and in range → no nudge.
    stub = types.ModuleType("structlog")
    monkeypatch.setattr(deps, "OPTIONAL", {"structlog": ">=25.5.0,<26"})
    monkeypatch.setitem(sys.modules, "structlog", stub)
    monkeypatch.setattr(deps, "_dist_version", lambda name: "25.5.0")
    assert deps.optional_dep_warnings() == []


def test_optional_warning_when_soft_dep_out_of_range(monkeypatch):
    stub = types.ModuleType("structlog")
    monkeypatch.setattr(deps, "OPTIONAL", {"structlog": ">=25.5.0,<26"})
    monkeypatch.setitem(sys.modules, "structlog", stub)
    monkeypatch.setattr(deps, "_dist_version", lambda name: "24.1.0")
    warnings = deps.optional_dep_warnings()
    assert len(warnings) == 1
    assert "structlog" in warnings[0]


# ── fork_warning() — stock firebase-messaging shadowing our fork ─────


def test_fork_warning_silent_when_the_fix_is_present():
    # The real dependency is the fork, so this is also a live check that the
    # installed build is the one we expect.
    assert deps.fork_warning() is None


def test_fork_warning_flags_a_stock_build(monkeypatch):
    # A stock build has no header parser. It keeps upstream's version, so no
    # version check would catch it — this is the only thing that does.
    stub = types.ModuleType("firebase_messaging.fcmpushclient")
    parent = types.ModuleType("firebase_messaging")
    parent.fcmpushclient = stub
    monkeypatch.setitem(sys.modules, "firebase_messaging", parent)
    monkeypatch.setitem(sys.modules, "firebase_messaging.fcmpushclient", stub)

    warning = deps.fork_warning()
    assert warning is not None
    assert "fork" in warning
    assert "never wake" in warning


def test_fork_warning_quiet_when_firebase_is_absent(monkeypatch):
    # dep_problem owns the missing-dependency case; saying it twice helps nobody.
    monkeypatch.delitem(sys.modules, "firebase_messaging", raising=False)
    monkeypatch.delitem(sys.modules, "firebase_messaging.fcmpushclient", raising=False)

    class _BlockFirebase:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("firebase_messaging"):
                raise ModuleNotFoundError(name)
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockFirebase(), *sys.meta_path])
    assert deps.fork_warning() is None


# ── vendor_shadow_warnings() — when vendoring stops being right ──────


def test_vendored_distributions_reads_the_tree():
    # Discovered, not listed, so adding or dropping one needs no second edit.
    dists = deps.vendored_distributions()
    assert set(dists) == {"firebase-messaging", "http-ece", "structlog"}
    assert dists["firebase-messaging"] == "0.4.5"


def test_quiet_when_the_environment_matches_what_we_vendor(monkeypatch):
    # The normal case. install.sh pip-installs these same distributions, so
    # "present outside vendor/" must not warn or the signal becomes noise.
    monkeypatch.setattr(sys, "path", [str(deps._vendor_dir())])
    assert deps.vendor_shadow_warnings() == []


def test_warns_when_the_environment_has_a_different_version(tmp_path, monkeypatch):
    # Stands in for Hermes shipping its own, newer structlog: we would then be
    # forcing our pin on the whole process.
    (tmp_path / "structlog-26.1.0.dist-info").mkdir()
    (tmp_path / "structlog-26.1.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: structlog\nVersion: 26.1.0\n"
    )
    monkeypatch.setattr(sys, "path", [str(deps._vendor_dir()), str(tmp_path)])

    warnings = deps.vendor_shadow_warnings()
    assert len(warnings) == 1
    assert "structlog" in warnings[0]
    assert "26.1.0" in warnings[0] and "25.5.0" in warnings[0]
    # It has to say what to do, not merely that something is odd.
    assert "stop vendoring" in warnings[0]


def test_same_version_elsewhere_is_not_a_warning(tmp_path, monkeypatch):
    # Exactly what our own installer leaves behind.
    (tmp_path / "structlog-25.5.0.dist-info").mkdir()
    (tmp_path / "structlog-25.5.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: structlog\nVersion: 25.5.0\n"
    )
    monkeypatch.setattr(sys, "path", [str(deps._vendor_dir()), str(tmp_path)])
    assert deps.vendor_shadow_warnings() == []


def test_vendor_dir_itself_is_never_the_shadowed_one(monkeypatch):
    monkeypatch.setattr(sys, "path", [str(deps._vendor_dir())] * 2)
    assert deps.vendor_shadow_warnings() == []
