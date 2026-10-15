"""Media in and out of the adapter, without bytes through the model.

Out: a reply's ``MEDIA:/absolute/path`` reaches the adapter's
send_image_file / send_document / send_video / send_voice, which upload the
file from this process and post it as an attachment where a text reply would
have gone. The model only ever names a path.

In: a control turn's images are fetched by this process into Hermes' image
cache and handed over as the event's ``media_urls``, for Hermes' own vision;
a data turn's are never fetched unasked.

Loaded standalone with Hermes stubbed, like ``test_poll_adapter``.
"""

import asyncio
import contextvars
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
            return kwargs

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
turn_context = sys.modules["hermes_filament_fcm.turn_context"]
fcm_client = sys.modules["hermes_filament_fcm.fcm_client"]
reactive = sys.modules["hermes_filament_fcm.reactive"]
media_tool = sys.modules["hermes_filament_fcm.media_tool"]

_ROOM = "!room:s"


class _Api:
    def __init__(self, upload_error=None, post_result=None):
        self.upload_error = upload_error
        self.post_result = post_result if post_result is not None else {}
        self.calls = []

    async def upload_media(self, path, filename=None, content_type=None):
        self.calls.append(("upload_media", path, filename))
        if self.upload_error:
            raise RuntimeError(self.upload_error)
        return "mxc://s/up"

    async def post_message(self, channel, markdown_body, attachments=None):
        self.calls.append(("post_message", channel, markdown_body, attachments))
        return self.post_result

    async def reply_in_thread(self, message_id, markdown_body, attachments=None):
        self.calls.append(("reply_in_thread", message_id, markdown_body, attachments))
        return self.post_result


def _adapter(api):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._filament_api = api
    a._installation_id = "inst"
    return a


def _run(coro, ctx=None):
    """Run *coro* in a fresh context, with *ctx* as the active turn."""

    async def go():
        if ctx is not None:
            turn_context.activate(ctx)
        return await coro

    return contextvars.Context().run(asyncio.run, go())


def _file(tmp_path, name="chart.png", data=b"\x89PNG\r\n\x1a\n..."):
    f = tmp_path / name
    f.write_bytes(data)
    return str(f)


def test_image_is_uploaded_and_posted_top_level_with_its_caption(tmp_path):
    api = _Api()
    path = _file(tmp_path)
    r = _run(_adapter(api).send_image_file(_ROOM, path, caption="Here it is"))
    assert r.success is True
    assert api.calls == [
        ("upload_media", path, "chart.png"),
        (
            "post_message",
            _ROOM,
            "Here it is",
            [{"mxc_url": "mxc://s/up", "filename": "chart.png"}],
        ),
    ]


def test_media_follows_the_metadata_thread(tmp_path):
    api = _Api()
    path = _file(tmp_path, "notes.pdf", b"%PDF")
    r = _run(
        _adapter(api).send_document(
            _ROOM, path, file_name="Q3 notes.pdf", metadata={"thread_id": "$root"}
        )
    )
    assert r.success is True
    assert api.calls[-1] == (
        "reply_in_thread",
        "$root",
        "",
        [{"mxc_url": "mxc://s/up", "filename": "Q3 notes.pdf"}],
    )
    assert api.calls[0] == ("upload_media", path, "Q3 notes.pdf")


def test_media_follows_the_turns_reply_anchor(tmp_path):
    # Same routing as a text reply: a shared-channel turn anchored on a
    # message threads under it; the anchor of another room doesn't apply.
    ctx = turn_context.control_turn(
        cursor_channel=_ROOM, reply_anchor=(_ROOM, "$trigger"), history_key=None
    )
    api = _Api()
    _run(_adapter(api).send_video(_ROOM, _file(tmp_path, "c.mp4", b"v")), ctx)
    assert api.calls[-1][:2] == ("reply_in_thread", "$trigger")
    api = _Api()
    _run(_adapter(api).send_voice("!other:s", _file(tmp_path, "a.ogg", b"a")), ctx)
    assert api.calls[-1][:2] == ("post_message", "!other:s")


def test_a_path_that_is_not_a_readable_file_is_refused(tmp_path):
    api = _Api()
    for path in (str(tmp_path / "missing.png"), str(tmp_path)):
        r = _run(_adapter(api).send_image_file(_ROOM, path))
        assert r.success is False and r.error
    assert api.calls == []


def test_upload_or_post_failure_is_reported_not_raised(tmp_path):
    api = _Api(upload_error="HTTP 413")
    r = _run(_adapter(api).send_image_file(_ROOM, _file(tmp_path)))
    assert r.success is False and "413" in r.error
    assert [c[0] for c in api.calls] == ["upload_media"]

    api = _Api(post_result={"error": {"code": -32602, "message": "not a member"}})
    r = _run(_adapter(api).send_image_file(_ROOM, _file(tmp_path)))
    assert r.success is False and r.error == "not a member"


def test_not_connected():
    r = _run(_adapter(None).send_document(_ROOM, "/tmp/x"))
    assert r.success is False and r.error == "Not connected"


# ── Inbound: control-turn images for Hermes' vision ──────────────────

_CC_ROOM = "!cc:s"
_OWNER = "@irena:s"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
_IMAGE = {
    "mxc_url": "mxc://s/img1",
    "msgtype": "m.image",
    "filename": "cat.png",
    "mimetype": "image/png",
    "size": 16,
}


def _envelope(payload):
    return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}


