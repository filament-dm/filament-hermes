"""Every pushed message is marked read the moment it arrives.

The receipt is written before the wake gate and before any model turn, and
without waiting on it: a message the agent receives and then ignores still
reads as seen, and a slow or failing ``mark_read`` never holds up dispatch.

Modules are loaded standalone (same pattern as ``test_reaction_wake``):
importing the package pulls in the Hermes ``gateway`` package, absent in a
bare test env, so ``firebase_messaging`` and the Hermes gateway modules are
stubbed first.
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
_ALICE = f"@alice:{_HOST}"


class _WakePolicy:
    """Admits every message, or none, so the receipt can be checked on both
    sides of the gate."""

    def __init__(self, wake: bool):
        self._wake = wake

    def should_wake_message(self, room_id, is_mention):
        return self._wake

    def should_wake_reaction(self, room_id, key):
        return self._wake

    def reply_style(self, room_id):
        return "thread"

    def thread_wake(self, room_id):
        return "engaged" if self._wake else "off"


class _NoEngagedThreads:
    def is_engaged(self, room_id, thread_root_id):
        return False

    def record(self, room_id, thread_root_id):
        pass


class _NoopServerConfig:
    async def sync(self, *, force=False):
        return None


def _api(behaviour=None):
    """A real FilamentAPI whose transport records calls. *behaviour* runs on
    each mark_read call and may raise, block, or return a result."""
    api = adapter.FilamentAPI.__new__(adapter.FilamentAPI)
    api.calls = []

    async def _call_tool(name, arguments):
        api.calls.append((name, arguments))
        if name == "mark_read" and behaviour is not None:
            return await behaviour()
        return {"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}}

    api.call_tool = _call_tool
    return api


def _make_adapter(api, *, wake: bool = True, real_dedup: bool = False):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._user_id = _AGENT
    a._cc_room_id = None
    a._wake_policy = _WakePolicy(wake)
    a._feature_flags = types.SimpleNamespace(is_enabled=lambda name: False)
    a._seen_history = types.SimpleNamespace(
        get=lambda key: None, record=lambda *a, **k: None
    )
    a._engaged_threads = _NoEngagedThreads()
    a._sender_is_agent_cache = {}
    a._filament_api = api
    a._server_config = _NoopServerConfig()
    if real_dedup:
        a._seen_events = adapter.deque(maxlen=10)
        a._seen_set = set()
    else:
        a._is_new_event = lambda event_id: True
    a._is_control_channel = lambda room_id: False
    a._shared_sessions_effective = lambda: True

    async def _no_media(msg):
        return None

    a._media_note = _no_media

    woke = []

    async def _record_wake(**kwargs):
        woke.append(kwargs)

    a._wake = _record_wake
    return a, woke


def _push(sender=_ALICE, event_id="$evt", room_id="!room", thread_id=None):
    return fcm_client.PushMessage(
        event_id=event_id,
        room_id=room_id,
        room_name="Room",
        sender=sender,
        sender_display_name="Someone",
        body="hello",
        is_direct=False,
        branch_type="channel_message",
        thread_id=thread_id,
        is_mention=True,
        is_everyone_mention=False,
        raw={},
        has_content=True,
    )


async def _settle(a):
    """Let the in-flight arrival receipts finish."""
    tasks = list(getattr(a, "_receipt_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


def _run(a, *msgs):
    async def _go():
        for msg in msgs:
            await a._handle_push_message_turn(msg, "turn-1")
        await _settle(a)

    asyncio.run(_go())


def _mark_reads(api):
    return [args for name, args in api.calls if name == "mark_read"]


def test_arrival_marks_the_message_read_once():
    api = _api()
    a, woke = _make_adapter(api)
    _run(a, _push())
    assert _mark_reads(api) == [{"channel": "!room", "up_to": "$evt"}]
    assert len(woke) == 1


def test_thread_reply_is_marked_at_its_own_event():
    """mark_read targets the reply itself; the room is the channel, never
    the thread root."""
    api = _api()
    a, _ = _make_adapter(api)
    _run(a, _push(event_id="$reply", thread_id="$root"))
    assert _mark_reads(api) == [{"channel": "!room", "up_to": "$reply"}]


def test_message_the_wake_gate_ignores_is_still_marked_read():
    api = _api()
    a, woke = _make_adapter(api, wake=False)
    _run(a, _push())
    assert woke == []
    assert _mark_reads(api) == [{"channel": "!room", "up_to": "$evt"}]


def test_system_notice_is_still_marked_read():
    api = _api()
    a, woke = _make_adapter(api)
    _run(a, _push(sender=f"@filament_god:{_HOST}"))
    assert woke == []
    assert _mark_reads(api) == [{"channel": "!room", "up_to": "$evt"}]


def test_repeated_push_marks_once():
    api = _api()
    a, woke = _make_adapter(api, real_dedup=True)
    _run(a, _push(), _push(), _push(event_id="$other"))
    assert _mark_reads(api) == [
        {"channel": "!room", "up_to": "$evt"},
        {"channel": "!room", "up_to": "$other"},
    ]
    assert len(woke) == 2


def test_own_message_is_not_marked():
    api = _api()
    a, woke = _make_adapter(api)
    _run(a, _push(sender=_AGENT))
    assert woke == []
    assert _mark_reads(api) == []


def test_failing_mark_read_does_not_block_dispatch():
    async def _boom():
        raise RuntimeError("server down")

    api = _api(_boom)
    a, woke = _make_adapter(api)
    _run(a, _push())
    assert len(_mark_reads(api)) == 1
    assert len(woke) == 1


def test_rejected_mark_read_does_not_block_dispatch():
    async def _rejected():
        return {"error": {"code": -32003, "message": "forbidden"}}

    api = _api(_rejected)
    a, woke = _make_adapter(api)
    _run(a, _push())
    assert len(_mark_reads(api)) == 1
    assert len(woke) == 1


def test_slow_mark_read_does_not_delay_dispatch():
    """The turn is dispatched while the receipt is still in flight."""
    release = None
    woke_before_receipt = []

    async def _slow():
        await release.wait()
        return {"result": {"content": []}}

    api = _api(_slow)
    a, woke = _make_adapter(api)

    async def _go():
        nonlocal release
        release = asyncio.Event()
        await a._handle_push_message_turn(_push(), "turn-1")
        woke_before_receipt.extend(woke)
        assert a._receipt_tasks, "receipt should still be in flight"
        release.set()
        await _settle(a)

    asyncio.run(_go())
    assert len(woke_before_receipt) == 1
    assert len(_mark_reads(api)) == 1
    assert not a._receipt_tasks


def test_no_api_skips_the_receipt():
    a, woke = _make_adapter(None)
    _run(a, _push())
    assert len(woke) == 1


def test_reaction_push_is_not_marked():
    api = _api()
    a, _ = _make_adapter(api)
    reaction = fcm_client.ReactionMessage(
        event_id="$reaction",
        room_id="!room",
        room_name="Room",
        sender=_ALICE,
        sender_display_name="Someone",
        key="👀",
        target_event_id="$target",
        removed=False,
        is_direct=False,
        thread_id=None,
        raw={},
    )

    async def _go():
        await a._handle_reaction_turn(reaction, "turn-1")
        await _settle(a)

    asyncio.run(_go())
    assert _mark_reads(api) == []


def test_invite_push_is_not_marked():
    api = _api()
    a, _ = _make_adapter(api)
    scheduled = []
    a._schedule_async = lambda coro, label="task": scheduled.append(coro)
    invite = fcm_client.InviteMessage(
        room_id="!loop",
        branch_type="add_to_space",
        inviter="Alice",
        inviter_id=_ALICE,
        room_name="Loop",
        raw={},
    )
    a._on_invite(invite)

    async def _go():
        for coro in scheduled:
            await coro
        await _settle(a)

    asyncio.run(_go())
    assert ("accept_invite", {"loop_id": "!loop"}) in api.calls
    assert _mark_reads(api) == []
