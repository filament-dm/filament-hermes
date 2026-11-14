"""``hermes filament restart``: one way to restart the gateway on every host.

Hermes runs on hosts that restart a gateway in different ways, and a restart
that is right on one leaves another with no gateway at all:

- an **s6 slot** (Nous cloud, the official Docker image): the supervisor
  restarts it, and the CLI's ``gateway restart`` asks s6 to;
- a **service** Hermes installed (systemd, launchd, a Windows task): the same,
  through the service manager;
- a gateway some **other supervisor** runs (``gateway run
  --external-supervisor``, or a custom systemd unit that really owns it): the
  CLI hands it back to that supervisor;
- a named profile **served by the host gateway** (multiplexing): the CLI
  unserves and serves it again inside the host;
- an **unmanaged** gateway someone started with ``gateway run``, including
  Agent37's profile gateways. Nothing brings it back, so it is stopped and a
  new one started. Hermes believes a gateway that says a supervisor launched
  it, and a shell under systemd (a CI runner, a desktop terminal) passes on
  ``INVOCATION_ID``, so a hand-started gateway can say "systemd". Then the
  CLI's ``gateway restart`` hands it back to a supervisor that does not exist
  and leaves no gateway. Only a gateway a unit provably owns counts as
  supervised here;
- one a client **auto-started** for itself (``--idle-exit``): it ends when
  its client goes, so it is stopped and replaced;
- **no gateway** at all: one is started.

``look`` decides which, once, here. ``restart`` then waits for running turns
to finish (it never interrupts one: Hermes's ``gateway restart`` and ``stop``
refuse while a session is live, and a refusal sends it back to waiting), runs
the matching Hermes command, and checks that a new gateway process answers.
``ensure_running`` is the start-only half, for a gateway that has exited.

The plugin's other restart paths come through here: the sign-in hand-off
(``setup_cli._restart_gateway``), ``/fil-upgrade`` and Hermes's own
``/restart`` (``leave_restart_helper``, run in the gateway as it stops),
``install.sh`` (through the setup wizard) and Agent37's post-restart hook.

Every Hermes command runs as a direct child, never through a shell or an
agent's terminal tool, so Hermes's approval gate for shell commands that stop
or restart the gateway does not apply. Each child gets the profile pinned
(``HERMES_HOME``, plus ``-p default`` for a root home, as Hermes's own
``spawn_unmanaged_gateway`` does) and no supervisor markers: dropping
``INVOCATION_ID`` also drops Hermes's "supervised child" rule that pins the
root profile, and a root home would otherwise follow the sticky
``active_profile``.

Needs Hermes v0.22.3 or later (``gateway ensure``, the live-session refusal,
the control socket's ``active-work``). On an older Hermes, ``legacy_restart``
does what the sign-in did before: restart with retries, no waiting.

Stdlib-only at import. Hermes's modules are imported inside ``RealHost`` (and
``_proven_supervisor``), so tests load this file standalone.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MIN_HERMES = "v0.22.3"

# Host kinds (see the module docstring).
S6 = "s6"
SERVICE = "service"
SUPERVISED = "supervised"
SERVED = "served"
UNMANAGED = "unmanaged"
AUTO_STARTED = "auto-started"
NONE = "none"

# Kinds whose supervisor brings the gateway back: Hermes's `gateway restart`.
_RESTARTED_BY_HERMES = (S6, SERVICE, SUPERVISED, SERVED)
# Kinds nothing brings back: `gateway stop`, then start a new one.
_STOP_AND_START = (UNMANAGED, AUTO_STARTED)

# Environment that makes a gateway say a supervisor launched it, or marks a
# process as part of a running gateway. The detached helper and every Hermes
# command it runs drop these (Hermes's spawn_unmanaged_gateway drops the same
# supervisor markers). HERMES_S6_SUPERVISED_CHILD stays: without it
# `gateway run` in an s6 container redirects to s6.
SUPERVISOR_ENV = (
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "NOTIFY_SOCKET",
    "WATCHDOG_USEC",
    "WATCHDOG_PID",
    "XPC_SERVICE_NAME",
    "LAUNCHD_SOCKET",
    "HERMES_LAUNCHD_LABEL",
    "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    "HERMES_SUPERVISED_CHILD",
    "_HERMES_GATEWAY",
)

HELPER_LOG = "filament-gateway.log"

# Waits, in seconds.
WAIT_FOR_IDLE_S = 900  # running turns, before giving up (--wait)
IDLE_POLL_S = 3.0
LIFECYCLE_TIMEOUT_S = 180  # one `gateway restart` / `stop` / `install`
ENSURE_TIMEOUT_S = 60  # `gateway ensure --timeout`
STOP_WAIT_S = 45  # the stopped gateway's process to exit
UP_WAIT_S = 90  # a new gateway to answer
START_RETRIES = 2  # more starts when none answers
SUPERVISOR_GRACE_S = 20  # a supervisor to bring a gateway back by itself
# The refusal Hermes prints when a session is live (gateway_lifecycle_guards).
_REFUSED = "live session"
_ALREADY_INSTALLED = "already installed"
# What Hermes says when a system-level unit's restart needs root.
_NEEDS_ROOT = "requires root"


class Say:
    """Where progress goes: the setup wizard's styled printers, or plain text
    in the helper's log."""

    def __init__(self, info=None, success=None, warning=None):
        self.info = info or (lambda text: print(f"  {text}", flush=True))
        self.success = success or (lambda text: print(f"✓ {text}", flush=True))
        self.warning = warning or (lambda text: print(f"⚠ {text}", flush=True))


