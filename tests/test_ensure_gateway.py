"""After a sign-in, ``hermes filament login`` leaves a gateway running.

A fresh Hermes has no gateway service, and the agent that started the login
in the background has finished its turn by the time the sign-in completes,
so the login itself installs and starts the service, or restarts a gateway
that is already up. Every child is detached so it outlives the login.

``_ensure_gateway`` is loaded standalone via AST (setup_cli's imports need
Hermes) with the subprocess calls injected.
"""

import ast
import subprocess
import time
import types
from pathlib import Path

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)


def _load(printed):
    ns = {
        "subprocess": subprocess,
        "time": time,
        "print_info": lambda text: printed.append(("info", text)),
        "print_success": lambda text: printed.append(("success", text)),
        "print_warning": lambda text: printed.append(("warning", text)),
    }
    tree = ast.parse(_SETUP_CLI.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_gateway":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    ns["_ALREADY_INSTALLED"] = "already installed"
    ns["_GATEWAY_INSTALL_TIMEOUT_S"] = 1
    # The restart is polled until this deadline; a poll() of None is a
    # gateway still running in the foreground.
    ns["_GATEWAY_RESTART_WAIT_S"] = 0.05
    return ns["_ensure_gateway"]


class _Host:
    """A Hermes CLI whose `gateway install --if-missing` answers in turn."""

    def __init__(self, installs=(), restart_code=0, running=None):
        self.installs = list(installs)
        self.restart_code = restart_code
        self.running = running
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(("run", argv[1:], kwargs))
        code, out = self.installs.pop(0)
        if isinstance(code, Exception):
            raise code
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    def popen(self, argv, **kwargs):
        self.calls.append(("popen", argv[1:], kwargs))
        return types.SimpleNamespace(poll=lambda: self.restart_code)


def _ensure(host, printed):
    return _load(printed)(
        hermes=["hermes"],
        run=host.run,
        popen=host.popen,
        running_pid=lambda: host.running,
        sleep=lambda _s: None,
    )


_INSTALLED = (0, "✓ Gateway service already installed\n")


def test_fresh_hermes_gets_a_service_installed_and_started():
    printed = []
    host = _Host(installs=[(0, "✓ Service installed and loaded\n"), _INSTALLED])
    assert _ensure(host, printed) == "installed"
    kind, argv, kwargs = host.calls[0]
    assert kind == "run"
    assert argv == [
        "gateway",
        "install",
        "--if-missing",
        "--start-now",
        "--start-on-login",
    ]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert ("success", "Gateway installed and started.") in printed
    assert not [c for c in host.calls if c[0] == "popen"]


def test_a_running_gateway_is_restarted():
    printed = []
    host = _Host(running=4242)
    assert _ensure(host, printed) == "restarted"
    ((kind, argv, kwargs),) = host.calls
    assert (kind, argv) == ("popen", ["gateway", "restart"])
    assert kwargs["start_new_session"] is True
    assert ("success", "Gateway restarted.") in printed


def test_an_installed_but_stopped_service_is_restarted():
    printed = []
    host = _Host(installs=[_INSTALLED])
    assert _ensure(host, printed) == "restarted"
    assert [c[0] for c in host.calls] == ["run", "popen"]


def test_no_service_manager_falls_back_to_a_background_gateway():
    printed = []
    # A container: install explains itself and exits 0 without installing.
    no_backend = (0, "Service install is not supported in containers.\n")
    host = _Host(installs=[no_backend, no_backend], restart_code=None)
    assert _ensure(host, printed) == "background"
    assert host.calls[-1][1] == ["gateway", "restart"]
    assert ("success", "Gateway started in the background.") in printed


def test_a_failed_install_says_why_and_still_starts_one():
    printed = []
    host = _Host(installs=[(1, "✗ launchctl bootstrap failed: 5\n")])
    assert _ensure(host, printed) == "restarted"
    notes = [text for kind, text in printed if kind == "info"]
    assert any("launchctl bootstrap failed: 5" in note for note in notes)


def test_a_missing_hermes_cli_is_reported():
    printed = []
    host = _Host(installs=[(FileNotFoundError("hermes"), "")])

    def popen(argv, **kwargs):
        raise FileNotFoundError("hermes")

    host.popen = popen
    assert _ensure(host, printed) == "failed"
    assert any(kind == "warning" for kind, _ in printed)


def test_a_failing_restart_is_not_reported_as_success():
    printed = []
    host = _Host(running=1, restart_code=1)
    assert _ensure(host, printed) == "failed"
    assert not [p for p in printed if p[0] == "success"]
