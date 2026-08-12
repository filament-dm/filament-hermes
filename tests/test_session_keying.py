"""The shared_channel_sessions flag drives the base adapter's
session-grouping knob.

Hermes core keys shared-channel sessions per (channel, sender) when
``config.extra["group_sessions_per_user"]`` is True (its default). The
adapter honors the ``shared_channel_sessions`` feature flag by pointing
that knob at False before each turn — one session per channel, sender as a
label — with two invariants pinned here:

- explicit operator config always wins (a pinned knob is never touched),
- the flag is read fresh per turn (backchannel toggle → next wake, no
  restart), in both directions.

Modules are loaded standalone with the gateway stubbed (same pattern as
``test_thread_follow_up``).
"""

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

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
        sys.modules["hermes_filament_fcm.reactive"],
        sys.modules["hermes_filament_fcm.adapter"],
    )


reactive, adapter = _load_modules()


def _make(tmp: Path, *, extra: dict, pinned_by_operator: bool):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a.config = SimpleNamespace(extra=extra)
    a._feature_flags = reactive.FeatureFlagStore(tmp / "feature_flags.json")
    a._session_grouping_pinned = pinned_by_operator
    return a


def _enable(tmp: Path, enabled: bool) -> None:
    (tmp / "feature_flags.json").write_text(
        json.dumps({"shared_channel_sessions": enabled})
    )


def test_flag_on_points_grouping_at_shared():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        _enable(tmp, True)
        a._apply_session_keying()
        assert a.config.extra["group_sessions_per_user"] is False


def test_flag_off_writes_nothing():
    # No phantom default: absent = hermes core's own default. The plugin
    # only ever writes the knob while the flag is ON.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        a._apply_session_keying()
        assert a.config.extra == {}


def test_toggle_takes_effect_next_turn_both_directions():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        _enable(tmp, True)
        a._apply_session_keying()
        assert a.config.extra["group_sessions_per_user"] is False
        _enable(tmp, False)  # principal turns it back off…
        a._apply_session_keying()
        # …and both the knob and the managed marker are removed — absent
        # means core default again, no restart.
        assert a.config.extra == {}


def test_operator_pinned_config_is_never_touched():
    # A pin is an explicit, unmarked False (shared by config) — the one
    # value the engine's scaffold can't produce. It is never touched.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(
            tmp,
            extra={"group_sessions_per_user": False},
            pinned_by_operator=True,
        )
        _enable(tmp, True)
        a._apply_session_keying()
        assert a.config.extra == {"group_sessions_per_user": False}


def test_scaffolded_default_true_is_not_a_pin():
    # The engine writes group_sessions_per_user: true (its default) into
    # every config.yaml it scaffolds. Reading that as an operator pin
    # dead-letters the flag on every stock install (observed live: the
    # flag was on, the file said true, and keying stayed per-sender).
    extra = {"group_sessions_per_user": True}
    pinned = (
        extra.get("group_sessions_per_user") is False
        and adapter._SESSION_KEYING_MANAGED_KEY not in extra
    )
    assert pinned is False
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra=dict(extra), pinned_by_operator=pinned)
        _enable(tmp, True)
        a._apply_session_keying()
        # The scaffolded True is overridden like any unpinned config.
        assert a.config.extra["group_sessions_per_user"] is False
        assert a._shared_sessions_effective() is True


def test_partially_constructed_adapter_is_a_noop():
    # Instances built via __new__ without __init__ (other test files do
    # this) must never mutate config.
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._apply_session_keying()  # must not raise


def test_flag_residue_is_not_an_operator_pin():
    # The privacy regression the review caught: the flag writes into
    # config.extra, and a later adapter construction over the same config
    # object must NOT read that residue as an operator pin (which would
    # freeze shared sessions ON after the principal turned them off).
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        _enable(tmp, True)
        a._apply_session_keying()
        assert a.config.extra["group_sessions_per_user"] is False
        # The platform rebuilds the adapter over the same config object.
        extra = a.config.extra
        pinned = (
            "group_sessions_per_user" in extra
            and adapter._SESSION_KEYING_MANAGED_KEY not in extra
        )
        assert pinned is False  # managed residue, not an operator pin
        b = _make(tmp, extra=extra, pinned_by_operator=pinned)
        _enable(tmp, False)
        b._apply_session_keying()
        assert "group_sessions_per_user" not in b.config.extra


