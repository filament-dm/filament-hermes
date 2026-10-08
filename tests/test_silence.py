"""ENG-1905: an agent that decides not to reply posts nothing.

Thread follow-ups wake the agent on every human reply in a thread it was
mentioned in, and most of them aren't for it. The model's way to stay quiet is
a bare ``[SILENT]`` reply; the invariants pinned here:

- the core rules (always in the envelope) name the marker
- send() never posts a bare marker, but posts prose that mentions one
- an unaddressed wake tells Hermes no reply is expected, so the gateway lets
  the marker stand instead of posting its "unexpected silence" fallback
- a Hermes whose MessageEvent predates reply_expected still dispatches

Modules are loaded standalone with the Hermes gateway stubbed (same pattern
as ``test_slash_adapter``).
"""

import asyncio
import dataclasses
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


@dataclasses.dataclass
class _MessageEvent:
    """Mirrors the Hermes dataclass shape, reply_expected included."""

    text: str = ""
    message_type: Any = None
    source: Any = None
    message_id: Optional[str] = None
    raw_message: Any = None
    channel_context: Optional[str] = None
    channel_prompt: Optional[str] = None
    reply_expected: Optional[bool] = None


@dataclasses.dataclass
class _OldMessageEvent:
    """A Hermes MessageEvent from before reply_expected existed."""

    text: str = ""
    message_type: Any = None
    source: Any = None
    message_id: Optional[str] = None
    raw_message: Any = None
    channel_context: Optional[str] = None
    channel_prompt: Optional[str] = None


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

        def _mark_connected(self):
            pass

        def _mark_disconnected(self):
            pass

    class _SendResult:
        def __init__(self, success, raw_response=None, error=None, retryable=False):
            self.success = success
            self.raw_response = raw_response
            self.error = error
            self.retryable = retryable

    base_mod.BasePlatformAdapter = _BaseAdapter
    base_mod.MessageEvent = _MessageEvent
    base_mod.MessageType = types.SimpleNamespace(TEXT="text")
    base_mod.ProcessingOutcome = type("ProcessingOutcome", (), {})
    base_mod.SendResult = _SendResult

    gateway_pkg.config = config_mod
    gateway_pkg.platforms = platforms_pkg
    platforms_pkg.base = base_mod
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_pkg
    sys.modules["gateway.platforms.base"] = base_mod

    # setup_cli imports hermes_cli at module level; setdefault keeps any
    # richer stub another test module already installed.
    hermes_cli_pkg = types.ModuleType("hermes_cli")
    setup_mod = types.ModuleType("hermes_cli.setup")
    setup_mod.__getattr__ = lambda name: lambda *a, **k: None
    hermes_cli_pkg.setup = setup_mod
    sys.modules.setdefault("hermes_cli", hermes_cli_pkg)
    sys.modules.setdefault("hermes_cli.setup", setup_mod)


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
        sys.modules["hermes_filament_fcm.reactive"],
        sys.modules["hermes_filament_fcm.adapter"],
    )


reactive, adapter = _load_modules()
framing = adapter.framing

_SHARED = "!shared:fil"


def _envelope(payload) -> dict:
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


class _FakeFilamentAPI:
    _mcp_url = "https://example.invalid/mcp/agents"

    def __init__(self):
        self.posted: list[tuple[str, str]] = []

    async def call_tool(self, name, arguments):
        return _envelope({})

    async def post_message(self, channel, markdown_body):
        self.posted.append((channel, markdown_body))
        return _envelope({"event_id": "$reply"})

    async def reply_in_thread(self, message_id, markdown_body):
        self.posted.append((message_id, markdown_body))
        return _envelope({"event_id": "$reply"})


class _FakeServerSync:
    async def sync(self):
        pass

    async def write_back(self, *sections):
        pass


def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path))
    api = _FakeFilamentAPI()
    a = adapter.FCMFilamentAdapter(
        object(), filament_api=api, server_sync=_FakeServerSync()
    )
    a._cc_room_id = "!cc:fil"
    a._owner_id = "@owner:fil"
    dispatched = []

    async def _record(event):
        dispatched.append(event)

    a.handle_message = _record
    return a, api, dispatched


def _wake(a, **kw):
    args = dict(
        channel=_SHARED,
        channel_name="general",
        sender="@human:fil",
        sender_name="Human",
        trigger="message",
        data="you two sort it out",
        target_event_id="$evt",
        thread_id="$root",
        raw={},
        breadcrumb=None,
    )
    args.update(kw)
    asyncio.run(a._wake(**args))


