"""poll_transport: parsing poll_work, and the loop's cursor, backoff and
fatal handling. Loaded standalone (stdlib-only module, no Hermes)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "poll_transport.py"
)
_spec = importlib.util.spec_from_file_location("poll_transport_standalone", _PATH)
pt = importlib.util.module_from_spec(_spec)
sys.modules["poll_transport_standalone"] = pt
_spec.loader.exec_module(pt)


def _message_item(**overrides):
    item = {
        "kind": "message",
        "channel_id": "!room:s",
        "thread_id": None,
        "is_backchannel": False,
        "messages": [
            {
                "event_id": "$1",
                "sender": "@bob:s",
                "body": "hi @agent",
                "timestamp": 1,
                "is_mention": True,
                "is_from_agent": False,
            }
        ],
        "reply_with": {"tool": "post_message", "args": {"channel": "!room:s"}},
    }
    item.update(overrides)
    return item


# ── configuration ───────────────────────────────────────────────────


def test_transport_defaults_to_fcm():
    assert pt.transport_from_env({}) == "fcm"
    assert pt.transport_from_env({"FILAMENT_TRANSPORT": ""}) == "fcm"
    assert pt.transport_from_env({"FILAMENT_TRANSPORT": "carrier-pigeon"}) == "fcm"
    assert pt.transport_from_env({"FILAMENT_TRANSPORT": " Poll "}) == "poll"


# ── parsing ─────────────────────────────────────────────────────────


def test_parses_every_kind():
    response = pt.parse_response(
        {
            "work": [
                _message_item(thread_id="$root", is_backchannel=True),
                {
                    "kind": "reaction",
                    "channel_id": "!room:s",
                    "thread_id": None,
                    "reactions": [
                        {
                            "event_id": "$r",
                            "sender": "@bob:s",
                            "key": "👍",
                            "target_event_id": "$1",
                        }
                    ],
                    "reply_with": None,
                },
                {
                    "kind": "invite",
                    "loop_id": "!loop:s",
                    "name": "Acme",
                    "invited_by": "@alice:s",
                },
                {
                    "kind": "vouch",
                    "loop_id": "!loop2:s",
                    "name": None,
                    "vouched_by": "@carol:s",
                },
            ],
            "cursor": "c:abc",
            "next_poll_ms": 1000,
        }
    )
    message, reaction, invite, vouch = response.work
    assert message.kind == "message" and message.thread_id == "$root"
    assert message.is_backchannel is True
    assert message.is_direct is None  # today's server doesn't say
    assert message.messages[0]["event_id"] == "$1"
    assert reaction.reactions[0]["key"] == "👍" and reaction.messages == []
    assert (invite.loop_id, invite.name, invite.sender) == (
        "!loop:s",
        "Acme",
        "@alice:s",
    )
    assert (vouch.loop_id, vouch.name, vouch.sender) == ("!loop2:s", None, "@carol:s")
    assert response.cursor == "c:abc"
    assert response.next_poll_ms == 1000


def test_reads_is_direct_when_the_server_sends_it():
    (item,) = pt.parse_response({"work": [_message_item(is_direct=True)]}).work
    assert item.is_direct is True


def test_a_kindless_item_from_an_older_server_is_a_message():
    raw = _message_item()
    del raw["kind"]
    (item,) = pt.parse_response({"work": [raw], "cursor": "c"}).work
    assert item.kind == "message"


def test_unusable_items_are_skipped():
    response = pt.parse_response(
        {
            "work": [
                "junk",
                {"kind": "message", "messages": [{"event_id": "$1"}]},  # no channel
                _message_item(messages=[{"body": "no id"}]),
                {"kind": "invite"},  # no loop
                {"kind": "future-kind", "channel_id": "!r:s"},
                _message_item(),
            ],
            "cursor": "c",
        }
    )
    assert [item.kind for item in response.work] == ["message"]


def test_not_a_response():
    assert pt.parse_response(None) is None
    assert pt.parse_response({"cursor": "c"}) is None
    assert pt.parse_response({"work": "nope"}) is None


def test_everyone_mentioned():
    assert pt.everyone_mentioned("@room heads up")
    assert pt.everyone_mentioned("hi @Everyone!")
    assert not pt.everyone_mentioned("email me at x@room.example")
    assert not pt.everyone_mentioned("")


# ── room details ────────────────────────────────────────────────────


def _details(**overrides):
    data = {
        "channel_id": "!room:s",
        "name": "general",
        "loop_id": "!loop:s",
        "loop_name": "Acme",
        "member_count": 3,
        "members": [
            {"mxid": "@bob:s", "display_name": "Bob"},
            {"mxid": "@agent:s", "display_name": "Hermes"},
            {"mxid": "@x:s", "display_name": None},
        ],
    }
    data.update(overrides)
    return data


def test_room_info_from_a_channel():
    info = pt.room_info_from_details(_details())
    assert info.name == "general"
    assert info.loop_name == "Acme"
    assert info.is_direct is False
    assert info.members == {"@bob:s": "Bob", "@agent:s": "Hermes", "@x:s": ""}


def test_a_two_person_room_without_loop_or_name_reads_as_direct():
    two = [{"mxid": "@owner:s"}, {"mxid": "@agent:s"}]
    dm = _details(name=None, loop_id=None, loop_name=None, member_count=2, members=two)
    assert pt.room_info_from_details(dm).is_direct is True
    # A named one, a looped one, or a bigger one is not.
    assert pt.room_info_from_details({**dm, "name": "Plans"}).is_direct is False
    assert pt.room_info_from_details({**dm, "loop_id": "!l:s"}).is_direct is False
    assert pt.room_info_from_details({**dm, "member_count": 3}).is_direct is False


def test_unreadable_details_are_an_empty_room():
    assert pt.room_info_from_details("nope") == pt.RoomInfo()


# ── the loop ────────────────────────────────────────────────────────


class _Server:
    """poll_work answers, in order; each is (data, error) or an exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, args):
        self.calls.append(dict(args))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _loop(server, dispatched=None, slept=None):
    async def dispatch(item):
        if dispatched is not None:
            dispatched.append(item)

    async def sleep(seconds):
        if slept is not None:
            slept.append(seconds)

    return pt.PollLoop(server, dispatch, sleep=sleep)


