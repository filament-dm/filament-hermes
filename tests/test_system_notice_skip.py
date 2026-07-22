"""ENG-645: the adapter must never spend a turn on a Filament system notice.

When someone is vouched into a loop, the Welcome channel gets an automatic
"X vouched for Y to join <loop>" announcement authored by @filament_god. The
product requirement is that agents don't respond to it at all, so
``_handle_push_message_turn`` skips a local-filament_god-authored message before
the wake-policy / media-note / breadcrumb work — no turn, no API call.

These tests prove the *gate* (not the wake policy) is what skips: the wake
policy is stubbed to always wake, and ``_wake`` is a recorder. The
``is_system_sender`` predicate itself is covered in ``test_reactive.py``.

Modules are loaded standalone (same pattern as ``test_media_notes``): importing
the package pulls in the Hermes ``gateway`` package, absent in a bare test env,
so ``firebase_messaging`` and the Hermes gateway modules are stubbed first.
"""

import asyncio
import importlib.util
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
    base_mod.MessageEvent = type("MessageEvent", (), {})
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


def _load_modules():
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
    return (
        sys.modules["hermes_filament_fcm.fcm_client"],
        sys.modules["hermes_filament_fcm.adapter"],
    )


fcm_client, adapter = _load_modules()

_HOST = "filament.example"
_AGENT = f"@d_agent:{_HOST}"


class _AlwaysWakePolicy:
    """A wake policy that admits every message — so a skipped turn can only be
    the system-notice gate, never the policy."""

    def should_wake_message(self, room_id, is_mention):
        return True

    def reply_style(self, room_id):
        return "thread"


def _make_adapter():
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._user_id = _AGENT
    a._cc_room_id = None
    a._wake_policy = _AlwaysWakePolicy()
    a._filament_api = None
    a._is_new_event = lambda event_id: True
    a._is_control_channel = lambda room_id: False

    async def _no_media(msg):
        return None

    a._media_note = _no_media

    woke = []

    async def _record_wake(**kwargs):
        woke.append(kwargs)

    a._wake = _record_wake
    return a, woke


def _push_from(sender: str):
    return fcm_client.PushMessage(
        event_id="$evt",
        room_id="!welcome",
        room_name="Welcome",
        sender=sender,
        sender_display_name="Someone",
        body="Alice vouched for Bob to join the loop",
        is_direct=False,
        branch_type="channel_message",
        thread_id=None,
        is_mention=False,
        is_everyone_mention=False,
        raw={},
        has_content=True,
    )


def _run_turn(sender: str):
    a, woke = _make_adapter()
    asyncio.run(a._handle_push_message_turn(_push_from(sender), "turn-1"))
    return woke


def test_local_system_notice_is_skipped():
    """A @filament_god message from the agent's own homeserver never wakes."""
    woke = _run_turn(f"@filament_god:{_HOST}")
    assert woke == []


def test_ordinary_message_still_wakes():
    """Regression guard: a normal sender still wakes the agent."""
    woke = _run_turn(f"@alice:{_HOST}")
    assert len(woke) == 1
    assert woke[0]["sender"] == f"@alice:{_HOST}"


def test_impersonating_foreign_god_still_wakes():
    """A federated/impersonating @filament_god on another homeserver is not
    trusted as system, so it is NOT skipped (fail-closed)."""
    woke = _run_turn("@filament_god:evil.example")
    assert len(woke) == 1
    assert woke[0]["sender"] == "@filament_god:evil.example"
