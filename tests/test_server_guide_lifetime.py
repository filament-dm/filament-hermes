"""The link-guide pointer lasts as long as the connection.

``_initialize_api`` writes the guide as a skill and records whether that
succeeded; every reactive turn reads that flag to decide whether the envelope
carries the one-line pointer. The flag therefore has to survive everything
between connect and the turns - including the first-contact greeting, which
runs immediately after initialize on exactly the agents this guidance is for.

Modules are loaded standalone (same pattern as ``test_thread_follow_up``):
importing the package pulls in the Hermes ``gateway`` package, absent in a bare
test env, so the gateway modules are stubbed first.
"""

import asyncio
import importlib.util
import inspect
import sys
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


def _greet_stub(guide_ready: bool):
    """The smallest object ``_maybe_greet`` can run against."""
    sent: list = []

    def build_source(**kw):
        return types.SimpleNamespace(**kw)

    posted: list = []

    class _Flags:
        """The hello names the commands that will run, so it asks the flag."""

        def is_enabled(self, _name):
            return False

    class _Api:
        """The canned hello goes out through the API, not a model turn."""

        async def post_message(self, channel, markdown_body):
            posted.append((channel, markdown_body))
            return {}

    stub = types.SimpleNamespace(
        _greet_pending=True,
        _cc_room_id="!backchannel:server",
        _server_guide_ready=guide_ready,
        _owner_id="@principal:server",
        _owner_name="Principal",
        _self_name="Agent",
        _wake_policy=types.SimpleNamespace(
            read_with_provenance=lambda: ({}, frozenset())
        ),
        _installation_id="inst",
        _gateway_instance_id="gw",
        _filament_api=_Api(),
        _feature_flags=_Flags(),
        build_source=build_source,
    )

    async def handle_message(event):
        sent.append(event)

    async def _greet_intro_turn():
        """Dispatched as its own task now; not what this test is about."""
        return None

    stub.handle_message = handle_message
    stub._greet_intro_turn = _greet_intro_turn
    return stub, sent, posted


def test_greeting_does_not_disable_the_link_pointer():
    stub, _sent, posted = _greet_stub(guide_ready=True)
    asyncio.run(_Adapter._maybe_greet(stub))

    # The greeting still happens, exactly once - the canned hello goes out
    # through the API; the capabilities intro is its own task and not the
    # subject here.
    assert len(posted) == 1
    assert stub._greet_pending is False
    # ...and the guidance written moments earlier is still on offer. Newly
    # connected agents are the one population that always greets, so clearing
    # this here would silence the pointer for the whole process life of the
    # agents the guide exists for.
    assert stub._server_guide_ready is True


def test_greeting_does_not_invent_a_pointer():
    # A connect where the skill could not be written stays off.
    stub, _sent, _posted = _greet_stub(guide_ready=False)
    asyncio.run(_Adapter._maybe_greet(stub))
    assert stub._server_guide_ready is False


def test_flag_is_declared_before_any_connect():
    # Every turn reads it as a plain attribute, so it exists from __init__
    # rather than being conjured by a successful initialize.
    body = inspect.getsource(_Adapter.__init__)
    assert "self._server_guide_ready: bool = False" in body
