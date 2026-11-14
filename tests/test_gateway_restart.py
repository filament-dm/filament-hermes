"""``hermes filament restart`` (``gateway_restart.py``): one restart for every
kind of Hermes host.

The module is stdlib-only at import, so it loads standalone. Hermes itself is
a small simulated host (``_Host``): what kind of gateway runs, the turns it is
running, and how its CLI answers ``gateway restart`` / ``stop`` / ``ensure``
/ ``install``, including the refusal Hermes prints while a session is live.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent
    / "hermes_filament_fcm"
    / "gateway_restart.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("gateway_restart_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gr = _load()

REFUSAL = (
    "Refusing to restart the gateway: 1 live session(s) would be interrupted.\n"
    "  Let them finish, or interrupt them deliberately:"
)


class _Host:
    """A Hermes host. ``kind`` is what ``look`` reports; the CLI acts on it.

    ``sessions``: live-session counts, one per poll (the last repeats).
    ``refusals``: how many lifecycle commands Hermes refuses for a session
    the poll missed. ``service_ok``: `gateway install` can install a service.
    ``comes_back``: a supervisor (or `ensure`) brings a new process up."""

    def __init__(
        self,
        kind=gr.UNMANAGED,
        pid=100,
        sessions=(0,),
        refusals=0,
        service_ok=True,
        comes_back=True,
        modern=True,
        stuck=False,
    ):
        self.kind = kind
        self.pid = pid
        self.sessions = list(sessions)
        self.refusals = refusals
        self.service_ok = service_ok
        self.comes_back = comes_back
        self._modern = modern
        self.stuck = stuck
        self.calls = []
        self.clock = 0.0
        self.home = Path("/home/agent/.hermes")
        self.profile = "default"
        self.installed = kind == gr.SERVICE

    # seams
    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def modern(self):
        return self._modern

    def look(self):
        return gr.Gateway(self.kind, self.pid, f"simulated {self.kind}")

    def identity(self):
        return (self.pid, f"t{self.pid}") if self.pid else None

    def live_sessions(self):
        if len(self.sessions) > 1:
            return self.sessions.pop(0)
        return self.sessions[0]

    def gone(self, pid):
        return self.pid != pid

    def _new_process(self):
        if self.comes_back:
            self.pid = (self.pid or 100) + 1

    def hermes(self, *args, timeout):
        self.calls.append(" ".join(args))
        verb = args[1] if len(args) > 1 else ""
        if verb in ("restart", "stop") and (self.refusals or self.live_sessions()):
            self.refusals = max(0, self.refusals - 1)
            return 1, REFUSAL.replace("restart", verb)
        if verb == "restart":
            if self.kind in (gr.S6, gr.SERVICE, gr.SUPERVISED):
                self._new_process()
                return 0, ""
            if self.kind == gr.SERVED:
                return 0, "Profile 'work' served by the host gateway."
            # Hermes hands a gateway that claims a supervisor back to it,
            # and nothing relaunches it.
            self.pid = None
            return 1, "Supervisor did not relaunch the gateway"
        if verb == "stop":
            if not self.stuck:
                self.pid = None
            return 0, "Stopped gateway for this profile"
        if verb == "ensure":
            if self.pid is None:
                self._new_process()
            state = "ready" if self.pid else "absent"
            return (0 if self.pid else 5), f'{{"state":"{state}","reason_code":null}}'
        if verb == "install":
            if "--no-start-now" in args:
                return (
                    0,
                    "already installed" if self.installed else "no service manager",
                )
            if self.installed:
                return 0, "Gateway service already installed"
            if not self.service_ok:
                return 0, "Service install not supported in this container"
            self.installed = True
            self._new_process()
            return 0, "Installed"
        if verb == "start":
            self._new_process()
            return 0, ""
        return 2, f"unexpected: {args}"

    def signal_restart(self, pid):
        self.calls.append(f"SIGUSR1 {pid}")
        self._new_process()
        return True

    def restart_detached(self, wait_s):
        self.calls.append("legacy gateway restart")
        self._new_process()
        return 0


def _say(lines):
    return gr.Say(
        info=lambda t: lines.append(("info", t)),
        success=lambda t: lines.append(("success", t)),
        warning=lambda t: lines.append(("warning", t)),
    )


def _restart(host, **kwargs):
    lines = []
    outcome = gr.restart(host, _say(lines), **kwargs)
    return outcome, lines


# ── Which kind of host ───────────────────────────────────────────────


class TestClassify:
    def test_an_s6_slot_wins(self):
        p = gr.Probes(pid=7, s6_slot="/run/service/gateway-default", service="systemd")
        assert gr.classify(p).kind == gr.S6

    def test_an_installed_service(self):
        assert gr.classify(gr.Probes(pid=7, service="launchd")).kind == gr.SERVICE

    def test_an_installed_service_that_is_down(self):
        gateway = gr.classify(gr.Probes(service="systemd"))
        assert (gateway.kind, gateway.pid) == (gr.SERVICE, None)

    def test_no_gateway(self):
        assert gr.classify(gr.Probes()).kind == gr.NONE

    def test_a_client_auto_started_gateway(self):
        assert gr.classify(gr.Probes(pid=7, idle_exit=True)).kind == gr.AUTO_STARTED

    def test_a_hand_started_gateway(self):
        gateway = gr.classify(gr.Probes(pid=7, declared="manual"))
        assert gateway.kind == gr.UNMANAGED
        assert gateway.detail == "started by hand"

    def test_a_gateway_that_only_claims_systemd_is_unmanaged(self):
        # The CI case: `hermes gateway run` from a shell under a systemd unit
        # inherits INVOCATION_ID, but no unit runs the gateway.
        gateway = gr.classify(gr.Probes(pid=7, declared="systemd", owned=False))
        assert gateway.kind == gr.UNMANAGED
        assert "says systemd launched it" in gateway.detail

    def test_a_gateway_a_unit_really_runs_is_supervised(self):
        gateway = gr.classify(gr.Probes(pid=7, declared="systemd", owned=True))
        assert gateway.kind == gr.SUPERVISED

    def test_an_external_supervisor_is_supervised(self):
        assert gr.classify(gr.Probes(pid=7, declared="external")).kind == gr.SUPERVISED

    def test_agent37_is_named(self):
        gateway = gr.classify(gr.Probes(pid=7, declared="manual", agent37=True))
        assert gateway.kind == gr.UNMANAGED
        assert "Agent37" in gateway.detail

    def test_a_profile_the_host_gateway_serves(self):
        # It has no gateway of its own: `gateway stop` would park it.
        gateway = gr.classify(gr.Probes(pid=12707, served_by_host=True))
        assert (gateway.kind, gateway.pid) == (gr.SERVED, 12707)


# ── Profile pinning and environment ─────────────────────────────────


class TestPinning:
    def test_a_root_home_is_pinned_with_p_default(self):
        assert gr.pin_args(Path("/opt/data")) == ["-p", "default"]
        assert gr.profile_name(Path("/opt/data")) == "default"

    def test_a_profile_home_pins_itself(self):
        home = Path("/home/a/.hermes/profiles/work")
        assert gr.pin_args(home) == []
        assert gr.profile_name(home) == "work"

    def test_children_carry_no_supervisor_markers(self):
        env = gr.child_env(
            Path("/h"),
            {
                "INVOCATION_ID": "abc",
                "JOURNAL_STREAM": "8:1",
                "XPC_SERVICE_NAME": "application.com.apple.Terminal",
                "_HERMES_GATEWAY": "1",
                "HERMES_S6_SUPERVISED_CHILD": "1",
                "PATH": "/bin",
                "HERMES_HOME": "/elsewhere",
            },
        )
        assert env == {
            "HERMES_S6_SUPERVISED_CHILD": "1",
            "PATH": "/bin",
            "HERMES_HOME": "/h",
        }


# ── Restarting each kind ─────────────────────────────────────────────


class TestRestart:
    def test_a_service_waits_for_turns_then_restarts_through_hermes(self):
        host = _Host(kind=gr.SERVICE, sessions=(2, 1, 0, 0))
        outcome, lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["gateway restart"]
        assert (
            "info",
            "Waiting for 2 running session(s) to finish before restarting...",
        ) in lines
        assert lines[-1] == ("success", "Gateway running (pid 101).")

    def test_an_s6_slot_restarts_through_hermes(self):
        host = _Host(kind=gr.S6)
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway restart"]

    def test_a_down_s6_slot_is_started(self):
        # `restart` on a down slot (s6-svc -t) does nothing; ensure starts it.
        host = _Host(kind=gr.S6, pid=None)
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway ensure --timeout 60"]

    def test_a_hand_started_gateway_is_stopped_and_started_again(self):
        host = _Host(kind=gr.UNMANAGED)
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway stop", "gateway ensure --timeout 60"]

    def test_a_gateway_that_claims_a_supervisor_never_gets_gateway_restart(self):
        # `gateway restart` would hand it to a supervisor that isn't there
        # (exit 1, no gateway). Stop + ensure starts a new one.
        host = _Host(kind=gr.UNMANAGED)
        _restart(host)
        assert "gateway restart" not in host.calls

    def test_a_refused_command_waits_and_tries_again(self):
        # The poll saw no session, but one started before the command ran.
        host = _Host(kind=gr.SERVICE, refusals=1)
        outcome, lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["gateway restart", "gateway restart"]
        assert (
            "info",
            "A session started in the meantime; waiting for it as well.",
        ) in lines

    def test_turns_that_never_end_leave_the_gateway_alone(self):
        host = _Host(kind=gr.SERVICE, sessions=(1,))
        outcome, lines = _restart(host, wait_s=30)
        assert outcome == "busy"
        assert host.calls == []
        assert host.pid == 100
        assert lines[-1][0] == "warning"
        assert "not restarted" in lines[-1][1]

    def test_no_gateway_with_a_service_manager_gets_a_service(self):
        host = _Host(kind=gr.NONE, pid=None)
        outcome, lines = _restart(host, service=True)
        assert outcome == "running"
        assert host.calls[0].startswith("gateway install --if-missing --start-now")
        assert ("info", "Installed the gateway as a service.") in lines

    def test_no_gateway_and_no_service_manager_starts_one_in_the_background(self):
        host = _Host(kind=gr.NONE, pid=None, service_ok=False)
        outcome, lines = _restart(host, service=True)
        assert outcome == "running"
        assert host.calls[-1] == "gateway ensure --timeout 60"
        assert any("starting it in the background instead" in t for _k, t in lines)

    def test_no_gateway_without_the_sign_in_is_just_started(self):
        host = _Host(kind=gr.NONE, pid=None)
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway ensure --timeout 60"]

    def test_the_sign_in_replaces_an_auto_started_gateway_with_a_service(self):
        host = _Host(kind=gr.AUTO_STARTED)
        assert _restart(host, service=True)[0] == "running"
        assert host.calls[0] == "gateway stop"
        assert host.calls[1].startswith("gateway install")

    def test_a_plain_restart_replaces_an_auto_started_gateway_with_a_lasting_one(self):
        host = _Host(kind=gr.AUTO_STARTED)
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway stop", "gateway ensure --timeout 60"]

    def test_a_gateway_that_will_not_stop_is_reported(self):
        host = _Host(kind=gr.UNMANAGED, stuck=True)
        outcome, lines = _restart(host)
        assert outcome == "failed"
        assert ("warning", "The gateway (pid 100) did not stop.") in lines
        assert "gateway ensure --timeout 60" not in host.calls

    def test_a_served_profile_is_restarted_inside_the_host_gateway(self):
        host = _Host(kind=gr.SERVED, pid=12707)
        host.profile = "work"
        assert _restart(host)[0] == "running"
        assert host.calls == ["gateway restart"]

    def test_a_supervisor_that_does_not_bring_it_back_gets_a_start(self):
        host = _Host(kind=gr.SERVICE)

        def restart_but_nothing_comes_back(*args, timeout):
            host.calls.append(" ".join(args))
            if args[1] == "restart":
                host.pid = None
                return 0, ""
            host.pid = 300
            return 0, '{"state":"ready"}'

        host.hermes = restart_but_nothing_comes_back
        outcome, lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["gateway restart", "gateway ensure --timeout 60"]
        assert ("info", "No gateway is running; starting it again...") in lines

    def test_a_system_unit_this_user_cannot_restart_is_signalled(self):
        # Hermes prints this (exit 0) for a system-level unit run as non-root.
        host = _Host(kind=gr.SERVICE)

        def needs_root(*args, timeout):
            host.calls.append(" ".join(args))
            return 0, "System gateway restart requires root."

        host.hermes = needs_root
        outcome, _lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["gateway restart", "SIGUSR1 100"]

    def test_a_gateway_that_never_comes_up_is_reported(self):
        host = _Host(kind=gr.NONE, pid=None, comes_back=False)
        outcome, lines = _restart(host)
        assert outcome == "failed"
        assert lines[-1][0] == "warning"
        assert "did not come up" in lines[-1][1]

    def test_an_old_hermes_restarts_the_old_way(self):
        host = _Host(kind=gr.UNMANAGED, modern=False)
        outcome, lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["legacy gateway restart"]
        assert any(gr.MIN_HERMES in t for _k, t in lines)

    def test_an_old_hermes_retries_until_a_gateway_answers(self):
        host = _Host(kind=gr.UNMANAGED, modern=False)
        tries = []

        def first_restart_leaves_none(wait_s):
            tries.append(1)
            host.calls.append("legacy gateway restart")
            host.pid = None if len(tries) == 1 else 200
            return 1 if len(tries) == 1 else 0

        host.restart_detached = first_restart_leaves_none
        outcome, lines = _restart(host)
        assert outcome == "running"
        assert host.calls == ["legacy gateway restart", "legacy gateway restart"]
        assert ("info", "No gateway is running; starting it again...") in lines


class TestWaitForIdle:
    def test_one_quiet_poll_is_not_enough(self):
        # A finished turn is often followed at once by another (the model
        # reporting a background command that ended).
        host = _Host(sessions=(0, 1, 0, 0))
        assert gr.wait_for_idle(host, deadline=100, say=_say([]))
        assert host.sessions == [0]

    def test_a_hermes_that_cannot_say_does_not_wait(self):
        host = _Host()
        host.live_sessions = lambda: None
        assert gr.wait_for_idle(host, deadline=0, say=_say([]))


class TestEnsureRunning:
    def test_a_running_gateway_is_left_alone(self):
        host = _Host(kind=gr.SERVICE)
        assert gr.ensure_running(host, _say([])) == "running"
        assert host.calls == []

    def test_a_supervisor_gets_a_grace_period(self):
        host = _Host(kind=gr.NONE, pid=None)
        polls = []

        def identity():
            polls.append(host.clock)
            return (555, "t") if len(polls) > 3 else None

        host.identity = identity
        assert gr.ensure_running(host, _say([]), grace_s=20) == "running"
        assert host.calls == []

    def test_nobody_brings_it_back_so_it_is_started(self):
        host = _Host(kind=gr.NONE, pid=None)
        lines = []
        assert gr.ensure_running(host, _say(lines), grace_s=5) == "running"
        assert host.calls == ["gateway ensure --timeout 60"]
        assert lines[-1] == ("success", "Gateway running (pid 101).")


# ── Running outside the gateway ─────────────────────────────────────


class TestInsideGateway:
    def test_the_gateway_is_an_ancestor(self):
        host = _Host(pid=40)
        assert gr.inside_gateway(host, ancestors=[50, 40, 30], command=lambda p: "")

    def test_a_gateway_with_no_known_pid_is_found_by_its_command_line(self):
        host = _Host(pid=None)
        commands = {30: "/opt/hermes/.venv/bin/python -m hermes_cli.main gateway run"}
        assert gr.inside_gateway(
            host, ancestors=[50, 30], command=lambda p: commands.get(p, "")
        )

    def test_a_terminal_outside_it(self):
        host = _Host(pid=40)
        assert not gr.inside_gateway(host, ancestors=[50, 30], command=lambda p: "-zsh")

    def test_ancestors_walk_up_to_init(self):
        parents = {50: 40, 40: 30, 30: 1}
        assert gr.ancestor_pids(50, ppid=parents.get) == [40, 30]
        assert gr.ancestor_pids(7, ppid={7: 8, 8: 7}.get) == [8, 7]
        assert gr.ancestor_pids(9, ppid=lambda _pid: None) == []


class TestRun:
    def test_inside_the_gateway_it_hands_off_and_returns(self, monkeypatch):
        host = _Host(kind=gr.UNMANAGED, pid=40)
        scheduled = []
        monkeypatch.setattr(gr, "inside_gateway", lambda h: True)
        monkeypatch.setattr(
            gr,
            "schedule",
            lambda home, flags, after_pid: (
                scheduled.append((flags, after_pid)) or Path("/log")
            ),
        )
        lines = []
        code = gr.run(host=host, say=_say(lines), service=True, handoff_settle=20)
        assert code == 0
        assert host.calls == []
        assert scheduled == [
            (["--wait=900", "--install-service", "--settle=20"], os.getpid())
        ]
        assert ("success", "Gateway restart scheduled (log: /log).") in lines

    def test_the_handed_off_copy_waits_for_its_caller_then_restarts(self, monkeypatch):
        host = _Host(kind=gr.UNMANAGED, pid=40)
        monkeypatch.setattr(
            gr, "inside_gateway", lambda h: pytest.fail("checked again")
        )
        caller = {"alive": 3}

        def gone(pid):
            if pid == 999:
                caller["alive"] -= 1
                return caller["alive"] <= 0
            return host.pid != pid

        host.gone = gone
        assert gr.run(host=host, say=_say([]), after_pid=999, settle=20) == 0
        assert host.calls == ["gateway stop", "gateway ensure --timeout 60"]
        assert host.clock >= 21

    def test_ensure_mode_only_starts(self, monkeypatch):
        host = _Host(kind=gr.SERVICE)
        monkeypatch.setattr(gr, "inside_gateway", lambda h: False)
        assert gr.run(host=host, say=_say([]), only_start=True) == 0
        assert host.calls == []

    def test_a_failed_restart_exits_nonzero(self, monkeypatch):
        host = _Host(kind=gr.NONE, pid=None, comes_back=False)
        monkeypatch.setattr(gr, "inside_gateway", lambda h: False)
        assert gr.run(host=host, say=_say([])) == 1


# ── Inside the gateway: /fil-upgrade and Hermes's /restart ──────────


class TestProvenSupervisor:
    def _ask(
        self, env, pid=4321, argv=("hermes", "gateway", "run"), owns=lambda pid: False
    ):
        return gr.proven_supervisor(env, pid=pid, argv=list(argv), owns=owns)

    def test_invocation_id_alone_is_no_supervisor(self):
        # A shell under a systemd unit passed it on; the unit runs the shell.
        assert self._ask({"INVOCATION_ID": "abc"}) is None

    def test_a_unit_that_owns_the_process_is(self):
        assert (
            self._ask({"INVOCATION_ID": "abc"}, owns=lambda pid: pid == 4321)
            == "systemd"
        )

    def test_a_launchd_job_label(self):
        assert self._ask({"XPC_SERVICE_NAME": "ai.hermes.gateway"}) == "launchd"
        assert (
            self._ask({"HERMES_LAUNCHD_LABEL": "ai.hermes.gateway-work"}) == "launchd"
        )

    def test_a_terminal_apps_xpc_name_is_not(self):
        assert (
            self._ask({"XPC_SERVICE_NAME": "application.com.microsoft.VSCode.1"})
            is None
        )

    def test_the_explicit_external_supervisor(self):
        assert (
            self._ask({}, argv=("hermes", "gateway", "run", "--external-supervisor"))
            == "external"
        )
        assert self._ask({"HERMES_GATEWAY_EXTERNAL_SUPERVISOR": "1"}) == "external"

    def test_a_containers_main_process(self):
        assert self._ask({}, pid=1) == "container"

    def test_any_other_container_process_is_not(self):
        # Agent37's profile gateways: background processes in a container.
        assert self._ask({}, pid=812) is None


class _Runner:
    def __init__(self, requested=True, detached=False):
        self._restart_requested = requested
        self._restart_detached = detached


class TestLeaveRestartHelper:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path):
        self.home = str(tmp_path)

    def _leave(self, runner, env):
        env = {**env, "HERMES_HOME": self.home}
        scheduled = []
        log = gr.leave_restart_helper(
            runner,
            environ=env,
            pid=812,
            schedule_fn=lambda home, flags, after_pid: (
                scheduled.append((home, flags, after_pid)) or Path("/log")
            ),
        )
        return log, scheduled

    def test_an_unsupervised_restart_leaves_a_helper(self):
        log, scheduled = self._leave(_Runner(), {"INVOCATION_ID": "x"})
        assert log == Path("/log")
        assert scheduled == [
            (Path(self.home), ["--ensure", "--after-timeout=2100"], 812)
        ]

    def test_one_helper_per_gateway_however_many_adapters_stop(self):
        # A host gateway serving two profiles disconnects two adapters.
        assert self._leave(_Runner(), {})[0] == Path("/log")
        assert self._leave(_Runner(), {}) == (None, [])

    def test_an_hour_old_claim_is_a_reused_pids(self, tmp_path):
        assert gr.claim_helper(tmp_path, 812, now=1000.0)
        os.utime(tmp_path / "logs" / ".filament-restart-helper-812", (1000.0, 1000.0))
        assert not gr.claim_helper(tmp_path, 812, now=1100.0)
        assert gr.claim_helper(tmp_path, 812, now=1000.0 + 3601)

    def test_a_plain_stop_does_not(self):
        assert self._leave(_Runner(requested=False), {}) == (None, [])

    def test_a_detached_restart_has_hermess_own_helper(self):
        assert self._leave(_Runner(detached=True), {}) == (None, [])

    def test_a_supervised_one_does_not(self):
        env = {"XPC_SERVICE_NAME": "ai.hermes.gateway"}
        assert self._leave(_Runner(), env) == (None, [])

    def test_no_runner(self):
        assert self._leave(None, {}) == (None, [])


def test_ensure_verdict_reads_the_json_line():
    out = 'starting...\n{"endpoint":null,"reason_code":"deadline","state":"starting"}\n'
    assert gr.ensure_verdict(out) == "starting/deadline"
    assert gr.ensure_verdict("plain failure\n") == "plain failure"


def test_looks_like_gateway():
    assert gr.looks_like_gateway(
        "/usr/bin/python3 /usr/local/bin/hermes gateway run --replace"
    )
    assert not gr.looks_like_gateway("hermes chat")
    assert not gr.looks_like_gateway("bash -lc 'echo gateway run'")
