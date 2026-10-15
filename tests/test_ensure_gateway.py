"""After a sign-in, ``hermes filament login`` leaves a gateway running.

A fresh Hermes has no gateway service, and the agent that started the login
in the background has finished its turn by the time the sign-in completes,
so the login itself installs and starts the service, starts an installed
one, or restarts a running one — and then checks that a gateway is actually
up under a new PID. Every child is detached so it outlives the login.

``_ensure_gateway`` is loaded standalone via AST (setup_cli's imports need
Hermes); the Hermes CLI is a small simulated host.
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
        "_ALREADY_INSTALLED": "already installed",
        "_GATEWAY_INSTALL_TIMEOUT_S": 1,
        "_GATEWAY_RESTART_WAIT_S": 1,
        # Polled with a no-op sleep, so these are loop bounds, not waits.
        "_GATEWAY_UP_WAIT_S": 0.02,
    }
    tree = ast.parse(_SETUP_CLI.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_gateway":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns["_ensure_gateway"]


class _Host:
    """A Hermes CLI over a service that is installed or not, running or not.

    ``supported``: a service manager exists. ``loaded_dead``: the service
    reports started but no process comes up (launchd in live run h9) until
    a restart."""

    def __init__(
        self, installed=False, running=False, supported=True, loaded_dead=False
    ):
        self.installed = installed
        self.pid = 100 if running else None
        self.supported = supported
        self.loaded_dead = loaded_dead
        self.calls = []

    def _spawn(self):
        self.pid = (self.pid or 100) + 1

    def run(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(("run", args, kwargs))
        out, code = "", 0
        if args[:2] == ["gateway", "install"]:
            if self.installed:
                out = "✓ Gateway service already installed"
            elif not self.supported:
                out = "Service install is not supported in containers."
            else:
                self.installed = True
                out = "✓ Service installed and loaded"
                if "--start-now" in args and not self.loaded_dead:
                    self._spawn()
        elif args[:2] == ["gateway", "start"] and not self.loaded_dead:
            self._spawn()
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")

    def popen(self, argv, **kwargs):
        self.calls.append(("popen", argv[1:], kwargs))
        self.loaded_dead = False
        self._spawn()
        return types.SimpleNamespace(poll=lambda: 0)


def _ensure(host, printed):
    return _load(printed)(
        hermes=["hermes"],
        run=host.run,
        popen=host.popen,
        running_pid=lambda: host.pid,
        sleep=lambda _s: None,
    )


def _verbs(host):
    return [" ".join(args[:2]) for _, args, _ in host.calls]


def test_fresh_hermes_gets_a_service_installed_and_started():
    printed = []
    host = _Host()
    assert _ensure(host, printed) == "running"
    kind, args, kwargs = host.calls[0]
    assert (kind, args) == (
        "run",
        ["gateway", "install", "--if-missing", "--start-now", "--start-on-login"],
    )
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "gateway restart" not in _verbs(host)
    assert ("info", "Installed the gateway as a service.") in printed
    assert ("success", "Gateway running (pid 101).") in printed


def test_a_running_gateway_is_restarted_and_must_come_back_new():
    printed = []
    host = _Host(installed=True, running=True)
    assert _ensure(host, printed) == "running"
    kind, args, kwargs = host.calls[0]
    assert (kind, args) == ("popen", ["gateway", "restart"])
    assert kwargs["start_new_session"] is True
    assert ("success", "Gateway running (pid 101).") in printed


def test_an_installed_but_stopped_service_is_started():
    printed = []
    host = _Host(installed=True)
    assert _ensure(host, printed) == "running"
    assert _verbs(host) == ["gateway install", "gateway start"]


def test_a_service_loaded_with_no_process_gets_one_restart():
    # Live run h9: launchd showed the service with no process.
    printed = []
    host = _Host(installed=True, loaded_dead=True)
    assert _ensure(host, printed) == "running"
    assert _verbs(host) == ["gateway install", "gateway start", "gateway restart"]
    assert ("success", "Gateway running (pid 101).") in printed


def test_no_service_manager_starts_one_in_the_background():
    printed = []
    host = _Host(supported=False)
    assert _ensure(host, printed) == "running"
    assert _verbs(host)[-1] == "gateway restart"
    assert any("starting it in the background" in t for _, t in printed)


def test_a_gateway_that_never_comes_up_is_reported():
    printed = []
    host = _Host(installed=True)
    host.popen = lambda argv, **kw: types.SimpleNamespace(poll=lambda: 0)
    host.loaded_dead = True
    assert _ensure(host, printed) == "failed"
    assert not [p for p in printed if p[0] == "success"]
    assert any("did not come up" in t for k, t in printed if k == "warning")


def test_a_missing_hermes_cli_is_reported():
    printed = []

    def run(argv, **kwargs):
        raise FileNotFoundError("hermes")

    def popen(argv, **kwargs):
        raise FileNotFoundError("hermes")

    assert (
        _load(printed)(
            hermes=["hermes"],
            run=run,
            popen=popen,
            running_pid=lambda: None,
            sleep=lambda _s: None,
        )
        == "failed"
    )
    assert any(kind == "warning" for kind, _ in printed)


def test_pid_from_status_output():
    ns = {"re": __import__("re")}
    tree = ast.parse(_SETUP_CLI.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_pid_from_status":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    pid_from = ns["_pid_from_status"]
    up = (
        "Launchd plist: /Users/x/Library/LaunchAgents/ai.hermes.gateway.plist\n"
        "\u2713 Service definition matches the current Hermes install\n"
        "\u2713 Gateway is supervised by launchd (PID 68581)\n"
    )
    assert pid_from(up) == 68581
    assert pid_from("\u2713 Gateway is running (PID: 42)") == 42
    assert pid_from("\u2717 Gateway is not running (last PID 9)") is None
    assert pid_from("") is None
