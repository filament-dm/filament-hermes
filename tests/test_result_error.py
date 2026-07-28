"""result_error: a tool-call envelope's server rejection must be visible.

Loaded standalone per the repo test pattern (no Hermes, stub httpx)."""

import importlib.util
import sys
import types
from pathlib import Path


def _load_filament_api():
    """Standalone load with a synthetic parent package, so the module's
    relative imports (._version, .observability) resolve without installing
    the package or having Hermes present."""
    if "httpx" not in sys.modules:
        stub = types.ModuleType("httpx")
        stub.AsyncClient = type("AsyncClient", (), {})  # annotation-only use
        sys.modules["httpx"] = stub
    base = Path(__file__).parent.parent / "hermes_filament_fcm"
    pkg_name = "hfcm_standalone"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(base)]
        sys.modules[pkg_name] = pkg
    for sub in ("_version", "observability", "filament_api"):
        full = f"{pkg_name}.{sub}"
        if full in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(full, base / f"{sub}.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[full] = m
        spec.loader.exec_module(m)
    return sys.modules[f"{pkg_name}.filament_api"]


def test_error_envelope_is_surfaced():
    api = _load_filament_api().FilamentAPI
    err = {"code": -32602, "message": "You don't have permission to knock"}
    assert api.result_error({"error": err}) == "You don't have permission to knock"
    assert api.result_error({"error": {"code": -32003}}) == "code -32003"


def test_success_and_junk_are_none():
    api = _load_filament_api().FilamentAPI
    assert api.result_error({"result": {"content": []}}) is None
    assert api.result_error(None) is None
    # A bare-string error (e.g. the {"error": "HTTP 500"} envelope _post
    # synthesizes) is a real failure and must be surfaced, not swallowed.
    assert api.result_error({"error": "stringy"}) == "stringy"
    assert api.result_error({"error": ""}) is None
