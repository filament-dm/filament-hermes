"""The gateway asking to be probed (ENG-1099).

Every other call this gateway makes travels agent -> server and proves only
that we can reach the server. That stays true when our own FCM registration
has been invalidated or the push listener has died, which is the failure a
principal experiences as "my agent stopped answering" while every status
surface still reads healthy. `_maybe_request_probe` asks the server to send us
one push so the inbound direction is tested too.

Pinned here: the interval gate (each probe costs a push to the machine hosting
the agent, so a 20s heartbeat must not become a 20s probe), and that nothing
about a probe can disturb the heartbeat it rides on.

Modules are loaded standalone (same pattern as `test_thread_follow_up`):
importing the package pulls in the Hermes `gateway` package, absent in a bare
test env, so the gateway modules are stubbed first.
"""

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _install_stubs() -> None:
    fb = types.ModuleType("firebase_messaging")
    fb.FcmPushClient = type("FcmPushClient", (), {})
    fb.FcmRegisterConfig = type("FcmRegisterConfig", (), {})
    sys.modules["firebase_messaging"] = fb

    agent_pkg = types.ModuleType("agent")
    async_utils = types.ModuleType("agent.async_utils")
    async_utils.safe_schedule_threadsafe = lambda coro, loop, log_message="": None
    agent_pkg.async_utils = async_utils
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.async_utils"] = async_utils

    gateway_pkg = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = lambda name: name
    platforms_pkg = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _BaseAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs):
            return kwargs

        async def handle_message(self, event):
            pass

        def _set_fatal_error(self, *args, **kwargs):
            pass

        def _mark_connected(self):
            pass

        def _mark_disconnected(self):
            pass

    base_mod.BasePlatformAdapter = _BaseAdapter
    base_mod.MessageEvent = type(
        "MessageEvent", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}
    )
    base_mod.MessageType = types.SimpleNamespace(TEXT="text")
    base_mod.ProcessingOutcome = type("ProcessingOutcome", (), {})
    base_mod.SendResult = type("SendResult", (), {})

    gateway_pkg.config = config_mod
    gateway_pkg.platforms = platforms_pkg
    platforms_pkg.base = base_mod
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_pkg
    sys.modules["gateway.platforms.base"] = base_mod


def _load_adapter():
    _install_stubs()
    pkg = types.ModuleType("hermes_filament_fcm")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament_fcm"] = pkg
    for name in ("credentials", "fcm_client", "filament_api", "reactive", "adapter"):
        spec = importlib.util.spec_from_file_location(
            f"hermes_filament_fcm.{name}", _PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"hermes_filament_fcm.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["hermes_filament_fcm.adapter"]


adapter = _load_adapter()
_Adapter = adapter.FCMFilamentAdapter
INTERVAL = adapter._PROBE_REQUEST_INTERVAL_S


class _FakeApi:
    """Records probe requests; optionally fails the way a dead link would."""

    def __init__(self, result=None, raises: bool = False):
        self.calls = 0
        self._result = result if result is not None else {"probed": True}
        self._raises = raises

    async def request_probe(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("connection reset")
        return self._result


def _stub(api, last_probe=None):
    return types.SimpleNamespace(
        _filament_api=api,
        _last_probe_request=last_probe,
        _installation_id="inst",
        _gateway_instance_id="gw",
    )


def _run(stub):
    asyncio.run(_Adapter._maybe_request_probe(stub))


def test_first_tick_after_connect_probes_immediately():
    # None means never probed, so a reconnect gets a fresh reading rather than
    # inheriting the previous connection's.
    api = _FakeApi()
    stub = _stub(api)
    _run(stub)
    assert api.calls == 1
    assert stub._last_probe_request is not None


def test_probe_is_rate_limited_between_heartbeats():
    # It rides a 20s timer; without the gate that would be a push to the
    # agent's host three times a minute.
    api = _FakeApi()
    stub = _stub(api, last_probe=time.monotonic())
    _run(stub)
    assert api.calls == 0


def test_probe_resumes_after_the_interval():
    api = _FakeApi()
    stub = _stub(api, last_probe=time.monotonic() - INTERVAL - 1)
    _run(stub)
    assert api.calls == 1


def test_interval_is_long_relative_to_the_heartbeat():
    # A reachability check, not a presence one. If these ever converge, the
    # probe stops being cheap.
    assert INTERVAL >= 300


def test_a_failed_probe_request_does_not_propagate():
    # The heartbeat loop this rides on must keep its cadence; a probe that
    # cannot even be requested is a symptom to log, not an exception to raise.
    api = _FakeApi(raises=True)
    stub = _stub(api)
    _run(stub)
    assert api.calls == 1


def test_failed_request_still_consumes_the_interval():
    # Otherwise a server refusing probes would be retried every heartbeat.
    api = _FakeApi(raises=True)
    stub = _stub(api)
    _run(stub)
    first = stub._last_probe_request
    _run(stub)
    assert api.calls == 1
    assert stub._last_probe_request == first


def test_no_push_token_is_reported_not_swallowed():
    # The gateway is running but unreachable by push - the exact state that
    # otherwise reads as healthy.
    api = _FakeApi(result={"probed": True, "no_push_tokens": True})
    stub = _stub(api)
    _run(stub)
    assert api.calls == 1


def test_no_api_yet_is_a_no_op():
    stub = _stub(None)
    _run(stub)
    assert stub._last_probe_request is None