@pytest.mark.parametrize(
    "reply",
    ["[SILENT]", "  [silent]\n", "**[SILENT]**", "`[SILENT]`", "NO_REPLY.", "no reply"],
)
def test_bare_markers_are_silence(reply):
    assert framing.is_silence_marker(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        None,
        "I'm not going to reply, this isn't meant for me.",
        "Silent retry succeeded",
        "[SILENT] not for me",
        "[SILENT",
    ],
)
def test_prose_is_not_silence(reply):
    assert not framing.is_silence_marker(reply)


def test_core_rules_name_the_marker():
    assert "`[SILENT]`" in reactive.CORE_RULES
    effective = reactive.InstructionsStore(Path("/nonexistent/x.md")).read_effective()
    assert "`[SILENT]`" in effective


def test_send_drops_a_bare_marker(tmp_path, monkeypatch):
    a, api, _ = _make_adapter(tmp_path, monkeypatch)
    result = asyncio.run(a.send(_SHARED, "[SILENT]"))
    assert result.success
    assert api.posted == []


def test_send_posts_prose_that_mentions_the_marker(tmp_path, monkeypatch):
    a, api, _ = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a.send(_SHARED, "Reply [SILENT] to keep me quiet."))
    assert api.posted == [(_SHARED, "Reply [SILENT] to keep me quiet.")]


def test_unaddressed_wake_expects_no_reply(tmp_path, monkeypatch):
    a, _, dispatched = _make_adapter(tmp_path, monkeypatch)
    _wake(a)
    assert dispatched[0].reply_expected is False


@pytest.mark.parametrize("kw", [{"addressed": True}, {"is_direct": True}])
def test_mention_or_dm_expects_a_reply(tmp_path, monkeypatch, kw):
    a, _, dispatched = _make_adapter(tmp_path, monkeypatch)
    _wake(a, **kw)
    assert dispatched[0].reply_expected is True


def test_older_hermes_without_the_field_still_dispatches(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "MessageEvent", _OldMessageEvent)
    a, _, dispatched = _make_adapter(tmp_path, monkeypatch)
    _wake(a)
    assert isinstance(dispatched[0], _OldMessageEvent)


_NOTICE = (
    "⚠️ The model returned only a silence marker for a message that needed a reply."
)


def _send_in_turn(a, content, reply_expected):
    async def main():
        if reply_expected is not None:
            adapter.turn_context.activate(
                adapter.turn_context.data_turn(
                    capabilities=None,
                    cursor_channel=None,
                    reply_anchor=None,
                    history_key=None,
                    reply_expected=reply_expected,
                )
            )
        return await a.send(_SHARED, content)

    return asyncio.run(main())


def test_older_hermes_notice_is_dropped_on_an_unaddressed_turn(tmp_path, monkeypatch):
    # Hermes 0.21 has no reply_expected: it swaps [SILENT] for this notice.
    monkeypatch.setattr(adapter, "_hermes_text", lambda key: _NOTICE)
    a, api, _ = _make_adapter(tmp_path, monkeypatch)
    assert _send_in_turn(a, _NOTICE, reply_expected=False).success
    assert api.posted == []


@pytest.mark.parametrize("reply_expected", [True, None])
def test_notice_still_posts_when_a_reply_was_expected(
    tmp_path, monkeypatch, reply_expected
):
    monkeypatch.setattr(adapter, "_hermes_text", lambda key: _NOTICE)
    a, api, _ = _make_adapter(tmp_path, monkeypatch)
    _send_in_turn(a, _NOTICE, reply_expected=reply_expected)
    assert api.posted == [(_SHARED, _NOTICE)]


def test_wake_pins_reply_expected_on_the_turn_context(tmp_path, monkeypatch):
    a, _, _ = _make_adapter(tmp_path, monkeypatch)
    seen = []

    async def _record(event):
        seen.append(adapter.turn_context.current().reply_expected)

    a.handle_message = _record
    _wake(a)
    _wake(a, addressed=True)
    assert seen == [False, True]


def test_hermes_021_constant_notice_is_dropped_too(tmp_path, monkeypatch):
    # 0.21 keeps the notice as a run_turn constant, not a catalog entry.
    run_turn = types.ModuleType("gateway.run_turn")
    run_turn._UNEXPECTED_SILENCE_REPLY = _NOTICE
    monkeypatch.setitem(sys.modules, "gateway.run_turn", run_turn)
    monkeypatch.setattr(adapter, "_hermes_text", lambda key: key)
    a, api, _ = _make_adapter(tmp_path, monkeypatch)
    assert _send_in_turn(a, _NOTICE, reply_expected=False).success
    assert api.posted == []
