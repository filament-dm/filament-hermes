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


_INIT_SRC = (
    Path(__file__).parent.parent / "hermes_filament_fcm" / "__init__.py"
).read_text()


def _tool_handler_src() -> str:
    return _INIT_SRC.split("def _make_tool_handler(", 1)[1].split("\ndef ", 1)[0]


def test_tool_proxy_checks_result_error():
    """The proxy must consult result_error. parse_tool_result flattens an error
    envelope into something that reads like a result."""
    assert "result_error(result)" in _tool_handler_src()


def test_tool_proxy_never_returns_an_empty_error():
    """str(exc) is "" for several transport errors, so the class is named."""
    src = _tool_handler_src()
    assert "type(exc).__name__" in src
    assert 'json.dumps({"error": str(exc)})' not in src
