"""The reaction wake path drops only the agent's own reactions.

Any other user's reaction - including one with an emoji the agent itself
might react with - is judged by the channel's wake policy alone. The adapter
posts no marker of its own on the messages it handles, so there is no
reaction key that is skipped before the policy is consulted.

Modules are loaded standalone (same pattern as ``test_system_notice_skip``):
importing the package pulls in the Hermes ``gateway`` package, absent in a
bare test env, so ``firebase_messaging`` and the gateway modules are stubbed.
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

    if "hermes_cli.setup" not in sys.modules:
        hermes_cli_pkg = types.ModuleType("hermes_cli")
        setup_mod = types.ModuleType("hermes_cli.setup")
        for fn in (
            "get_env_value",
            "print_header",
            "print_info",
            "print_success",
            "print_warning",
            "prompt",
            "prompt_yes_no",
            "remove_env_value",
            "save_env_value",
        ):
            setattr(setup_mod, fn, lambda *a, **k: None)
        hermes_cli_pkg.setup = setup_mod
        sys.modules["hermes_cli"] = hermes_cli_pkg
        sys.modules["hermes_cli.setup"] = setup_mod


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
_EYES = "👀"


class _EyesTriggerPolicy:
    """A wake policy where 👀 is the only configured reaction trigger."""

    def should_wake_reaction(self, room_id, key):
        return key == _EYES


class _NoopServerConfig:
    async def sync(self, *, force=False):
        return None


def _make_adapter():
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._user_id = _AGENT
    a._wake_policy = _EyesTriggerPolicy()
    a._server_config = _NoopServerConfig()
    a._is_new_event = lambda event_id: True
    a._is_control_channel = lambda room_id: False
    a._shared_sessions_effective = lambda: True

    woke = []

    async def _record_wake(**kwargs):
        woke.append(kwargs)

    a._wake = _record_wake
    return a, woke


def _reaction(sender: str, key: str = _EYES):
    return fcm_client.ReactionMessage(
        event_id="$reaction",
        room_id="!room",
        room_name="Room",
        sender=sender,
        sender_display_name="Someone",
        key=key,
        target_event_id="$target",
        removed=False,
        is_direct=False,
        thread_id=None,
        raw={},
    )


def _run_turn(sender: str, key: str = _EYES):
    a, woke = _make_adapter()
    asyncio.run(a._handle_reaction_turn(_reaction(sender, key), "turn-1"))
    return woke


def test_another_users_eyes_reaction_wakes_when_configured():
    """👀 is an ordinary emoji: with 👀 as a trigger, someone else's 👀 wakes."""
    woke = _run_turn(f"@alice:{_HOST}")
    assert len(woke) == 1
    assert woke[0]["trigger"] == f"{_EYES} reaction"
    assert woke[0]["target_event_id"] == "$target"


def test_the_agents_own_reaction_never_wakes():
    """The own-sender guard still holds, whatever emoji the agent used."""
    assert _run_turn(_AGENT) == []


def test_an_unconfigured_emoji_does_not_wake():
    """The wake policy, not a fixed skip list, decides which emoji wake."""
    assert _run_turn(f"@alice:{_HOST}", key="🎉") == []