class _InboundApi:
    """get_thread serves *media* on the pushed event; fetch_media writes
    *data* (or raises)."""

    _mcp_url = "https://example.invalid/mcp/agents"
    parse_tool_result = staticmethod(
        sys.modules["hermes_filament_fcm.filament_api"].FilamentAPI.parse_tool_result
    )

    def __init__(self, media, data=_PNG, error=None):
        self.media = media
        self.data = data
        self.error = error
        self.fetched = []

    async def get_thread(self, message_id):
        return _envelope({"root": {"event_id": message_id, "media": self.media}})

    async def call_tool(self, name, arguments):
        return _envelope({"messages": []})

    async def fetch_media(self, mxc_url, dest, message_id=None, max_bytes=None):
        self.fetched.append((mxc_url, message_id, max_bytes))
        if self.error:
            raise RuntimeError(self.error)
        Path(dest).write_bytes(self.data)
        return len(self.data)


class _Sync:
    async def sync(self, *, force=False):
        return None


def _control_adapter(tmp_path, monkeypatch, api):
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path))
    cache = tmp_path / "cache" / "images"
    monkeypatch.setattr(
        sys.modules["gateway.platforms.base"],
        "get_image_cache_dir",
        lambda: cache,
        raising=False,
    )
    a = adapter.FCMFilamentAdapter(object(), filament_api=api, server_sync=_Sync())
    a._cc_room_id = _CC_ROOM
    a._owner_id = _OWNER
    dispatched = []

    async def _record(event):
        dispatched.append(event)

    a.handle_message = _record
    return a, dispatched, cache


def _control_msg(body="look", has_content=True):
    return fcm_client.PushMessage(
        event_id="$evt",
        room_id=_CC_ROOM,
        room_name="backchannel",
        sender=_OWNER,
        sender_display_name="Irena",
        body=body,
        is_direct=True,
        branch_type="direct_message",
        thread_id=None,
        is_mention=False,
        is_everyone_mention=False,
        raw={},
        has_content=has_content,
    )


def test_control_turn_images_reach_hermes_as_local_files(tmp_path, monkeypatch):
    api = _InboundApi([_IMAGE])
    a, dispatched, cache = _control_adapter(tmp_path, monkeypatch, api)
    asyncio.run(a._handle_control_message(_control_msg(has_content=False)))
    (event,) = dispatched
    assert event.media_urls == [str(cache / "filament_img1.png")]
    assert event.media_types == ["image/png"]
    assert Path(event.media_urls[0]).read_bytes() == _PNG
    # Fetched by message, under the cap; the note is kept.
    assert api.fetched == [("mxc://s/img1", "$evt", media_tool.INBOUND_IMAGE_MAX_BYTES)]
    assert "[attachment: cat.png" in event.text
    assert "message_id $evt" in event.text


def test_a_failed_or_bogus_image_degrades_to_the_note(tmp_path, monkeypatch):
    failing = _InboundApi([_IMAGE], error="HTTP 403")
    bogus = _InboundApi([_IMAGE], data=b"<html>")
    for api in (failing, bogus):
        a, dispatched, cache = _control_adapter(tmp_path, monkeypatch, api)
        asyncio.run(a._handle_control_message(_control_msg()))
        (event,) = dispatched
        assert not getattr(event, "media_urls", None)
        assert "[attachment: cat.png" in event.text
        assert not list(cache.glob("*.png"))


def test_only_capped_images_in_formats_hermes_reads_are_fetched():
    big = dict(_IMAGE, size=media_tool.INBOUND_IMAGE_MAX_BYTES + 1)
    svg = dict(_IMAGE, mimetype="image/svg+xml")
    pdf = dict(_IMAGE, msgtype="m.file", mimetype="application/pdf")
    unsized = {k: v for k, v in _IMAGE.items() if k != "size"}
    assert media_tool.inbound_images([big, svg, pdf, "junk", unsized]) == [unsized]
    gallery = [dict(_IMAGE, mxc_url=f"mxc://s/{i}") for i in range(10)]
    assert len(media_tool.inbound_images(gallery)) == media_tool.INBOUND_IMAGE_MAX_COUNT
    assert media_tool.inbound_images(None) == []


def test_data_turn_images_are_never_fetched(tmp_path):
    # A non-principal's image in a shared channel: the note only.
    api = _InboundApi([_IMAGE])
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._server_config = _Sync()
    a._user_id = "@agent:s"
    a._owner_id = _OWNER
    a._cc_room_id = _CC_ROOM
    a._wake_policy = reactive.WakePolicyStore(tmp_path / "wake.json")
    a._feature_flags = reactive.FeatureFlagStore(tmp_path / "flags.json")
    a._seen_history = reactive.SeenHistoryStore(tmp_path / "seen.json")
    a._engaged_threads = reactive.EngagedThreadStore(tmp_path / "threads.json")
    a._sender_is_agent_cache = {}
    a._filament_api = api
    a._is_new_event = lambda event_id: True
    a._mentions_me = lambda body: True
    a._strip_mention = lambda body: body

    async def _no_history(*args):
        return None, None, None

    a._history_for_wake = _no_history
    woke = []

    async def _record_wake(**kwargs):
        woke.append(kwargs)

    a._wake = _record_wake
    msg = fcm_client.PushMessage(
        event_id="$evt",
        room_id="!shared:s",
        room_name="general",
        sender="@stranger:s",
        sender_display_name="Stranger",
        body="@agent look",
        is_direct=False,
        branch_type="channel_message",
        thread_id=None,
        is_mention=True,
        is_everyone_mention=False,
        raw={},
        sender_is_agent=False,
    )
    asyncio.run(a._handle_push_message_turn(msg, "turn-1"))
    (wake,) = woke
    assert "[attachment: cat.png" in wake["data"]
    assert "media_urls" not in wake
    assert api.fetched == []