def _run(loop, polls):
    async def go():
        for _ in range(polls):
            await loop.poll_once()

    asyncio.run(go())


def test_dispatches_work_and_carries_the_cursor():
    server = _Server(
        ({"work": [_message_item()], "cursor": "c:1", "next_poll_ms": 1000}, None),
        ({"work": [], "cursor": "c:2", "next_poll_ms": 1000}, None),
    )
    dispatched, slept = [], []
    loop = _loop(server, dispatched, slept)
    _run(loop, 2)
    assert [i.kind for i in dispatched] == ["message"]
    assert "cursor" not in server.calls[0]
    # Without a cursor the first poll doesn't wait; with one, polls do.
    assert server.calls[0]["wait_seconds"] == 0
    assert server.calls[1]["cursor"] == "c:1"
    assert server.calls[1]["wait_seconds"] == pt.DEFAULT_WAIT_SECONDS
    assert loop.cursor == "c:2"
    # Work goes straight to the next poll; an empty answer takes the hint.
    assert slept == [1.0]


def test_an_empty_answer_never_spins():
    server = _Server(({"work": [], "cursor": "c", "next_poll_ms": 0}, None))
    slept = []
    _run(_loop(server, slept=slept), 1)
    assert slept == [0.2]


def test_errors_back_off_and_keep_the_cursor():
    server = _Server(
        ({"work": [], "cursor": "c:1"}, None),
        (None, "HTTP 502"),
        ConnectionError("reset"),
        ({"weird": True}, None),
        ({"work": [], "cursor": "c:2"}, None),
    )
    slept = []
    loop = _loop(server, slept=slept)
    _run(loop, 5)
    assert slept == [0.2, 1.0, 2.0, 4.0, 0.2]
    assert [c.get("cursor") for c in server.calls] == [None, "c:1", "c:1", "c:1", "c:1"]
    assert loop.failures == 0


def test_backoff_is_capped():
    server = _Server(*[(None, "HTTP 500")] * 9)
    slept = []
    _run(_loop(server, slept=slept), 9)
    assert slept[-1] == 60.0
    assert max(slept) == 60.0


def test_auth_failure_is_fatal_but_retryable():
    with pytest.raises(pt.PollFatal) as raised:
        _run(_loop(_Server((None, "HTTP 401"))), 1)
    assert raised.value.retryable is True


def test_a_server_without_poll_work_is_fatal():
    with pytest.raises(pt.PollFatal) as raised:
        _run(_loop(_Server((None, "Unknown tool: poll_work"))), 1)
    assert raised.value.retryable is False
    assert "FILAMENT_TRANSPORT" in str(raised.value)


