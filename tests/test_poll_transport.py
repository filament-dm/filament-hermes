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
    assert server.calls[0]["wait_seconds"] == pt.DEFAULT_WAIT_SECONDS
    assert server.calls[1]["cursor"] == "c:1"
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


def test_classify_error():
    assert pt.classify_error("HTTP 403") == "auth"
    assert pt.classify_error("Unauthorized") == "auth"
    assert pt.classify_error("Method not found: tools/call") == "unsupported"
    assert pt.classify_error("HTTP 429") == "retry"
    assert pt.classify_error("invalid response") == "retry"