# ── Profile and child processes ─────────────────────────────────────


def hermes_home() -> Path:
    """This profile's home, as Hermes resolved it for this process."""
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def profile_name(home: Path) -> str:
    """``<root>/profiles/<name>`` is profile *name*; anything else is the root."""
    return home.name if home.parent.name == "profiles" else "default"


def pin_args(home: Path) -> list[str]:
    """Global flags that pin *home*'s profile on a Hermes command.

    A ``<root>/profiles/<name>`` HERMES_HOME pins itself. A root one does not:
    Hermes then follows the sticky ``active_profile`` (unless the process
    carries a supervisor marker, which children here never do), so it gets
    ``-p default``, resolved against that same HERMES_HOME."""
    return [] if home.parent.name == "profiles" else ["-p", "default"]


def child_env(home: Path, environ=None) -> dict[str, str]:
    """Environment for a Hermes command run for *home*: its HERMES_HOME, no
    supervisor markers."""
    env = {
        k: v
        for k, v in (os.environ if environ is None else environ).items()
        if k not in SUPERVISOR_ENV
    }
    env["HERMES_HOME"] = str(home)
    return env


def hermes_argv() -> list[str]:
    """How to invoke the Hermes CLI: ``hermes`` on PATH, else this process's
    own entry script when it is one (an agent's terminal can lack the PATH
    entry the installer added)."""
    found = shutil.which("hermes")
    if found:
        return [found]
    script = sys.argv[0] if sys.argv else ""
    if os.path.basename(script).startswith("hermes") and os.path.isfile(script):
        return [script]
    return ["hermes"]


def last_line(out: str) -> str:
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def ensure_verdict(out: str) -> str:
    """``state``/``reason_code`` from ``gateway ensure``'s JSON line, for a
    message."""
    for raw in reversed((out or "").splitlines()):
        line = raw.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return "/".join(
                    str(data[k]) for k in ("state", "reason_code") if data.get(k)
                )
    return last_line(out)