def test_a_dispatch_failure_does_not_stop_the_batch():
    seen = []

    async def dispatch(item):
        seen.append(item.channel_id)
        if item.channel_id == "!a:s":
            raise RuntimeError("boom")

    server = _Server(
        (
            {
                "work": [
                    _message_item(channel_id="!a:s"),
                    _message_item(channel_id="!b:s"),
                ],
                "cursor": "c",
            },
            None,
        )
    )

    async def sleep(_):
        return None

    asyncio.run(pt.PollLoop(server, dispatch, sleep=sleep).poll_once())
    assert seen == ["!a:s", "!b:s"]


# ── a draining gateway ──────────────────────────────────────────────


class _WorkServer:
    """poll_work's cursor rules (synapse agents_mcp/tools_poll.py and
    work_done.py), for one room: a poll passing cursor C, or none (the saved
    position), finishes everything up to C and every delivery whose cursor
    is at or before C, then hands over what is after C and unfinished. A
    reply finishes its message."""

    def __init__(self):
        # A live server's stream is never empty; a cursor at 0 reads as none.
        self.head = 100
        self.calls = []
        self.events = []  # (stream ordering, event id)
        self.saved = 0
        self.done = 0
        self.deliveries = []  # (newest stream ordering handed over, cursor)
        self.before_answer = None

    def post(self, event_id):
        self.head += 1
        self.events.append((self.head, event_id))

    def reply(self, event_id):
        so = next(so for so, eid in self.events if eid == event_id)
        self.done = max(self.done, so)

    async def __call__(self, args):
        self.calls.append(dict(args))
        given = int(args["cursor"][2:]) if args.get("cursor") else 0
        start = given or self.saved
        self.done = max(self.done, start)
        for so, cursor in [d for d in self.deliveries if d[1] <= start]:
            self.done = max(self.done, so)
            self.deliveries.remove((so, cursor))
        if self.before_answer:
            # Whatever happens while the poll is held: a drain, a message.
            self.before_answer()
            self.before_answer = None
        work = [(so, eid) for so, eid in self.events if so > start and so > self.done]
        if work:
            self.deliveries.append((work[-1][0], self.head))
            self.saved = self.head
        items = [_message_item(messages=[{"event_id": eid}]) for _, eid in work]
        return {"work": items, "cursor": f"c:{self.head}", "next_poll_ms": 1000}, None


def _gateway(server, *, cursor=None, admitting=lambda: True, kept=None):
    """A PollLoop whose dispatched event ids land in ``loop.ran``."""

    async def dispatch(item):
        loop.ran.extend(m["event_id"] for m in item.messages)

    async def sleep(_):
        return None

    loop = pt.PollLoop(
        server,
        dispatch,
        sleep=sleep,
        cursor=cursor,
        admitting=admitting,
        keep_cursor=None if kept is None else kept.append,
    )
    loop.ran = []
    return loop


def test_work_that_arrives_during_a_drain_reaches_the_next_gateway():
    server = _WorkServer()
    server.post("$handled")
    draining = []
    kept = []
    old = _gateway(server, admitting=lambda: not draining, kept=kept)
    _run(old, 1)
    assert old.ran == ["$handled"]
    server.reply("$handled")

    # The drain begins while the next poll is held, and the owner's DM lands
    # in it (live run h30-r2-reconnect-3).
    server.before_answer = lambda: (draining.append(True), server.post("$dm"))
    _run(old, 3)
    assert old.ran == ["$handled"], "a draining gateway runs no new turn"
    assert len(server.calls) == 2, "and polls no more"
    assert kept == ["c:101"], "the cursor from before the delivery is saved"
    assert old.cursor == "c:101"

    new = _gateway(server, cursor=kept[-1], kept=kept)
    _run(new, 1)
    assert new.ran == ["$dm"], "the server hands the DM over again, only it"
    assert kept == ["c:101", None], "the saved cursor is dropped once used"
    _run(new, 1)
    assert new.ran == ["$dm"]


def test_without_the_hold_a_drain_loses_the_work():
    """What the loop did before it asked whether the gateway admits turns:
    it handed the DM to a gateway that refused it, and the next gateway's
    cursorless poll finished it."""
    server = _WorkServer()
    server.post("$handled")
    old = _gateway(server)
    _run(old, 1)
    server.reply("$handled")
    server.before_answer = lambda: server.post("$dm")
    _run(old, 1)
    assert old.ran == ["$handled", "$dm"]  # refused by the draining gateway
    new = _gateway(server)
    _run(new, 2)
    assert new.ran == []


