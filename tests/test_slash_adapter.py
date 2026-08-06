"""Adapter-level wiring for the backchannel slash-command layer.

The product invariant pinned here lives in ``_handle_control_message``, in
both directions: a control message whose stripped body starts with ``/fil-``
(case-insensitive) is executed deterministically — it must NEVER be
dispatched to the LLM, in success or failure — while any *other* leading-``/``
message belongs to some other software's slash namespace and must fall
through to the normal LLM control path untouched. The pure
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
        self.write_back_calls: list[tuple[str, ...]] = []

    async def sync(self):
        pass

    async def write_back(self, *sections):
        self.write_back_calls.append(sections)
        self.written_back.extend(sections)


def _make_adapter(tmp_path, monkeypatch, slash_enabled=True):
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path))
    # The slash surface is gated behind the slash_commands feature flag
    # (default OFF, read fresh per event). Most tests exercise the enabled
    # surface, so the fixture pre-seeds the flag file; pass
    # slash_enabled=False to pin the dark default.
    if slash_enabled:
        (tmp_path / "feature_flags.json").write_text(
            json.dumps({"slash_commands": True})
        )
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
    asyncio.run(
        a._handle_control_message(_control_msg("/fil-config #welcome post off"))
    )
    assert dispatched == []  # no LLM turn, ever
    assert len(api.posted) == 1
    room, reply = api.posted[0]
    assert room == _CC_ROOM
    assert reply.startswith("✓ Disabled **post** in **#welcome**")
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
    asyncio.run(a._handle_control_message(_control_msg("/fil-frobnicate the widgets")))
    assert dispatched == []  # parse failure is help text, not a model turn
    assert len(api.posted) == 1
    assert "/fil-config" in api.posted[0][1]  # the command index
    assert sync.written_back == []  # nothing written


def test_guidance_slash_writes_channel_instructions(tmp_path, monkeypatch):
    a, _api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(
        a._handle_control_message(
            _control_msg("/fil-config #welcome guidance Be  brief.")
        )
    )
    assert dispatched == []
    saved = json.loads((tmp_path / "channel_instructions.json").read_text())
    assert saved == {_WELCOME: "Be  brief."}
    assert sync.written_back == ["channel_instructions"]


def test_space_rooms_and_backchannel_are_not_slash_channels(tmp_path, monkeypatch):
    a, _api, _sync, _dispatched = _make_adapter(tmp_path, monkeypatch)
    channels, backchannel = asyncio.run(a._slash_channels())
    assert (_WELCOME, "welcome") in channels
    assert all(room_id != "!loop:fil" for room_id, _name in channels)
    # The cc room is excluded from the vocabulary (per-channel controls are
    # meaningless for the control plane) and returned separately so the
    # parser can answer explicit targeting with the shared-channels-only
    # note. Consequence: it can never surface as a help example.
    assert all(room_id != _CC_ROOM for room_id, _name in channels)
    assert backchannel == (_CC_ROOM, "backchannel")


def test_slash_command_targeting_backchannel_gets_note(tmp_path, monkeypatch):
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(
        a._handle_control_message(_control_msg("/fil-config #backchannel post off"))
    )
    assert dispatched == []
    assert len(api.posted) == 1
    assert "shared channels only" in api.posted[0][1]
    assert sync.written_back == []  # no writes


def test_slash_tools_list_replies_catalog(tmp_path, monkeypatch):
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/fil-config tools list")))
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert "**Built-in bundles:**" in reply
    assert "**read_history**" in reply
    # Examples come from the shared-channel list, backchannel excluded.
    assert "#welcome" in reply
    assert "#backchannel" not in reply
    assert sync.written_back == []


def test_channel_show_slash_replies_without_writes(tmp_path, monkeypatch):
    # "/fil-config <channel>" is a read-only full-config query: one
    # deterministic reply, no LLM turn, no store writes, no write-backs.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/fil-config #welcome")))
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert reply.startswith("**#welcome** configuration:")
    assert "**read_history**" in reply  # granted rows, bold names
    assert "**Wake:** mention (default)" in reply
    assert "**Guidance:** none" in reply
    assert "`/fil-config #welcome" in reply  # usage examples
    assert sync.written_back == []
    assert not (tmp_path / "capability_policy.json").exists()


def test_config_list_slash_replies_overview_without_writes(tmp_path, monkeypatch):
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/fil-config list")))
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert reply.startswith("**Channels:**")
    assert "- **#welcome** — tools: default" in reply
    assert "Details: `/fil-config #welcome show`" in reply
    assert "#backchannel" not in reply  # excluded from the overview too
    assert sync.written_back == []


def test_old_form_slash_gets_redirect_not_mutation(tmp_path, monkeypatch):
    # A retired top-level command answers with the one-line redirect —
    # deterministic, no LLM turn, and crucially no writes.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/fil-tools #welcome post off")))
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert "moved under `/fil-config`" in reply
    assert "`/fil-config #welcome post off`" in reply
    assert sync.written_back == []
    assert not (tmp_path / "capability_policy.json").exists()


def test_feature_list_slash_replies_live_states(tmp_path, monkeypatch):
    # The fixture seeds slash_commands on; the list must reflect the live
    # store, read-only.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("/fil-config feature list")))
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert reply.startswith("**Features:**")
    assert "✅ **slash_commands** — on —" in reply
    assert "⬜ **advanced_tool_controls** — off —" in reply
    assert sync.written_back == []


def test_query_mixed_with_mutation_never_writes(tmp_path, monkeypatch):
    # A query keyword alongside mutation tokens must never mutate: pointer
    # reply only, nothing written, nothing mirrored.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(
        a._handle_control_message(_control_msg("/fil-config #welcome list post off"))
    )
    assert dispatched == []
    assert len(api.posted) == 1
    reply = api.posted[0][1]
    assert "`/fil-config #welcome show`" in reply
    assert "`/fil-config #welcome <tool> <on|off>`" in reply
    assert sync.written_back == []
    assert not (tmp_path / "capability_policy.json").exists()


def test_non_slash_control_message_still_dispatches(tmp_path, monkeypatch):
    a, _api, _sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(a._handle_control_message(_control_msg("hello there")))
    assert len(dispatched) == 1  # the normal LLM control path is untouched


def test_slash_flag_off_falls_through_to_llm(tmp_path, monkeypatch):
    # Default-OFF gate: with slash_commands disabled, a /fil- message takes
    # the normal LLM control path exactly like non-fil slashes — no
    # deterministic reply, no writes. (Opt-in is via set_feature /
    # set_agent_config / the server config document; slash can't enable
    # itself while off.)
    a, api, sync, dispatched = _make_adapter(
        tmp_path, monkeypatch, slash_enabled=False
    )
    asyncio.run(
        a._handle_control_message(_control_msg("/fil-config #welcome post off"))
    )
    assert len(dispatched) == 1  # reached handle_message
    assert api.posted == []
    assert sync.written_back == []
    assert not (tmp_path / "capability_policy.json").exists()


def test_non_fil_slash_message_falls_through_to_llm(tmp_path, monkeypatch):
    # The other direction of the intercept boundary: leading-/ messages
    # outside the /fil- namespace belong to other software's slash commands
    # and must NOT be swallowed — they take the normal LLM control path.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    for body in ("/tools #welcome post off", "/help", "/filament status"):
        asyncio.run(a._handle_control_message(_control_msg(body)))
    assert len(dispatched) == 3  # every one reached the LLM path
    assert api.posted == []  # no deterministic reply
    assert sync.written_back == []  # and no config writes


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_flag_enable_and_grant_write_back_in_one_batched_call(
    tmp_path, monkeypatch
):
    # The grant that also flips advanced_tool_controls must push both
    # sections in ONE write_back call: pushed separately, the first call's
    # rebase would revert the second section's local file to the server's
    # stale copy before it was ever sent.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    asyncio.run(
        a._handle_control_message(_control_msg("/fil-config #welcome post off"))
    )
    assert sync.write_back_calls == [("capability_policy", "feature_flags")]


def test_slash_arguments_keep_interior_mxid(tmp_path, monkeypatch):
    # Only a LEADING mention is addressing; the agent's MXID inside command
    # arguments (guidance text) is data and must reach the store verbatim.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    a._user_id = "@d_agent:fil"
    asyncio.run(
        a._handle_control_message(
            _control_msg(
                "@d_agent:fil /fil-config #welcome guidance "
                "Escalate to @d_agent:fil twice."
            )
        )
    )
    assert dispatched == []  # the leading mention didn't break the intercept
    stored = json.loads((tmp_path / "channel_instructions.json").read_text())
    assert stored[_WELCOME] == "Escalate to @d_agent:fil twice."


def test_lead_mention_requires_a_token_boundary(tmp_path, monkeypatch):
    # "agent/fil-config …" is NOT an addressed slash command — the localpart
    # is a prefix of the token, not a standalone leading mention. It must go
    # to the LLM untouched by the slash surface.
    a, api, sync, dispatched = _make_adapter(tmp_path, monkeypatch)
    a._user_id = "@agent:fil"
    asyncio.run(
        a._handle_control_message(
            _control_msg("agent/fil-config #welcome post off")
        )
    )
    assert len(dispatched) == 1  # LLM path
    assert sync.written_back == []  # no mutation
    # A real leading mention (with boundary) still strips and executes.
    asyncio.run(
        a._handle_control_message(
            _control_msg("@agent:fil /fil-config #welcome post off")
        )
    )
    assert len(dispatched) == 1  # no second LLM turn
    assert "capability_policy" in sync.written_back
