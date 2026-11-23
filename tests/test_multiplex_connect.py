"""Connecting a profile a multiplexed gateway serves does not restart it.

The gateway starts a served profile's adapters again when the profile's .env
changes, so a restart would only interrupt every other profile it serves.
``_restart_gateway`` and ``_served_by_multiplexer`` are loaded standalone via
AST (setup_cli's imports need Hermes).
"""

import ast
import sys
import types
from pathlib import Path

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)
_NAMES = {"_restart_gateway", "_served_by_multiplexer"}


def _load(ns):
    tree = ast.parse(_SETUP_CLI.read_text())
    nodes = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in _NAMES
    ]
    exec(compile(ast.Module(nodes, []), str(_SETUP_CLI), "exec"), ns)
    return ns


def _host(monkeypatch, served):
    gateway = types.ModuleType("hermes_cli.gateway")
    gateway.named_profile_served_by_running_multiplexer = lambda: served
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", gateway)


def _restart(monkeypatch, served):
    _host(monkeypatch, served)
    calls = []
    ns = _load(
        {
            "print_success": lambda text: calls.append("said"),
            "_running_gateway_pid": lambda: None,
            "_ancestor_pids": set,
            "_schedule_gateway_handoff": lambda: False,
            "_ensure_gateway": lambda **kw: calls.append("ensured"),
            "_hermes_argv": list,
            "_gateway_auto_started": lambda pid: False,
            "subprocess": types.SimpleNamespace(run=None, Popen=None),
        }
    )
    ns["_restart_gateway"]()
    return calls


def test_a_served_profile_is_left_to_the_running_gateway(monkeypatch):
    assert _restart(monkeypatch, served=True) == ["said"]


def test_any_other_profile_gets_a_gateway(monkeypatch):
    assert _restart(monkeypatch, served=False) == ["ensured"]


def test_a_hermes_without_multiplexing_is_never_served(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.gateway", None)
    assert _load({})["_served_by_multiplexer"]() is False
