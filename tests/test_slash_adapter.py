"""Adapter-level wiring for the backchannel slash-command layer.

The product invariant pinned here lives in ``_handle_control_message``: a
control message whose body starts with ``/`` is executed deterministically —
it must NEVER be dispatched to the LLM, in success or failure. The pure
parsing/compilation is covered in ``test_slash.py``; these tests exercise the
adapter seam around it: live channel resolution via ``list_channels``, the
store writes, the section write-backs, and the confirmation reply.

Modules are loaded standalone with the Hermes gateway stubbed (same pattern
as ``test_thread_follow_up``).
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

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

    class _MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

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

_CC_ROOM = "!cc:fil"
_WELCOME = "!welcome:fil"


def _envelope(payload) -> dict:
    return {
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}
    }


class _FakeFilamentAPI:
    """list_channels + post_message, in the real MCP envelope shape."""

    _mcp_url = "https://example.invalid/mcp/agents"

    def __init__(self):
        self.posted: list[tuple[str, str]] = []
        self.calls: list[str] = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name == "list_channels":
            return _envelope(
                {
                    "channels": [
                        {"channel_id": _WELCOME, "name": "welcome"},
                        {"channel_id": _CC_ROOM, "name": "backchannel"},
                        {"channel_id": "!loop:fil", "name": "aloop",
                         "type": "m.space"},
                    ]
                }
            )
        if name == "get_recent_messages":
            return _envelope({"messages": []})
        return _envelope({})

    async def get_thread(self, message_id):
        return _envelope({"root": {"event_id": message_id}})

    async def post_message(self, channel, markdown_body):
        self.posted.append((channel, markdown_body))
        return _envelope({"event_id": "$reply"})

    async def reply_in_thread(self, message_id, markdown_body):
        self.posted.append((message_id, markdown_body))
        return _envelope({"event_id": "$reply"})


class _FakeServerSync:
    def __init__(self):
        self.written_back: list[str] = []

    async def sync(self):
        pass

    async def write_back(self, section):
        self.written_back.append(section)


def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path))
    api = _FakeFilamentAPI()
    sync = _FakeServerSync()
    a = adapter.FCMFilamentAdapter(object(), filament_api=api, server_sync=sync)
    a._cc_room_id = _CC_ROOM
    a._owner_id = "@irena:fil"
    dispatched = []

    async def _record(event):
        dispatched.append(event)

    a.handle_message = _record
    return a, api, sync, dispatched


def _control_msg(body):
    return fcm_client.PushMessage(
        event_id="$evt",
        room_id=_CC_ROOM,
        room_name="backchannel",
        sender="@irena:fil",
        sender_display_name="Irena",
        body=body,
        is_direct=True,
        branch_type="direct_message",
        thread_id=None,
        is_mention=False,
        is_everyone_mention=False,
        raw={},
    )


def test_slash_message_never_reaches_the_llm(tmp_path, monkeypatch):
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/tools #welcome post off")))
    assert dispatched == []  # no LLM turn, ever
    assert len(api.posted) == 1
    room, reply = api.posted[0]
    assert room == _CC_ROOM
    assert reply.startswith("✓ Disabled post in #welcome")
    # The mutation landed in the real store files (read-fresh-per-event).
    policy = json.loads((tmp_path / "capability_policy.json").read_text())
    assert policy["per_channel"][_WELCOME] == [
        "read_history",
        "directory",
        "escalate",
    ]
    # Opt-in coupling: the same command turned the gating feature on...
    flags = json.loads((tmp_path / "feature_flags.json").read_text())
    assert flags["advanced_tool_controls"] is True
    # ...and both sections were mirrored to the server document.
    assert sync.written_back == ["capability_policy", "feature_flags"]


def test_unparseable_slash_message_replies_help_not_model(tmp_path, monkeypatch):
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/frobnicate the widgets")))
    assert dispatched == []  # parse failure is help text, not a model turn
    assert len(api.posted) == 1
    assert "/tools" in api.posted[0][1]  # the command index
    assert sync.written_back == []  # nothing written


def test_guidance_slash_writes_channel_instructions(tmp_path, monkeypatch):
    a, _api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(
        a._handle_control_message(_control_msg("/guidance #welcome Be  brief."))
    )
    assert dispatched == []
    saved = json.loads((tmp_path / "channel_instructions.json").read_text())
    assert saved == {_WELCOME: "Be  brief."}
    assert sync.written_back == ["channel_instructions"]


def test_space_rooms_are_not_slash_channels(tmp_path, monkeypatch):
    a, _api, _sync, _dispatched = _make_adapter(tmp_path, monkeypatch)
    channels = asyncio.run(a._slash_channels())
    assert (_WELCOME, "welcome") in channels
    assert all(room_id != "!loop:fil" for room_id, _name in channels)


def test_non_slash_control_message_still_dispatches(tmp_path, monkeypatch):
    a, _api, _sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("hello there")))
    assert len(dispatched) == 1  # the normal LLM control path is untouched


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
