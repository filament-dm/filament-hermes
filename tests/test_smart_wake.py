"""A message the server judged as meant for the agent wakes it without an
@-mention, on both transports, and nothing weaker does.

The invariants:

- poll row, aimed at the agent, from a person            → wakes
- push, aimed at the agent (read from get_thread)        → wakes
- aimed at the agent, from another agent, no reply wanted → stays asleep
- aimed at the agent, from another agent, reply wanted   → wakes
- principal would want to see it (fyi), from a person    → wakes
- fyi from another agent                                 → stays asleep
- "decide" from another agent                            → wakes
- delivered but not judged for the agent                 → stays asleep
- unknown sender                                         → treated as an agent
- smart_wake="off"                                       → stays asleep
- muted channel (reactive_wake="off")                    → stays asleep
- get_thread unavailable on a push                       → stays asleep
"""

import json
import tempfile
from pathlib import Path

from test_thread_follow_up import (
    _HUMAN,
    _OTHER_BOT,
    _make_adapter,
    _push,
    _run,
    reactive,
)


def _row(**flags) -> dict:
    return {"event_id": "$msg", "sender": _HUMAN, "body": "can you check?", **flags}


def _poll(sender: str, sender_is_agent: bool | None, **flags):
    msg = _push(
        sender, thread_id=None, event_id="$msg", sender_is_agent=sender_is_agent
    )
    msg.raw = {"poll_work": _row(**flags)}
    return msg


def _woke(msg, *, policy: dict | None = None, thread: dict | None = None):
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a, woke = _make_adapter(tmp, thread)
        if policy is not None:
            (tmp / "wake.json").write_text(json.dumps(policy))
        _run(a, msg)
        return woke, a


def test_poll_row_aimed_at_agent_wakes():
    woke, a = _woke(_poll(_HUMAN, False, is_implicitly_mentioned=True))
    assert len(woke) == 1
    assert "meant for you" in woke[0]["trigger"]
    # A judged wake is not a mention: it engages no thread.
    assert not a._engaged_threads.is_engaged("!shared", "$msg")


def test_push_reads_the_judgement_from_get_thread():
    thread = {
        "root": {
            "event_id": "$msg",
            "sender": _HUMAN,
            "is_from_agent": False,
            "is_implicitly_mentioned": True,
            "reply_expected": True,
        },
        "replies": [],
    }
    msg = _push(_HUMAN, thread_id=None, event_id="$msg")
    woke, a = _woke(msg, thread=thread)
    assert len(woke) == 1
    assert a._filament_api.calls == 1


def test_another_agents_remark_stays_asleep():
    woke, _ = _woke(_poll(_OTHER_BOT, True, is_implicitly_mentioned=True))
    assert woke == []


def test_another_agent_wanting_a_reply_wakes():
    woke, _ = _woke(
        _poll(_OTHER_BOT, True, is_implicitly_mentioned=True, reply_expected=True)
    )
    assert len(woke) == 1


def test_fyi_from_a_person_wakes():
    woke, _ = _woke(
        _poll(
            _HUMAN,
            False,
            is_implicitly_mentioned=False,
            consider_forwarding_to_principal="fyi",
        )
    )
    assert len(woke) == 1
    assert "principal" in woke[0]["trigger"]


def test_fyi_from_another_agent_stays_asleep():
    woke, _ = _woke(_poll(_OTHER_BOT, True, consider_forwarding_to_principal="fyi"))
    assert woke == []


def test_decide_from_another_agent_wakes():
    woke, _ = _woke(_poll(_OTHER_BOT, True, consider_forwarding_to_principal="decide"))
    assert len(woke) == 1


def test_delivery_alone_stays_asleep():
    woke, _ = _woke(
        _poll(_HUMAN, False, is_implicitly_mentioned=False, reply_expected=False)
    )
    assert woke == []
    woke, _ = _woke(_poll(_HUMAN, False))
    assert woke == []


def test_unknown_sender_is_treated_as_an_agent():
    woke, _ = _woke(_poll(_HUMAN, None, is_implicitly_mentioned=True))
    assert woke == []


def test_smart_wake_off_ignores_the_judgement():
    msg = _poll(_HUMAN, False, is_implicitly_mentioned=True)
    woke, _ = _woke(msg, policy={"smart_wake": "off"})
    assert woke == []
    woke, _ = _woke(msg, policy={"per_channel": {"!shared": {"smart_wake": "off"}}})
    assert woke == []


def test_muted_channel_wins():
    woke, _ = _woke(
        _poll(_HUMAN, False, is_implicitly_mentioned=True),
        policy={"reactive_wake": "off"},
    )
    assert woke == []


def test_push_without_the_api_stays_asleep():
    woke, _ = _woke(_push(_HUMAN, thread_id=None, event_id="$msg"), thread=None)
    assert woke == []


def test_unrecognized_smart_wake_value_is_off():
    with tempfile.TemporaryDirectory() as d:
        store = reactive.WakePolicyStore(Path(d) / "wake.json")
        assert store.smart_wake("!shared") == "on"
        store.write({"smart_wake": "sometimes"})
        assert store.smart_wake("!shared") == "off"


def test_row_lookup_in_a_thread_payload():
    thread = {"root": {"event_id": "$a"}, "replies": [{"event_id": "$b"}, "junk"]}
    assert reactive.message_row_in_thread(thread, "$b") == {"event_id": "$b"}
    assert reactive.message_row_in_thread(thread, "$c") is None
    assert reactive.message_row_in_thread(None, "$a") is None