def test_operator_pin_without_marker_still_pins():
    extra = {"group_sessions_per_user": False}
    pinned = (
        extra.get("group_sessions_per_user") is False
        and adapter._SESSION_KEYING_MANAGED_KEY not in extra
    )
    assert pinned is True


def test_breadcrumb_consults_cursor_only_under_shared_sessions():
    # A channel-wide cursor only means "this conversation has seen it"
    # when the channel has one conversation. With per-sender keying (flag
    # off), one sender's fetch must not silence another sender's cue.
    import asyncio
    import json as _json

    class _API:
        @staticmethod
        def parse_tool_result(raw):
            return raw

        async def call_tool(self, name, arguments):
            return {
                "messages": [
                    {"event_id": "$a", "sender": "@x:s", "is_from_self": False},
                    {"event_id": "$b", "sender": "@y:s", "is_from_self": False},
                ]
            }

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
        a._filament_api = _API()
        a._feature_flags = reactive.FeatureFlagStore(tmp / "flags.json")
        a._channel_cursors = reactive.ChannelCursorStore(tmp / "cursors.json")
        a._channel_cursors.record("!room:s", "$b")  # fully caught up

        # Flag OFF: the cursor is ignored — windowed count, cue fires.
        crumb = asyncio.run(a._context_breadcrumb("!room:s", "$t"))
        assert crumb is not None and "recent message(s)" in crumb

        # Flag ON: the cursor applies — caught up, cue quiet.
        (tmp / "flags.json").write_text(
            _json.dumps({"shared_channel_sessions": True})
        )
        assert asyncio.run(a._context_breadcrumb("!room:s", "$t")) is None



def test_decoupled_keying_engages_cursor_on_default_style():
    # The phase-2 chain: with shared keying effective, a top-level wake on
    # a default ("thread") channel keys to the channel conversation, so
    # the cursor may record — the engagement condition is gone.
    keying, anchor = reactive.keying_and_reply(None, "$trig", "thread", True)
    assert keying is None and anchor == "$trig"
    assert reactive.conversation_key("!r:s", keying) == ("channel", "!r:s")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        _enable(tmp, True)
        assert a._cursor_channel_for_turn("!r:s", keying) == "!r:s"


def test_cursor_recording_scoped_to_shared_session_turns():
    # A cursor asserts "the CHANNEL's conversation has read up to here";
    # only a data turn that IS the channel's shared session may record one.
    # Under per-sender keying nothing may record — else a cursor laid down
    # before a keying flip marks the brand-new shared session as caught up
    # on messages it never saw (and outside any turn: fail-safe None).
    assert reactive.current_cursor_channel.get() is None
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a = _make(tmp, extra={}, pinned_by_operator=False)
        _enable(tmp, True)
        assert a._cursor_channel_for_turn("!room:s") == "!room:s"
        # A thread turn joins the thread's conversation, not the
        # channel's (conversation_key) — it never records, even with
        # shared keying on.
        assert a._cursor_channel_for_turn("!room:s", "$thread") is None
        _enable(tmp, False)
        assert a._cursor_channel_for_turn("!room:s") is None
        # An operator pin (explicit False = shared) records with the flag
        # off; the scaffolded True follows the flag like any unpinned
        # config.
        pinned_shared = _make(
            tmp,
            extra={"group_sessions_per_user": False},
            pinned_by_operator=True,
        )
        assert pinned_shared._cursor_channel_for_turn("!r:s") == "!r:s"
        scaffolded = _make(
            tmp,
            extra={"group_sessions_per_user": True},
            pinned_by_operator=False,
        )
        assert scaffolded._cursor_channel_for_turn("!r:s") is None


def test_effective_keying_pin_means_shared_scaffold_follows_flag():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # Operator pin (explicit False): shared by config, flag irrelevant.
        a = _make(
            tmp,
            extra={"group_sessions_per_user": False},
            pinned_by_operator=True,
        )
        _enable(tmp, False)
        assert a._shared_sessions_effective() is True
        # Scaffolded True is not a pin: the flag decides, both directions.
        b = _make(
            tmp,
            extra={"group_sessions_per_user": True},
            pinned_by_operator=False,
        )
        assert b._shared_sessions_effective() is False
        _enable(tmp, True)
        assert b._shared_sessions_effective() is True
        # Unpinned, no knob at all: the flag decides.
        c = _make(tmp, extra={}, pinned_by_operator=False)
        assert c._shared_sessions_effective() is True
        _enable(tmp, False)
        assert c._shared_sessions_effective() is False