def ps_field(pid: int, field: str) -> str:
    """One ``ps -o`` field of *pid*, or "" (no ps, no such process)."""
    try:
        result = subprocess.run(
            ["ps", "-o", field, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (result.stdout or "").strip()


def ppid_of(pid: int) -> int | None:
    if pid == os.getpid():
        return os.getppid()
    out = ps_field(pid, "ppid=")
    return int(out) if out.isdigit() else None


def ancestor_pids(pid: int | None = None, *, ppid=None) -> list[int]:
    """The PIDs above *pid* (default: this process), nearest first, up to
    init."""
    ppid = ppid or ppid_of
    seen: list[int] = []
    current = os.getpid() if pid is None else pid
    for _ in range(64):
        parent = ppid(current)
        if parent is None or parent <= 1 or parent in seen:
            break
        seen.append(parent)
        current = parent
    return seen


def pid_alive(pid: int) -> bool:
    """Whether *pid* is a live process. A zombie is not: a gateway started
    with ``setsid nohup`` is reparented to PID 1, and a container whose PID 1
    does not reap (``sleep infinity``) keeps it as a zombie after it exits."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return not ps_field(pid, "stat=").startswith("Z")


def looks_like_gateway(command: str) -> bool:
    """Whether a process command line is a Hermes gateway (``hermes gateway
    run`` or ``python -m hermes_cli.main ... gateway run``)."""
    return "gateway run" in command and "hermes" in command


# ── What kind of host this is ────────────────────────────────────────


@dataclass(frozen=True)
class Probes:
    """What one look at the host found, for ``classify``."""

    pid: int | None = None  # this profile's running gateway
    s6_slot: str | None = None  # its live s6 slot
    service: str | None = None  # systemd / launchd / windows, for this home
    served_by_host: bool = False  # a named profile the host gateway serves
    idle_exit: bool = False  # the gateway runs with --idle-exit
    declared: str | None = None  # the supervisor the gateway says launched it
    owned: bool = False  # ...and that supervisor really runs it
    agent37: bool = False  # Agent37's image (informational)


@dataclass(frozen=True)
class Gateway:
    kind: str
    pid: int | None
    detail: str

    def describe(self) -> str:
        running = f"pid {self.pid}" if self.pid else "not running"
        return f"{self.detail} ({running})"


def classify(p: Probes) -> Gateway:
    """The host kind for these facts. Pure; ``RealHost.look`` gathers them."""
    on_agent37 = " on Agent37" if p.agent37 else ""
    if p.s6_slot:
        return Gateway(S6, p.pid, f"s6 slot {os.path.basename(p.s6_slot)}")
    if p.service:
        return Gateway(SERVICE, p.pid, f"{p.service} service")
    if p.served_by_host:
        return Gateway(SERVED, p.pid, "served by the host gateway")
    if p.pid is None:
        return Gateway(NONE, None, f"no gateway{on_agent37}")
    if p.idle_exit:
        return Gateway(AUTO_STARTED, p.pid, "auto-started by a client (--idle-exit)")
    if p.declared == "external" or (p.declared in ("systemd", "launchd") and p.owned):
        return Gateway(SUPERVISED, p.pid, f"run by {p.declared}")
    if p.declared and p.declared not in ("manual", "desktop"):
        return Gateway(
            UNMANAGED,
            p.pid,
            f"started by hand{on_agent37}; it says {p.declared} launched it, "
            "but nothing restarts it",
        )
    return Gateway(UNMANAGED, p.pid, f"started by hand{on_agent37}")


class RealHost:
    """The real Hermes behind ``restart``: facts from Hermes's own modules
    (this runs inside the Hermes CLI, or the setup wizard on Hermes's Python),
    lifecycle changes through the Hermes CLI."""

    def __init__(self, home: Path | None = None):
        self.home = (home or hermes_home()).expanduser()
        self.profile = profile_name(self.home)

    # time, so tests can run it fast
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def modern(self) -> bool:
        """Hermes has the commands ``restart`` uses (v0.22.3+)."""
        try:
            guards = importlib.import_module("hermes_cli.gateway_lifecycle_guards")
            runtime_cli = importlib.import_module("hermes_cli.gateway_runtime_cli")
        except Exception:
            return False
        return hasattr(guards, "live_work") and hasattr(
            runtime_cli, "cmd_gateway_ensure"
        )

    def _identify(self) -> dict | None:
        try:
            from gateway.control_socket import identify_gateway  # noqa: PLC0415

            answer = identify_gateway(self.home)
        except Exception:
            return None
        return answer if isinstance(answer, dict) else None

    def _running_pid(self) -> int | None:
        try:
            from gateway.status import get_running_pid  # noqa: PLC0415

            return get_running_pid()
        except Exception:
            return None

    def identity(self) -> tuple | None:
        """(pid, start time) of the gateway answering for this profile, or
        None. A restart is done once this changes."""
        answer = self._identify()
        if answer and answer.get("pid"):
            return (answer.get("pid"), answer.get("start_time"))
        host = self._host_serving()
        if host is not None:
            return (host or "host", None)
        pid = self._running_pid()
        return (pid, None) if pid else None

    def _s6_slot(self) -> str | None:
        for base in ("/run/service", "/run/hermes-service"):
            for name in (f"gateway-{self.profile}", f"hermes-gateway-{self.profile}"):
                slot = Path(base) / name
                control = slot / "supervise" / "control"
                try:
                    if control.is_fifo():
                        return str(slot)
                except OSError:
                    continue
        try:
            manager = importlib.import_module("hermes_cli.service_manager")
            if manager.detect_service_manager() == "s6":
                # s6-svscan is PID 1 but the slot is elsewhere: Hermes's own
                # `gateway restart` finds it.
                return f"gateway-{self.profile}"
        except Exception:
            pass
        return None

    def _service(self) -> str | None:
        try:
            from hermes_cli.gateway import _installed_service_kind  # noqa: PLC0415

            return _installed_service_kind()
        except Exception:
            return None

    def _host_serving(self) -> int | None:
        """The PID of the host gateway serving this named profile (Hermes
        multiplexes every profile into the default profile's gateway), or
        None. Such a profile has no gateway, control socket or PID file of
        its own; Hermes's `gateway stop` parks it instead of stopping anything."""
        if self.profile == "default":
            return None
        try:
            from hermes_cli import gateway as gw  # noqa: PLC0415

            owner = gw._served_by_another_host_gateway()
            if owner is not None:
                return getattr(owner, "pid", None) or 0
            if gw.named_profile_served_by_running_multiplexer():
                return 0
        except Exception:
            return None
        return None

    def _own(self, answer: dict | None) -> bool:
        """Whether an identify answer is this home's own gateway."""
        try:
            home = Path(str(answer.get("hermes_home") or "")).resolve()
            return bool(answer) and home == self.home.resolve()
        except Exception:
            return bool(answer)

    def look(self) -> Gateway:
        answer = self._identify()
        own = bool(answer) and self._own(answer)
        host = None if own else self._host_serving()
        if host is not None:
            return classify(Probes(pid=host or None, served_by_host=True))
        pid = (answer or {}).get("pid") or self._running_pid()
        declared = None
        owned = False
        if answer:
            try:
                from gateway.control_socket import launch_supervisor  # noqa: PLC0415

                declared = launch_supervisor(answer)
            except Exception:
                declared = answer.get("launch_supervisor") or answer.get("supervisor")
        if pid and declared == "systemd":
            owned = systemd_owns(pid)
        elif declared in ("launchd", "external"):
            # Only launchd sets an ai.hermes job label; --external-supervisor
            # is an explicit opt-in. Both mean a supervisor runs it.
            owned = True
        return classify(
            Probes(
                pid=pid,
                s6_slot=self._s6_slot(),
                service=self._service(),
                idle_exit=bool(pid) and "--idle-exit" in ps_field(pid, "command="),
                declared=declared,
                owned=owned,
                agent37=(Path.home() / ".agent37").is_dir(),
            )
        )

    def live_sessions(self) -> int | None:
        """Turns, cron runs and delegations the gateway is running for this
        profile; 0 when no gateway answers; None when this Hermes can't say."""
        try:
            from hermes_cli.gateway_lifecycle_guards import live_work  # noqa: PLC0415
        except Exception:
            return None
        try:
            work = live_work(self.home)
            if work is None and self.profile != "default":
                from hermes_constants import get_default_hermes_root  # noqa: PLC0415

                work = live_work(get_default_hermes_root(), self.profile)
        except Exception:
            return None
        if not work:
            return 0
        units = [u for u in work.get("active_work") or [] if isinstance(u, dict)]
        try:
            agents = int(work.get("active_agents") or 0)
        except (TypeError, ValueError):
            agents = 0
        return max(len(units), agents)

    def hermes(self, *args: str, timeout: float) -> tuple[int | None, str]:
        """Run ``hermes <pin> <args>`` for this profile; (exit code or None
        on error, output). Its own session, stdin closed: it survives this
        process, and Hermes's live-session check finds no terminal to ask on
        and refuses instead."""
        try:
            result = subprocess.run(
                [*hermes_argv(), *pin_args(self.home), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=child_env(self.home),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return None, str(exc)
        return result.returncode, f"{result.stdout or ''}\n{result.stderr or ''}"

    def gone(self, pid: int) -> bool:
        return not pid_alive(pid)

    def signal_restart(self, pid: int) -> bool:
        """SIGUSR1: the gateway drains, then exits 75 for its supervisor."""
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            return False
        return True

    def restart_detached(self, wait_s: float) -> int | None:
        """Start ``hermes gateway restart`` in the background (Hermes before
        v0.22.3). Its exit code, or None while it still runs at *wait_s*:
        with no service, it runs the new gateway in its own foreground."""
        try:
            child = subprocess.Popen(
                [*hermes_argv(), *pin_args(self.home), "gateway", "restart"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=child_env(self.home),
            )
        except OSError:
            return 127
        deadline = self.now() + wait_s
        while self.now() < deadline:
            code = child.poll()
            if code is not None:
                return code
            self.sleep(0.5)
        return None


def systemd_owns(pid: int) -> bool:
    """Whether a systemd unit's live MainPID is *pid*. A shell under a unit (a
    CI runner, gnome-terminal-server) passes INVOCATION_ID to every process it
    starts, but is not their supervisor: the unit's MainPID is the shell's
    service, not the gateway. Hermes's own check (``main_dashboard``)."""
    try:
        from hermes_cli.main_dashboard import _systemd_owner_for_pid  # noqa: PLC0415

        return _systemd_owner_for_pid(pid) is not None
    except Exception:
        return False


# ── Restarting ───────────────────────────────────────────────────────


def wait_for_idle(host, deadline: float, say: Say) -> bool:
    """Wait until the gateway runs no turn (twice in a row, a poll apart: a
    turn that just ended is often followed by another, such as the model
    reporting a finished background command). False at *deadline*."""
    announced = None
    quiet = 0
    while True:
        count = host.live_sessions()
        if count is None:  # this Hermes can't say; its refusal still guards
            return True
        if count == 0:
            quiet += 1
            if quiet >= 2:
                return True
        else:
            quiet = 0
            if count != announced:
                say.info(
                    f"Waiting for {count} running session(s) to finish before "
                    "restarting..."
                )
                announced = count
        if host.now() >= deadline:
            say.warning(
                f"{count} session(s) were still running, so the gateway was not "
                "restarted: a restart would have cut them off. Run "
                "`hermes filament restart` again once they finish."
            )
            return False
        host.sleep(IDLE_POLL_S)


def install_service(host) -> str:
    """``installed``, ``already``, or ``error: <why>``."""
    code, out = host.hermes(
        "gateway",
        "install",
        "--if-missing",
        "--start-now",
        "--start-on-login",
        timeout=LIFECYCLE_TIMEOUT_S,
    )
    if _ALREADY_INSTALLED in out.lower():
        return "already"
    if code != 0:
        return f"error: {last_line(out) or f'exit {code}'}"
    # `install` exits 0 on hosts it can't serve too (a container says so and
    # returns): ask again, which only an install answers "already installed".
    _code, again = host.hermes(
        "gateway",
        "install",
        "--if-missing",
        "--no-start-now",
        timeout=LIFECYCLE_TIMEOUT_S,
    )
    if _ALREADY_INSTALLED in again.lower():
        return "installed"
    return f"error: {last_line(out) or 'no service manager'}"


def start(host, say: Say, *, service: bool = False) -> None:
    """Start a gateway for this profile: as a service first when *service*
    (the sign-in: a fresh Hermes has none, and the agent that ran it has
    ended its turn, so nothing else would start one), else as Hermes's
    ``gateway ensure`` does: an installed service or s6 slot is started, and
    otherwise an unmanaged gateway is spawned."""
    if service:
        installed = install_service(host)
        if installed == "installed":
            say.info("Installed the gateway as a service.")
            return
        if installed != "already":
            say.info(
                f"Could not install the gateway as a service ({installed[7:]}); "
                "starting it in the background instead."
            )
    code, out = host.hermes(
        "gateway",
        "ensure",
        "--timeout",
        str(ENSURE_TIMEOUT_S),
        timeout=ENSURE_TIMEOUT_S + 30,
    )
    if code != 0:
        say.warning(f"`hermes gateway ensure` exited {code}: {ensure_verdict(out)}")
        if getattr(host, "profile", "default") != "default":
            # A named profile someone stopped is parked, and the host gateway
            # won't serve it again until `gateway start` unparks it.
            code, out = host.hermes("gateway", "start", timeout=LIFECYCLE_TIMEOUT_S)
            if code != 0:
                say.warning(f"`hermes gateway start` exited {code}: {last_line(out)}")


def is_new(now: tuple, before: tuple | None) -> bool:
    """Whether identity *now* is another process than *before*: another PID,
    or the same PID with another start time (both known)."""
    if before is None:
        return True
    if now[0] != before[0]:
        return True
    return bool(now[1] and before[1] and now[1] != before[1])


def came_up(host, before: tuple | None, wait_s: float = UP_WAIT_S) -> tuple | None:
    """The identity of a gateway that is not *before*, once one answers."""
    deadline = host.now() + wait_s
    while True:
        now = host.identity()
        if now is not None and is_new(now, before):
            return now
        if host.now() >= deadline:
            return None
        host.sleep(1.0)


def confirm(host, before: tuple | None, say: Say) -> str:
    """Check a new gateway answers; start one if none does. ``running`` or
    ``failed``."""
    now = came_up(host, before)
    for _ in range(START_RETRIES):
        if now is not None:
            break
        if host.identity() is None:
            say.info("No gateway is running; starting it again...")
            start(host, say)
        now = came_up(host, before)
    if now is not None:
        say.success(f"Gateway running (pid {now[0]}).")
        return "running"
    say.warning(
        "The gateway did not come up. Check: hermes gateway status "
        f"(log: {getattr(host, 'home', Path('~/.hermes'))}/logs/gateway.log)"
    )
    return "failed"


def restart(
    host,
    say: Say | None = None,
    *,
    service: bool = False,
    wait_s: float = WAIT_FOR_IDLE_S,
) -> str:
    """Restart this profile's gateway the way its host restarts gateways.

    ``running``, ``failed``, or ``busy`` (turns kept running past *wait_s*;
    nothing was restarted). *service*: start a gateway that isn't running,
    or replace an auto-started one, as a service when this host can run one
    (the sign-in)."""
    say = say or Say()
    if not host.modern():
        say.info(f"This Hermes predates {MIN_HERMES}; restarting the old way.")
        return legacy_restart(host, say, service=service)
    deadline = host.now() + wait_s
    gateway = host.look()
    say.info(f"Gateway: {gateway.describe()}.")
    before = host.identity()
    if gateway.kind == NONE or (gateway.pid is None and gateway.kind in (S6, SERVICE)):
        say.info("Starting the gateway...")
        start(host, say, service=service and gateway.kind == NONE)
        return confirm(host, before, say)

    verb = "restart" if gateway.kind in _RESTARTED_BY_HERMES else "stop"
    target = f"pid {gateway.pid}" if gateway.pid else f"profile {host.profile}"
    if gateway.kind == SERVED:
        target = f"profile {host.profile}, inside the host gateway"
    while True:
        if not wait_for_idle(host, deadline, say):
            return "busy"
        if verb == "restart":
            say.info(f"Restarting the gateway ({target})...")
        else:
            say.info(f"Stopping the gateway ({target}) to start a new one...")
        code, out = host.hermes("gateway", verb, timeout=LIFECYCLE_TIMEOUT_S)
        if _REFUSED not in out:
            break
        # A turn started between the last look and the command. Hermes
        # refused rather than interrupt it: wait for that one too.
        say.info("A session started in the meantime; waiting for it as well.")
        if host.now() >= deadline:
            say.warning(
                "Sessions kept starting, so the gateway was not restarted. Run "
                "`hermes filament restart` again later."
            )
            return "busy"
        host.sleep(IDLE_POLL_S)
    if verb == "restart" and gateway.pid and _NEEDS_ROOT in out:
        # A system-level unit (a container where Hermes won't install a user
        # one): only root may restart it through systemctl. The gateway runs
        # as this user, though, and SIGUSR1 is Hermes's own graceful restart
        # (drain, exit 75); the unit's RestartForceExitStatus=75 relaunches it.
        say.info(
            "Hermes can restart this service only as root; asking the gateway "
            "to restart itself for its supervisor instead."
        )
        if not host.signal_restart(gateway.pid):
            say.warning(
                "Could not signal the gateway. Restart it as root with "
                "`hermes gateway restart --system`."
            )
            return "failed"
    elif code != 0:
        say.warning(f"`hermes gateway {verb}` exited {code}: {last_line(out)}")

    if gateway.kind == SERVED:
        # The host gateway keeps its PID; Hermes confirms the profile is
        # served again itself.
        if code == 0:
            say.success("The host gateway serves this profile again.")
            return "running"
        return "failed"
    if verb == "stop":
        stop_by = host.now() + STOP_WAIT_S
        while gateway.pid and not host.gone(gateway.pid) and host.now() < stop_by:
            host.sleep(1.0)
        if gateway.pid and not host.gone(gateway.pid):
            say.warning(f"The gateway (pid {gateway.pid}) did not stop.")
            return "failed"
        start(host, say, service=service and gateway.kind == AUTO_STARTED)
    return confirm(host, before, say)


def ensure_running(
    host, say: Say | None = None, *, grace_s: float = SUPERVISOR_GRACE_S
) -> str:
    """Leave a gateway running without restarting one that runs: for a
    gateway that has just exited (a supervisor gets *grace_s* to bring it
    back first) and for a host that has just booted. ``running``/``failed``."""
    say = say or Say()
    deadline = host.now() + grace_s
    while True:
        now = host.identity()
        if now is not None:
            say.success(f"Gateway running (pid {now[0]}).")
            return "running"
        if host.now() >= deadline:
            break
        host.sleep(1.0)
    say.info("No gateway is running; starting it...")
    if not host.modern():
        return legacy_restart(host, say)
    start(host, say)
    return confirm(host, None, say)


# ── Hermes before v0.22.3 ────────────────────────────────────────────

_LEGACY_RESTART_WAIT_S = 45
_LEGACY_UP_WAIT_S = 30
_LEGACY_RETRIES = 3


def legacy_restart(host, say: Say, *, service: bool = False) -> str:
    """The sign-in's restart before this module, for Hermes without ``gateway
    ensure`` or the live-session refusal: install or start a service, or run
    ``hermes gateway restart`` detached (with no gateway it runs one in the
    foreground, which is fine detached), then retry until a gateway answers
    under a new PID. ``running`` or ``failed``."""

    def restart_detached() -> bool:
        code = host.restart_detached(_LEGACY_RESTART_WAIT_S)
        if code is None:
            return True  # still running: a foreground gateway, left be
        if code != 0:
            say.warning(f"`hermes gateway restart` exited {code}.")
        return code == 0

    def up(previous):
        deadline = host.now() + _LEGACY_UP_WAIT_S
        while True:
            now = host.identity()
            if now is not None and now[0] != previous:
                return now[0]
            if host.now() >= deadline:
                return None
            host.sleep(1.0)

    gateway = host.look()
    previous = gateway.pid
    if previous is not None and not (service and gateway.kind == AUTO_STARTED):
        say.info(f"Restarting the gateway (pid {previous})...")
        restart_detached()
    else:
        if previous is not None:
            say.info(
                f"Replacing the chat's auto-started gateway (pid {previous}) "
                "with the gateway service..."
            )
            host.hermes("gateway", "stop", timeout=LIFECYCLE_TIMEOUT_S)
        say.info("Starting the gateway...")
        installed = install_service(host) if service else "error: not asked"
        if installed == "installed":
            say.info("Installed the gateway as a service.")
        elif installed == "already":
            code, out = host.hermes("gateway", "start", timeout=LIFECYCLE_TIMEOUT_S)
            if code != 0:
                say.warning(f"`hermes gateway start` failed: {last_line(out)}")
        else:
            if service:
                say.info(
                    f"Could not install the gateway as a service ({installed[7:]}); "
                    "starting it in the background instead."
                )
            restart_detached()
    pid = up(previous)
    for _ in range(_LEGACY_RETRIES):
        if pid is not None:
            break
        if host.identity() is None:
            say.info("No gateway is running; starting it again...")
        else:
            say.info("The gateway is not up yet; restarting it once more...")
        restart_detached()
        pid = up(previous)
    if pid is not None:
        say.success(f"Gateway running (pid {pid}).")
        return "running"
    say.warning(
        "The gateway did not come up. Check: hermes gateway status "
        f"(log: {host.home}/logs/gateway.log)"
    )
    return "failed"


# ── Running outside the gateway ──────────────────────────────────────


def inside_gateway(host, *, ancestors=None, command=None) -> bool:
    """Whether this process runs inside the gateway it would restart.

    Hermes's chat runs in the gateway since its unified runtime, so an
    agent's terminal tool is a child of it, and a restart from there kills
    this process with it (the stop sweeps its tool processes' groups and
    descendants). Also when no PID is known (an s6 slot without a PID file):
    an ancestor that is a Hermes gateway counts."""
    ancestors = ancestor_pids() if ancestors is None else ancestors
    command = command or (lambda pid: ps_field(pid, "command="))
    now = host.identity()
    if now is not None and now[0] in ancestors:
        return True
    return any(looks_like_gateway(command(pid)) for pid in ancestors)


def schedule(
    home: Path, flags: list[str], *, after_pid: int, log: Path | None = None
) -> Path | None:
    """Start ``hermes filament restart <flags> --after-pid <after_pid>``
    outside this process tree; its log path, or None if it could not start.

    ``sh`` backgrounds it and exits at once, so the helper is reparented to
    init/launchd, in a session of its own: neither the gateway's
    process-group kill nor its descendant sweep reaches it. POSIX only."""
    if os.name != "posix":
        return None
    log = log or home / "logs" / HELPER_LOG
    argv = [*hermes_argv(), *pin_args(home), "filament", "restart", *flags]
    argv += ["--after-pid", str(after_pid)]
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["/bin/sh", "-c", 'nohup "$@" >> "$0" 2>&1 < /dev/null &', str(log), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            timeout=10,
            check=True,
            env=child_env(home),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return log


def stamp(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)


def run(
    *,
    after_pid: int | None = None,
    after_timeout: float = 120,
    settle: float = 0,
    service: bool = False,
    only_start: bool = False,
    grace_s: float = SUPERVISOR_GRACE_S,
    wait_s: float = WAIT_FOR_IDLE_S,
    handoff_settle: float = 0,
    handoff_notice: str | None = None,
    say: Say | None = None,
    host=None,
) -> int:
    """``hermes filament restart``. Returns an exit code.

    From inside the gateway it re-runs itself outside it (``schedule``, with
    ``--settle=<handoff_settle>``) and returns at once; that copy waits for
    this one to exit (``after_pid``), then for the gateway's turns, including
    the one that ran this."""
    say = say or Say()
    host = host or RealHost()
    if after_pid is None and inside_gateway(host):
        flags = [f"--wait={int(wait_s)}"]
        if service:
            flags.append("--install-service")
        if only_start:
            flags.append("--ensure")
        if handoff_settle:
            flags.append(f"--settle={int(handoff_settle)}")
        log = schedule(host.home, flags, after_pid=os.getpid())
        if log is None:
            say.warning(
                "Could not start the restart outside the gateway; restarting here."
            )
        else:
            say.info(
                handoff_notice
                or "This runs inside the Hermes gateway, so the restart runs "
                "outside it: once this turn ends, the gateway restarts and the "
                "chat reconnects by itself."
            )
            say.success(f"Gateway restart scheduled (log: {log}).")
            return 0
    if after_pid is not None:
        stamp(f"filament restart after pid {after_pid} ({host.home})")
        deadline = host.now() + after_timeout
        while not host.gone(after_pid) and host.now() < deadline:
            host.sleep(0.5)
    if settle:
        host.sleep(settle)
    if only_start:
        outcome = ensure_running(host, say, grace_s=grace_s)
    else:
        outcome = restart(host, say, service=service, wait_s=wait_s)
    if after_pid is not None:
        stamp(f"filament restart: {outcome}")
    return 0 if outcome == "running" else 1


# ── Inside the gateway: /fil-upgrade and Hermes's /restart ───────────


def proven_supervisor(
    environ=None, *, pid: int | None = None, argv=None, owns=None
) -> str | None:
    """What restarts THIS gateway process when it exits, or None.

    Hermes's own ``/restart`` exits for a supervisor whenever the process
    carries a supervisor marker or runs in a container, and both can be
    present with no supervisor: a gateway started by hand from a shell under
    systemd inherits INVOCATION_ID, and Agent37's profile gateways run in a
    container as plain background processes. Here a marker counts only with
    proof: systemd's when a unit's MainPID is this process; s6's when its
    slot is live; launchd's job label (only launchd sets one); the explicit
    ``--external-supervisor``; and a container only when this process is
    its main one (PID 1)."""
    env = os.environ if environ is None else environ
    pid = os.getpid() if pid is None else pid
    argv = sys.argv if argv is None else argv
    owns = owns or systemd_owns
    if env.get("HERMES_S6_SUPERVISED_CHILD"):
        home = Path(env.get("HERMES_HOME") or Path.home() / ".hermes")
        host = RealHost(home)
        if host._s6_slot():
            return "s6"
    if "--external-supervisor" in argv or str(
        env.get("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", "")
    ).strip().lower() in ("1", "true", "yes", "on"):
        return "external"
    for name in ("XPC_SERVICE_NAME", "HERMES_LAUNCHD_LABEL"):
        if str(env.get(name, "")).startswith("ai.hermes"):
            return "launchd"
    if env.get("INVOCATION_ID") and owns(pid):
        return "systemd"
    if pid == 1:
        return "container"
    return None


def leave_restart_helper(
    runner, *, environ=None, pid: int | None = None, schedule_fn=None
) -> str | None:
    """Called in the gateway as it stops. When the stop is a restart that
    exits for a supervisor and no supervisor is proven to exist, start
    ``hermes filament restart --ensure`` outside the gateway: once this
    process exits it waits ``SUPERVISOR_GRACE_S`` for a supervisor, then
    starts a gateway itself. Returns the helper's log path, or None when none
    was needed or it could not start.

    Covers ``/fil-upgrade`` and Hermes's ``/restart`` alike: both ask the
    runner for a restart (``request_restart``), and for a ``detached`` one
    Hermes leaves its own helper, so only the exit-for-a-supervisor case is
    ours."""
    if runner is None or not getattr(runner, "_restart_requested", False):
        return None
    if getattr(runner, "_restart_detached", False):
        return None
    if proven_supervisor(environ, pid=pid):
        return None
    pid = os.getpid() if pid is None else pid
    env = os.environ if environ is None else environ
    home = Path(env.get("HERMES_HOME") or Path.home() / ".hermes")
    schedule_fn = schedule_fn or schedule
    # One helper per stopping gateway: a host gateway serving several
    # profiles disconnects one Filament adapter per profile, each with its
    # own copy of this module, so the claim is a file.
    if not claim_helper(home, pid):
        return None
    # The drain before the exit can run long (agent.restart_after_turn_timeout,
    # 30 min by default) when this is called early; give it that and more.
    return schedule_fn(home, ["--ensure", "--after-timeout=2100"], after_pid=pid)


def claim_helper(home: Path, pid: int, *, now: float | None = None) -> bool:
    """True for the first caller per gateway PID (an hour-old claim is a
    reused PID's and is taken over)."""
    path = home / "logs" / f".filament-restart-helper-{pid}"
    now = time.time() if now is None else now
    with _HELPERS_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if now - path.stat().st_mtime > 3600:
                    os.utime(path, (now, now))
                    return True
            except OSError:
                pass
            return False
        except OSError:
            return True  # can't record it; a second helper is harmless


_HELPERS_LOCK = threading.Lock()
