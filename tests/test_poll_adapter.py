"""The adapter side of the poll_work transport: each poll item reaches the
handler its FCM push would, carrying what the push said about its room and
sender; and a polling adapter neither registers with FCM nor asks for the
FCM-path probe.

Loaded standalone with Hermes stubbed, like ``test_first_hello``.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


class _SendResult:
    def __init__(self, success=False, error=None, raw_response=None, retryable=False):
        self.success = success
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable


class _MessageEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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
            return types.SimpleNamespace(**kwargs)

        async def handle_message(self, event):
            pass

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


def _load():
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


adapter = _load()
framing = sys.modules["hermes_filament_fcm.framing"]
fcm_client = sys.modules["hermes_filament_fcm.fcm_client"]
poll_transport = sys.modules["hermes_filament_fcm.poll_transport"]

_ROOM = "!room:s"


class _Api:
    def __init__(self, details=None):
        self.details = details if details is not None else _details()
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "get_channel_details":
            text = json.dumps(self.details)
            return {"result": {"content": [{"type": "text", "text": text}]}}
        raise AssertionError(f"unexpected tool {name}")


def _details(**overrides):
    data = {
        "channel_id": _ROOM,
        "name": "general",
        "loop_id": "!loop:s",
        "loop_name": "Acme",
        "member_count": 2,
        "members": [
            {"mxid": "@bob:s", "display_name": "Bob"},
            {"mxid": "@agent:s", "display_name": "Hermes"},
        ],
    }
    data.update(overrides)
    return data


def _make(api=None):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._filament_api = api or _Api()
    a._polling = True
    a._room_info = {}
    a.pushed, a.reacted, a.invited, a.vouched, a.scheduled = [], [], [], [], []

    async def handle_push(msg):
        a.pushed.append(msg)

    async def handle_reaction(reaction):
        a.reacted.append(reaction)

    a._handle_push_message = handle_push
    a._handle_reaction = handle_reaction
    a._on_invite = a.invited.append
    a._on_vouch = a.vouched.append

    def schedule(coro, label="task"):
        a.scheduled.append(label)
        coro.close()

    a._schedule_async = schedule
    return a


def _item(raw):
    return poll_transport.parse_item(raw)


def _message(**row):
    base = {
        "event_id": "$1",
        "sender": "@bob:s",
        "body": "hello @agent",
        "is_mention": True,
        "is_from_agent": False,
    }
    base.update(row)
    return base


def test_message_rows_become_push_messages():
    a = _make()
    item = _item(
        {
            "kind": "message",
            "channel_id": _ROOM,
            "thread_id": "$root",
            "messages": [_message(), _message(event_id="$2", body="", media=[{}])],
        }
    )
    asyncio.run(a._run_poll_item(item))
    first, second = a.pushed
    assert isinstance(first, fcm_client.PushMessage)
    assert (first.event_id, first.room_id, first.thread_id) == ("$1", _ROOM, "$root")
    assert first.room_name == "general"
    assert first.loop_name == "Acme"
    assert first.sender_display_name == "Bob"
    assert first.is_mention is True
    assert first.sender_is_agent is False
    assert first.is_direct is False
    assert first.branch_type == "channel_message"
    assert first.has_content is True and first.has_media is False
    # An uncaptioned attachment: the media note path fetches it.
    assert second.has_content is False and second.has_media is True
    # One details call serves the whole item.
    assert [name for name, _ in a._filament_api.calls] == ["get_channel_details"]


def test_room_details_are_cached():
    a = _make()
    item = _item({"kind": "message", "channel_id": _ROOM, "messages": [_message()]})
    asyncio.run(a._run_poll_item(item))
    asyncio.run(a._run_poll_item(item))
    assert len(a._filament_api.calls) == 1


def test_a_dm_guess_routes_like_a_direct_push():
    dm = _details(name=None, loop_id=None, loop_name=None)
    a = _make(_Api(dm))
    item = _item({"kind": "message", "channel_id": _ROOM, "messages": [_message()]})
    asyncio.run(a._run_poll_item(item))
    (msg,) = a.pushed
    assert msg.is_direct is True
    assert msg.branch_type == "direct_message"
    assert msg.room_name == ""


def test_the_server_flag_beats_the_guess():
    a = _make()
    item = _item(
        {
            "kind": "message",
            "channel_id": _ROOM,
            "is_direct": True,
            "messages": [_message()],
        }
    )
    asyncio.run(a._run_poll_item(item))
    assert a.pushed[0].is_direct is True


def test_an_unreadable_room_still_delivers():
    class _Broken(_Api):
        async def call_tool(self, name, args):
            raise ConnectionError("down")

    a = _make(_Broken())
    item = _item({"kind": "message", "channel_id": _ROOM, "messages": [_message()]})
    asyncio.run(a._run_poll_item(item))
    (msg,) = a.pushed
    assert msg.room_name == "" and msg.sender_display_name == ""


def test_reaction_rows_become_reactions():
    a = _make()
    item = _item(
        {
            "kind": "reaction",
            "channel_id": _ROOM,
            "reactions": [
                {
                    "event_id": "$r",
                    "sender": "@bob:s",
                    "key": "👍",
                    "target_event_id": "$1",
                }
            ],
        }
    )
    asyncio.run(a._run_poll_item(item))
    (reaction,) = a.reacted
    assert isinstance(reaction, fcm_client.ReactionMessage)
    assert (reaction.key, reaction.target_event_id) == ("👍", "$1")
    assert reaction.sender_display_name == "Bob"
    assert reaction.removed is False


def test_dispatch_schedules_turns_and_accepts_invites_and_vouches():
    a = _make()
    msg = _item({"kind": "message", "channel_id": _ROOM, "messages": [_message()]})
    invite = _item({"kind": "invite", "loop_id": "!l:s", "invited_by": "@al:s"})
    vouch = _item({"kind": "vouch", "loop_id": "!v:s", "vouched_by": "@cy:s"})
    for item in (msg, invite, vouch):
        asyncio.run(a._dispatch_poll_item(item))
    assert a.scheduled == ["poll message"]
    ((inv,), (vou,)) = (a.invited, a.vouched)
    assert isinstance(inv, fcm_client.InviteMessage) and inv.room_id == "!l:s"
    assert isinstance(vou, fcm_client.VouchMessage) and vou.loop_id == "!v:s"


def test_a_polling_adapter_does_not_ask_for_the_push_probe():
    a = _make()
    a._last_probe_request = None
    asyncio.run(a._maybe_request_probe())
    assert a._filament_api.calls == []
    assert a._last_probe_request is None


def test_poll_mode_connect_skips_fcm():
    a = _make()
    a._installation_id = "inst"
    a._gateway_instance_id = "gw"
    a._user_id = a._owner_id = a._cc_room_id = None
    a._filament_api._mcp_url = "http://x/mcp/agents"
    steps = []

    async def ok(*_a, **_k):
        return True

    async def nothing(*_a, **_k):
        return None

    async def fcm(*_a, **_k):
        raise AssertionError("FCM stage ran in poll mode")

    a._initialize_api = ok
    a._register_fcm = fcm
    a._register_pusher = fcm
    a._start_listener = fcm
    a._start_polling = lambda: steps.append("poll")
    a._accept_pending_vouches = nothing
    a._announce_completed_upgrade = nothing
    a._mark_connected = lambda: steps.append("connected")
    a._start_update_check = lambda: None
    a._seed_profile = nothing
    a._maybe_greet = nothing
    a._server_config = types.SimpleNamespace(sync=nothing, maybe_report_tools=nothing)
    adapter._seed_display_defaults = lambda: None
    assert asyncio.run(a._connect_attempt(False, "conn")) is True
    assert steps == ["poll", "connected"]


def test_server_described_items_skip_the_room_lookup():
    a = _make()
    item = _item(
        {
            "kind": "message",
            "channel_id": _ROOM,
            "is_direct": True,
            "channel_name": None,
            "loop_name": None,
            "sender_names": {"@bob:s": "Bobby"},
            "messages": [_message()],
        }
    )
    asyncio.run(a._run_poll_item(item))
    (msg,) = a.pushed
    assert msg.is_direct is True
    assert msg.sender_display_name == "Bobby"
    assert a._filament_api.calls == []


# ── a draining gateway ──────────────────────────────────────────────


def test_admitting_follows_the_gateway_drain():
    a = _make()
    assert a._gateway_admitting() is True, "no runner: an older Hermes"
    a.gateway_runner = types.SimpleNamespace()
    assert a._gateway_admitting() is True
    a.gateway_runner = types.SimpleNamespace(
        _draining=False, session_authority=types.SimpleNamespace(retiring=False)
    )
    assert a._gateway_admitting() is True
    a.gateway_runner._draining = True
    assert a._gateway_admitting() is False
    a.gateway_runner._draining = False
    a.gateway_runner.session_authority.retiring = True
    assert a._gateway_admitting() is False


def test_the_poll_loop_starts_from_the_cursor_a_draining_gateway_left(
    tmp_path, monkeypatch
):
    credentials = sys.modules["hermes_filament_fcm.credentials"]
    a = _make()
    a._credentials = credentials.CredentialStore(str(tmp_path))
    a._user_id = "@agent:s"
    a._credentials.save_poll_cursor("c:abc", "@agent:s")
    made = {}

    class _Loop:
        def __init__(self, call, dispatch, **kwargs):
            made.update(kwargs)

        async def run(self):
            return None

    monkeypatch.setattr(poll_transport, "PollLoop", _Loop)
    asyncio.run(a._run_poll_loop())
    assert made["cursor"] == "c:abc"
    assert made["admitting"] == a._gateway_admitting
    made["keep_cursor"](None)
    assert not (tmp_path / "poll_cursor.json").exists()
    made["keep_cursor"]("c:def")
    assert a._credentials.load_poll_cursor("@agent:s") == "c:def"


def test_a_cursor_left_for_another_agent_is_dropped(tmp_path):
    credentials = sys.modules["hermes_filament_fcm.credentials"]
    store = credentials.CredentialStore(str(tmp_path))
    assert store.load_poll_cursor("@agent:s") is None
    store.save_poll_cursor("c:abc", "@old-agent:s")
    assert store.load_poll_cursor("@agent:s") is None
    assert not (tmp_path / "poll_cursor.json").exists()
    store.clear_poll_cursor()  # missing is fine
