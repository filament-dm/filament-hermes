"""Tests for the directory-plugin entry point and its vendored dependencies.

`hermes plugins install <repo> --enable` clones this repo and nothing else — no
pip, no uv, no plugin code executed. Everything that makes the installed tree a
working plugin therefore has to be *in the repo*, and these tests pin the parts
that are easy to break without noticing:

* the root ``__init__.py`` exists, exposes ``register``, and imports with only
  the standard library available;
* ``vendor/`` carries every dependency that is supposed to be vendored, at a
  version satisfying pyproject's constraint;
* ``vendor/`` stays pure Python, so the one committed tree works everywhere.

Loaded standalone (no Hermes, no firebase) like the other tests here.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# The version-range checker, loaded standalone by that module.
from test_deps import deps

_ROOT = Path(__file__).resolve().parent.parent
_INIT = _ROOT / "__init__.py"
_VENDOR = _ROOT / "vendor"

# Declared in pyproject, shipped in vendor/. httpx is the third declared
# dependency and is deliberately NOT vendored — it is a Hermes core dependency,
# like firebase-messaging's aiohttp/cryptography/protobuf. http-ece is not
# declared (it is firebase-messaging's own requirement) but is vendored, because
# nothing else supplies it.
VENDORED = ("firebase-messaging", "http-ece", "structlog")
NOT_VENDORED = ("httpx", "aiohttp", "cryptography", "protobuf")


def _load_root_init(package: str | None, seed_vendor: bool = False):
    """Exec the root ``__init__.py`` under a fresh module object.

    ``package`` mirrors the two ways it actually gets imported: Hermes' loader
    sets ``__package__``/``__path__`` so relative imports resolve
    (``hermes_cli/plugins.py:_load_directory_module``), while pytest imports it
    as a bare ``__init__`` module with no parent package once an
    ``__init__.py`` appears at the rootdir.

    Any pre-existing ``vendor/`` entry is stripped from ``sys.path`` first, and
    re-seeded only when ``seed_vendor`` asks for it. Without that these tests
    would be measuring pytest's own Package import of this same file, which has
    already appended the entry by the time they run. ``sys.path`` is restored on
    the way out either way.
    """
    name = package or "__init__"
    spec = importlib.util.spec_from_file_location(name, _INIT)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
        module.__path__ = [str(_ROOT)]
    saved_path = list(sys.path)
    saved_mod = sys.modules.get(name)
    sys.modules[name] = module
    try:
        sys.path[:] = [p for p in sys.path if p != str(_VENDOR)]
        if seed_vendor:
            sys.path.append(str(_VENDOR))
        spec.loader.exec_module(module)
        return module, list(sys.path)
    finally:
        sys.path[:] = saved_path
        if saved_mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved_mod


# ── the entry point ──────────────────────────────────────────────────


def test_root_init_exists():
    """Hermes' directory loader raises FileNotFoundError without this file."""
    assert _INIT.is_file()


def test_root_init_imports_without_a_parent_package():
    """The regression guard: no module-level relative import.

    pytest makes the rootdir a Package and imports this file before every test.
    A module-level ``from .hermes_filament_fcm import register`` raises
    "attempted relative import with no known parent package" there, which turns
    every test in the repo into a collection error.
    """
    module, _ = _load_root_init(package=None)
    assert callable(module.register)


def test_root_init_puts_vendor_on_sys_path():
    module, path_after = _load_root_init(package="filament_fcm_undertest")
    assert str(_VENDOR) in path_after
    assert callable(module.register)


def test_vendor_is_appended_not_prepended():
    """An ambient install must win over the vendored copy, package by package.

    Prepending would silently override a dependency the user installed
    deliberately, and make deps.py report a version that isn't the one a
    plain ``import`` in the same process would get.
    """
    _, path_after = _load_root_init(package="filament_fcm_undertest2")
    assert path_after.index(str(_VENDOR)) == len(path_after) - 1


def test_root_init_is_idempotent():
    """Loading with the entry already present must not stack a duplicate.

    Hermes imports this file once per gateway process, but pytest's Package
    import and the plugin loader can both run in one interpreter.
    """
    _, fresh = _load_root_init(package="filament_fcm_undertest3")
    _, seeded = _load_root_init(package="filament_fcm_undertest4", seed_vendor=True)
    assert fresh.count(str(_VENDOR)) == 1
    assert seeded.count(str(_VENDOR)) == 1


# ── the vendored tree ────────────────────────────────────────────────


def _pyproject_constraints() -> dict[str, str]:
    """``{distribution: version specifier}`` from [project].dependencies.

    Parsed with a regex rather than tomllib so this matches ``_version.py``'s
    stdlib-only rule and runs on the same interpreters.
    """
    text = (_ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.M | re.S)
    assert block, "no [project].dependencies in pyproject.toml"
    out = {}
    for raw in re.findall(r"[\"']([^\"']+)[\"']", block.group(1)):
        m = re.match(r"([A-Za-z0-9._-]+)\s*(.*)", raw)
        out[m.group(1)] = m.group(2)
    return out


def _vendored_version(dist: str) -> str | None:
    """The version in vendor/'s .dist-info for *dist*, or None if absent."""
    prefix = dist.replace("-", "_")
    for d in _VENDOR.glob(f"{prefix}-*.dist-info"):
        return d.name[len(prefix) + 1 : -len(".dist-info")]
    return None


@pytest.mark.parametrize("dist", VENDORED)
def test_dependency_is_vendored(dist):
    assert _vendored_version(dist) is not None, (
        f"{dist} is missing from vendor/ — run scripts/vendor-deps.sh. Without "
        "it the plugin cannot import on a host that has no ambient copy, which "
        "is every stock Hermes."
    )


@pytest.mark.parametrize("dist", VENDORED)
def test_vendored_version_satisfies_pyproject(dist):
    """vendor/ must not drift from the declared constraint.

    Only the declared ones are checked: http-ece is firebase-messaging's own
    requirement, so pyproject says nothing about it.
    """
    constraints = _pyproject_constraints()
    if dist not in constraints:
        pytest.skip(f"{dist} is not declared in pyproject (transitive)")
    installed = _vendored_version(dist)
    assert deps.satisfies(installed, constraints[dist]), (
        f"vendor/ has {dist} {installed}, which does not satisfy "
        f"{constraints[dist]!r} from pyproject.toml"
    )


@pytest.mark.parametrize("dist", NOT_VENDORED)
def test_core_dependencies_are_not_vendored(dist):
    """Vendoring these would ship platform-specific compiled wheels and risk
    clashing with the copies Hermes imports itself."""
    assert _vendored_version(dist) is None, (
        f"{dist} was vendored — it is a Hermes core dependency and must come "
        "from the environment. Check scripts/vendor-deps.sh still passes "
        "--no-deps."
    )


def test_vendor_tree_is_pure_python():
    """A compiled artifact means the tree stopped being portable across
    platforms and interpreter versions."""
    compiled = [
        str(p.relative_to(_VENDOR))
        for ext in ("*.so", "*.pyd", "*.dylib")
        for p in _VENDOR.rglob(ext)
    ]
    assert not compiled, f"compiled extensions in vendor/: {compiled}"


def test_vendored_dists_keep_their_metadata():
    """deps.py reads versions through importlib.metadata, which needs the
    .dist-info directories to survive vendoring."""
    for dist in VENDORED:
        prefix = dist.replace("-", "_")
        info = list(_VENDOR.glob(f"{prefix}-*.dist-info"))
        assert info, f"{dist} has no .dist-info in vendor/"
        assert (info[0] / "METADATA").is_file(), f"{dist} .dist-info has no METADATA"
