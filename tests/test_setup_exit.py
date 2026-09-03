"""Exit-code semantics of the filament-fcm-setup entry point.

A scripted (hosted) install must fail loudly when setup does not complete,
but an interactive operator declining "Reconfigure?" on a working install
is a no-op, not a failure — that must exit 0 (local and dockerized hermes
run this wizard by hand).

Loaded standalone via AST: setup_cli's imports need Hermes, and these tests
only need ``main`` and the decline branch of ``_run_interactive_setup``.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)


def _extract(name: str, ns: dict):
    tree = ast.parse(_SETUP_CLI.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{name} not found in setup_cli.py")


def _noop(*args, **kwargs):
    return None


def _main_with(ready, monkeypatch):
    monkeypatch.delenv("FILAMENT_SETUP_SKIP_RESTART", raising=False)
    restarts = []
    ns = {
        "os": os,
        "sys": sys,
        "print_header": _noop,
        "print_info": _noop,
        "migrate_legacy_install": _noop,
        "_run_interactive_setup": lambda: ready,
        "_restart_gateway": lambda: restarts.append(True),
        "_find_hermes_home": lambda: Path("/tmp/hermes-home"),
    }
    return _extract("main", ns), restarts


def test_completed_setup_exits_zero_and_restarts(monkeypatch):
    main, restarts = _main_with(True, monkeypatch)
    main()
    assert restarts == [True]


def test_declined_reconfigure_is_a_noop_exit_zero(monkeypatch):
    """The existing configuration stays valid: no restart, no failure."""
    main, restarts = _main_with(None, monkeypatch)
    main()
    assert restarts == []


def test_incomplete_setup_exits_nonzero(monkeypatch):
    main, restarts = _main_with(False, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert restarts == []


def test_decline_branch_returns_none(monkeypatch):
    """_run_interactive_setup: an existing token + declined "Reconfigure?"
    returns None (skip), reserving False for real failures."""
    monkeypatch.delenv("CONNECT_TOKEN", raising=False)
    ns = {
        "os": os,
        "print_header": _noop,
        "print_info": _noop,
        "get_env_value": lambda key: "fmcp_existing_token",
        "prompt_yes_no": lambda *a, **k: False,
    }
    run = _extract("_run_interactive_setup", ns)
    assert run() is None