def test_a_draining_gateway_sends_no_poll():
    server = _Server()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    async def dispatch(_):
        raise AssertionError("dispatched while draining")

    loop = pt.PollLoop(server, dispatch, sleep=sleep, admitting=lambda: False)
    _run(loop, 3)
    assert server.calls == []
    assert slept == [1.0, 1.0, 1.0]
    assert loop.holding is True


def test_work_left_before_any_cursor_is_not_saved(caplog):
    draining = []
    server = _WorkServer()
    server.before_answer = lambda: (draining.append(True), server.post("$dm"))
    kept = []
    loop = _gateway(server, admitting=lambda: not draining, kept=kept)
    with caplog.at_level("WARNING", logger="gateway.filament_fcm"):
        _run(loop, 1)
    assert loop.ran == [] and kept == []
    assert "may not come back" in caplog.text


def test_a_gateway_that_admits_again_drops_the_saved_cursor():
    admitting = [True]
    server = _WorkServer()
    kept = []
    loop = _gateway(server, admitting=lambda: admitting[0], kept=kept)
    _run(loop, 1)
    server.before_answer = lambda: (admitting.__setitem__(0, False), server.post("$a"))
    _run(loop, 1)
    assert kept == ["c:100"] and loop.ran == []
    admitting[0] = True
    _run(loop, 1)
    assert loop.ran == ["$a"]
    assert kept == ["c:100", None]


def test_a_failure_to_save_the_cursor_is_only_a_warning(caplog):
    draining = []
    server = _WorkServer()

    def keep(_):
        raise OSError("read-only")

    async def dispatch(_):
        return None

    async def sleep(_):
        return None

    loop = pt.PollLoop(
        server, dispatch, sleep=sleep, admitting=lambda: not draining, keep_cursor=keep
    )
    _run(loop, 1)
    server.before_answer = lambda: (draining.append(True), server.post("$dm"))
    with caplog.at_level("WARNING", logger="gateway.filament_fcm"):
        _run(loop, 2)
    assert "could not save the poll_work cursor" in caplog.text


def test_classify_error():
    assert pt.classify_error("HTTP 403") == "auth"
    assert pt.classify_error("Unauthorized") == "auth"
    # The server's 401 body once the agent is deleted (live run h20-r4-delete).
    assert (
        pt.classify_error(
            "Authentication required. See WWW-Authenticate header for OAuth discovery."
        )
        == "auth"
    )
    assert pt.classify_error("Method not found: tools/call") == "unsupported"
    assert pt.classify_error("HTTP 429") == "retry"
    assert pt.classify_error("invalid response") == "retry"


def test_a_dropped_connection_now_and_then_is_not_a_warning(caplog):
    caplog.set_level("DEBUG", logger="gateway.filament_fcm")
    server = _Server(
        ({"work": [], "cursor": "c:1"}, None),
        ConnectionError(),
        ({"work": [], "cursor": "c:2"}, None),
    )
    loop = _loop(server, slept=[])
    _run(loop, 3)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    # Same cursor on the retry: the server hands the same work again.
    assert server.calls[2]["cursor"] == "c:1"
    assert any("connection closed" in r.getMessage() for r in caplog.records)


def test_a_run_of_failures_is_a_warning(caplog):
    caplog.set_level("DEBUG", logger="gateway.filament_fcm")
    server = _Server(*[(None, "HTTP 502")] * 3)
    _run(_loop(server, slept=[]), 3)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "failure 3" in warnings[0].getMessage()


def test_a_server_that_describes_the_room_needs_no_lookup():
    (item,) = pt.parse_response(
        {
            "work": [
                _message_item(
                    is_direct=False,
                    channel_name="general",
                    loop_name="Acme",
                    sender_names={"@bob:s": "Bob", "@x:s": None},
                )
            ]
        }
    ).work
    info = pt.room_info(item)
    assert info == pt.RoomInfo(
        name="general",
        loop_name="Acme",
        is_direct=False,
        members={"@bob:s": "Bob", "@x:s": ""},
    )


def test_an_older_server_item_needs_the_lookup():
    (item,) = pt.parse_response({"work": [_message_item()]}).work
    assert pt.room_info(item) is None
