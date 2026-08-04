"""Tests for the ``hermes filament`` CLI command (``cli.py``).

This command is what makes the connect flow a copy-pasteable line — `hermes
plugins install ... --enable && hermes filament connect fmcp_...` — so the parts
Hermes depends on are worth pinning: the registered name, the argparse shape,
and the dispatch contract (`main.py` calls `args.func(args)` and uses the return
value as an exit code).

Loaded standalone, like the other tests here: ``cli.py`` imports nothing but the
standard library at module level, and defers ``setup_cli`` (which needs Hermes)
into the handler.
"""

import argparse
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _load_cli():
    pkg = types.ModuleType("hermes_filament_fcm")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament_fcm"] = pkg
    spec = importlib.util.spec_from_file_location(
        "hermes_filament_fcm.cli", _PKG_DIR / "cli.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_filament_fcm.cli"] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


class _Ctx:
    """Stand-in for Hermes' PluginContext."""

    def __init__(self):
        self.registered = {}

    def register_cli_command(self, **kwargs):
        self.registered = kwargs


def _parse(argv):
    """Build the parser the way main.py does, then parse."""
    parser = argparse.ArgumentParser(prog="hermes filament")
    cli._setup(parser)
    return parser.parse_args(argv)


# ── registration ─────────────────────────────────────────────────────


def test_registers_as_hermes_filament():
    ctx = _Ctx()
    cli.register_cli(ctx)
    assert ctx.registered["name"] == "filament"
    assert callable(ctx.registered["setup_fn"])
    assert callable(ctx.registered["handler_fn"])


def test_registration_is_optional():
    """A Hermes without plugin CLI commands must still load the plugin.

    The connect token then comes from the manifest's requires_env prompt
    instead, so this degrades rather than breaking the install.
    """

    class Old:
        pass

    cli.register_cli(Old())  # must not raise


def test_registration_failure_does_not_propagate():
    """register_cli_command raising must not fail the whole plugin load."""

    class Broken:
        def register_cli_command(self, **kwargs):
            raise RuntimeError("nope")

    cli.register_cli(Broken())  # must not raise


# ── the argparse shape ───────────────────────────────────────────────


def test_token_is_a_positional():
    """`hermes filament connect fmcp_x` — a positional keeps the copy-pasted
    line short, and matches the shape of `claude mcp add`."""
    args = _parse(["connect", "fmcp_abc"])
    assert args.filament_command == "connect"
    assert args.token == "fmcp_abc"


def test_url_override():
    """--url is how a dev/staging cluster is reached without editing .env."""
    args = _parse(["connect", "fmcp_abc", "--url", "http://local:8448/mcp/agents"])
    assert args.url == "http://local:8448/mcp/agents"


def test_url_defaults_to_none():
    """None means "use the saved value, else production" — resolved in
    connect(), not here, so the default can't drift between the two."""
    assert _parse(["connect", "fmcp_abc"]).url is None


def test_p_reads_the_token_from_stdin():
    """The opt-out for a shared machine: -p keeps the token out of argv."""
    assert _parse(["connect", "-p"]).from_stdin is True
    assert _parse(["connect", "--prompt"]).from_stdin is True


def test_command_line_token_is_the_default():
    """Convenience stays the default — one copy-pasteable line."""
    assert _parse(["connect", "fmcp_abc"]).from_stdin is False


def test_restart_is_the_default():
    """The line should end with a connected gateway, so restarting is opt-out."""
    assert _parse(["connect", "fmcp_abc"]).no_restart is False
    assert _parse(["connect", "fmcp_abc", "--no-restart"]).no_restart is True


def test_token_is_optional_at_the_parser():
    """A missing token must reach connect() to get its own message, rather than
    an argparse usage error that says nothing about where to find one."""
    assert _parse(["connect"]).token is None


# ── dispatch ─────────────────────────────────────────────────────────


def test_handler_rejects_a_bare_invocation():
    """`hermes filament` with no subcommand exits non-zero with usage."""
    assert cli._handler(_parse([])) == 2


def test_handler_calls_connect_and_returns_its_code(monkeypatch):
    """main.py uses the return value as the process exit code."""
    calls = {}

    def fake_connect(token, url=None, restart=True, from_stdin=False):
        calls.update(token=token, url=url, restart=restart, from_stdin=from_stdin)
        return 0

    stub = types.ModuleType("hermes_filament_fcm.setup_cli")
    stub.connect = fake_connect
    monkeypatch.setitem(sys.modules, "hermes_filament_fcm.setup_cli", stub)

    rc = cli._handler(_parse(["connect", "fmcp_abc", "--no-restart"]))
    assert rc == 0
    assert calls == {
        "token": "fmcp_abc",
        "url": None,
        "restart": False,
        "from_stdin": False,
    }


def test_handler_passes_the_stdin_flag_through(monkeypatch):
    seen = {}

    stub = types.ModuleType("hermes_filament_fcm.setup_cli")
    stub.connect = lambda token, url=None, restart=True, from_stdin=False: (
        seen.update(from_stdin=from_stdin) or 0
    )
    monkeypatch.setitem(sys.modules, "hermes_filament_fcm.setup_cli", stub)

    cli._handler(_parse(["connect", "-p"]))
    assert seen == {"from_stdin": True}


@pytest.mark.parametrize("code", [0, 1, 2])
def test_handler_propagates_failure_codes(monkeypatch, code):
    stub = types.ModuleType("hermes_filament_fcm.setup_cli")
    stub.connect = lambda token, url=None, restart=True, from_stdin=False: code
    monkeypatch.setitem(sys.modules, "hermes_filament_fcm.setup_cli", stub)
    assert cli._handler(_parse(["connect", "fmcp_abc"])) == code
