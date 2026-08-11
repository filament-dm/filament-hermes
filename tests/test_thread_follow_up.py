"""ENG-724: a non-agent's reply in a thread the agent was already @-mentioned
in counts as a mention and wakes the agent — and nothing else does.

These are adapter-level tests of the wiring in ``_handle_push_message_turn``,
because the bug that reverted the first attempt (filament-hermes#20) lived
exactly there: the gate treated *delivery* as engagement, so two subscribed
agents woke each other in a loop. The invariants pinned here:

- un-mentioned + engaged thread + HUMAN sender  → wakes (no re-tag needed)
- un-mentioned + engaged thread + AGENT sender  → stays asleep (no storms)
- un-mentioned + un-engaged thread              → stays asleep (delivery ≠ engagement)
- unclassifiable sender (API failure)           → stays asleep (fail closed)
- thread_wake="off"                             → stays asleep (escape hatch)
- muted channel (reactive_wake="off")           → stays asleep (mute wins)
- an admitted mention records its thread root as engaged

Modules are loaded standalone (same pattern as ``test_system_notice_skip``):
importing the package pulls in the Hermes ``gateway`` package, absent in a bare
test env, so ``firebase_messaging`` and the gateway modules are stubbed first.
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
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
        sys.modules["hermes_filament_fcm.reactive"],
        sys.modules["hermes_filament_fcm.adapter"],
    )


fcm_client, reactive, adapter = _load_modules()

_HOST = "filament.example"
_AGENT = f"@d_agent:{_HOST}"
_HUMAN = f"@franni:{_HOST}"
_OTHER_BOT = f"@d_otherbot:{_HOST}"
_ROOT = "$thread_root"


class _FakeFilamentAPI:
    """Serves a canned get_thread response in the real MCP envelope shape, so
    the adapter's parse path is exercised, not bypassed."""

    def __init__(self, thread: dict):
        self._thread = thread
        self.calls = 0

    async def get_thread(self, message_id):
        self.calls += 1
        return {
            "result": {"content": [{"type": "text", "text": json.dumps(self._thread)}]}
        }


def _thread_with(sender: str, event_id: str, is_agent: bool) -> dict:
    return {
        "root": {"event_id": _ROOT, "sender": _HUMAN, "is_from_agent": False},
        "replies": [
            {"event_id": "$agent_reply", "sender": _AGENT, "is_from_agent": True},
            {"event_id": event_id, "sender": sender, "is_from_agent": is_agent},
        ],
    }


class _NoopServerConfig:
    """Server-config sync stub: the turn path awaits sync() before reading the
    stores fresh; these tests exercise the thread-wake rule only."""

    async def sync(self, *, force=False):
        return None


def _make_adapter(tmp: Path, thread: dict | None):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._server_config = _NoopServerConfig()
    a._user_id = _AGENT
    a._cc_room_id = None
    a._wake_policy = reactive.WakePolicyStore(tmp / "wake.json")
    a._engaged_threads = reactive.EngagedThreadStore(tmp / "threads.json")
    a._sender_is_agent_cache = {}
    a._filament_api = _FakeFilamentAPI(thread) if thread is not None else None
    a._is_new_event = lambda event_id: True
    a._is_control_channel = lambda room_id: False
    a._mentions_me = lambda body: "@d_agent" in body
    a._strip_mention = lambda body: body

    async def _no_media(msg):
        return None

    a._media_note = _no_media

    woke = []

    async def _record_wake(**kwargs):
        woke.append(kwargs)

    a._wake = _record_wake
    return a, woke


def _push(
    sender: str,
    *,
    body: str = "and one more thing...",
    thread_id: str | None = _ROOT,
    is_mention: bool = False,
    event_id: str = "$follow_up",
):
    return fcm_client.PushMessage(
        event_id=event_id,
        room_id="!shared",
        room_name="general",
        sender=sender,
        sender_display_name="Someone",
        body=body,
        is_direct=False,
        branch_type="channel_message",
        thread_id=thread_id,
        is_mention=is_mention,
        is_everyone_mention=False,
        raw={},
        has_content=True,
    )


def _run(a, msg):
    asyncio.run(a._handle_push_message_turn(msg, "turn-1"))


def test_human_follow_up_in_engaged_thread_wakes():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        a._engaged_threads.record("!shared", _ROOT)
        _run(a, _push(_HUMAN))
        assert len(woke) == 1
        # The reply threads off the same root, renewing the subscription loop.
        assert woke[0]["thread_id"] == _ROOT


def test_agent_follow_up_in_engaged_thread_stays_asleep():
    # The storm case (filament-hermes#20): another agent's un-mentioned reply
    # must never wake us, even in a thread we're engaged in.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_OTHER_BOT, "$follow_up", True))
        a._engaged_threads.record("!shared", _ROOT)
        _run(a, _push(_OTHER_BOT))
        assert woke == []


def test_agent_explicit_mention_still_wakes():
    # Agents CAN still summon each other — with an explicit @-mention.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_OTHER_BOT, "$follow_up", True))
        _run(a, _push(_OTHER_BOT, is_mention=True))
        assert len(woke) == 1


def test_unengaged_thread_stays_asleep():
    # Delivery is NOT engagement: a thread we were never mentioned in doesn't
    # wake us, and the sender-classification API isn't even consulted.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        _run(a, _push(_HUMAN))
        assert woke == []
        assert a._filament_api.calls == 0


def test_unclassifiable_sender_fails_closed():
    # get_thread unavailable → sender unknown → treated as an agent, no wake.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, thread=None)
        a._engaged_threads.record("!shared", _ROOT)
        _run(a, _push(_HUMAN))
        assert woke == []


def test_thread_wake_off_is_mention_only():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        a._engaged_threads.record("!shared", _ROOT)
        a._wake_policy.write({"thread_wake": "off"})
        _run(a, _push(_HUMAN))
        assert woke == []


def test_muted_channel_wins_over_thread_follow_up():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        a._engaged_threads.record("!shared", _ROOT)
        a._wake_policy.write({"reactive_wake": "off"})
        _run(a, _push(_HUMAN))
        assert woke == []


def test_admitted_mention_records_thread_root():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        # A top-level mention records its own event id — the root-to-be.
        _run(a, _push(_HUMAN, thread_id=None, is_mention=True, event_id=_ROOT))
        assert len(woke) == 1
        assert a._engaged_threads.is_engaged("!shared", _ROOT)
        # ...so the un-mentioned follow-up in that thread now wakes.
        _run(a, _push(_HUMAN))
        assert len(woke) == 2


def test_muted_mention_does_not_record_engagement():
    # A mention the policy refused to wake on must not seed engagement:
    # unmuting later shouldn't retroactively hand old threads a wake.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, _thread_with(_HUMAN, "$follow_up", False))
        a._wake_policy.write({"reactive_wake": "off"})
        _run(a, _push(_HUMAN, thread_id=None, is_mention=True, event_id=_ROOT))
        assert woke == []
        assert not a._engaged_threads.is_engaged("!shared", _ROOT)
