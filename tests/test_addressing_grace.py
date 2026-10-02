"""Addressing waits: in a mention-only channel, an unmentioned message the
server reads as addressed to the agent counts as a mention.

Adapter-level, like test_thread_follow_up (whose module loading and push
builder these reuse). Pinned here:

- the wait ends as soon as the judgement lands, and at _ADDRESSING_WAIT_S
  when it never does; "all" and "off" channels do not wait at all
- a human message judged as addressed to the agent wakes it, with no thread
  and no sender hint on the push
- an agent sender, or a row with no sender flag, wakes only when the server
  also reads a reply as expected (no "thanks" / "you're welcome" loops)
- a trigger pushed out of the history window is still found
- at most _MAX_ADDRESSING_HOLDS_PER_ROOM messages per room wait at once
"""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path


def _stub_hermes_cli() -> None:
    # The adapter imports setup_cli, which needs hermes_cli. Only some test
    # files stub it, so do it here rather than depend on collection order.
    if "hermes_cli.setup" in sys.modules:
        return
    pkg = types.ModuleType("hermes_cli")
    setup = types.ModuleType("hermes_cli.setup")
    setup.__getattr__ = lambda name: lambda *a, **k: None
    pkg.setup = setup
    sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.setup"] = setup


_stub_hermes_cli()

import test_thread_follow_up as base  # noqa: E402

adapter = base.adapter


def _row(
    event_id: str,
    *,
    addressed: bool,
    from_agent: "bool | None" = False,
    reply: "bool | None" = None,
) -> dict:
    row = {
        "event_id": event_id,
        "sender": base._HUMAN,
        "is_implicitly_mentioned": addressed,
        "reply_expected": addressed if reply is None else reply,
    }
    if from_agent is not None:
        row["is_from_agent"] = from_agent
    return row


class _ChannelAPI:
    """get_recent_messages in the real MCP envelope; the trigger shows up only
    in reads at least ``visible_from`` messages long."""

    def __init__(self, trigger: dict, visible_from: int = 1, judged_from: int = 1):
        self._trigger = trigger
        self._visible_from = visible_from
        self._judged_from = judged_from
        self.limits: list[int] = []

    async def call_tool(self, name, args):
        assert name == "get_recent_messages"
        limit = args["limit"]
        self.limits.append(limit)
        filler = [
            {"event_id": f"$other{i}", "sender": base._HUMAN, "is_from_agent": False}
            for i in range(limit)
        ]
        trigger = dict(self._trigger)
        if len(self.limits) < self._judged_from:
            trigger.pop("is_implicitly_mentioned", None)
            trigger.pop("reply_expected", None)
        messages = filler[: limit - 1] + (
            [trigger] if limit >= self._visible_from else filler[-1:]
        )
        return {
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"messages": messages})}
                ]
            }
        }


def _make(tmp: Path, api):
    a, woke = base._make_adapter(tmp, thread=None)
    a._filament_api = api
    a._addressing_holds = {}
    return a, woke


def _top_level(event_id: str = "$trigger"):
    return base._push(base._HUMAN, thread_id=None, event_id=event_id)


def _fast(monkeypatch, wait: float = 0.05) -> None:
    monkeypatch.setattr(adapter, "_ADDRESSING_WAIT_S", wait)
    monkeypatch.setattr(adapter, "_ADDRESSING_POLL_S", 0.01)


def test_addressed_human_message_wakes(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=True))
        a, woke = _make(Path(d), api)
        base._run(a, _top_level())
        assert len(woke) == 1
        assert a._addressing_holds == {}


def test_message_for_someone_else_stays_asleep(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        a, woke = _make(Path(d), _ChannelAPI(_row("$trigger", addressed=False)))
        base._run(a, _top_level())
        assert woke == []


def test_agent_sender_wakes_only_when_a_reply_is_expected(monkeypatch):
    _fast(monkeypatch)
    # Another agent, or a row without the sender flag, needs reply_expected.
    for from_agent, reply, expected_wakes in (
        (True, True, 1),
        (True, False, 0),
        (None, True, 1),
        (None, False, 0),
    ):
        with tempfile.TemporaryDirectory() as d:
            api = _ChannelAPI(
                _row("$trigger", addressed=True, from_agent=from_agent, reply=reply)
            )
            a, woke = _make(Path(d), api)
            base._run(a, _top_level())
            assert len(woke) == expected_wakes, (from_agent, reply)


def test_human_sender_wakes_without_a_reply_expected(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=True, reply=False))
        a, woke = _make(Path(d), api)
        base._run(a, _top_level())
        assert len(woke) == 1


def test_trigger_outside_the_window_is_still_found(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(
            _row("$trigger", addressed=True),
            visible_from=adapter._ADDRESSING_LOOKBACK,
        )
        a, woke = _make(Path(d), api)
        base._run(a, _top_level())
        assert len(woke) == 1
        assert api.limits == [
            adapter.BREADCRUMB_LIMIT,
            adapter._ADDRESSING_LOOKBACK,
        ]


def test_holds_per_room_are_capped(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=True))
        a, woke = _make(Path(d), api)
        a._addressing_holds = {"!shared": adapter._MAX_ADDRESSING_HOLDS_PER_ROOM}
        base._run(a, _top_level())
        # Past the cap the message is judged on mentions alone: no read, no wake.
        assert api.limits == []
        assert woke == []
        assert a._addressing_holds == {
            "!shared": adapter._MAX_ADDRESSING_HOLDS_PER_ROOM
        }


def test_concurrent_holds_stay_within_the_cap(monkeypatch):
    _fast(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=False))
        a, _ = _make(Path(d), api)
        waited = 0
        peak = 0
        real_wait = a._await_addressing

        async def counting_wait(msg):
            nonlocal waited, peak
            waited += 1
            peak = max(peak, a._addressing_holds.get("!shared", 0))
            return await real_wait(msg)

        a._await_addressing = counting_wait

        async def burst():
            await asyncio.gather(
                *(
                    a._handle_push_message_turn(_top_level(f"$m{i}"), f"turn-{i}")
                    for i in range(adapter._MAX_ADDRESSING_HOLDS_PER_ROOM + 3)
                )
            )

        asyncio.run(burst())
        # Only the capped number waited; the rest were judged at once.
        assert waited == adapter._MAX_ADDRESSING_HOLDS_PER_ROOM
        assert peak == adapter._MAX_ADDRESSING_HOLDS_PER_ROOM
        assert a._addressing_holds == {}


def test_the_wait_ends_when_the_judgement_lands(monkeypatch):
    _fast(monkeypatch, wait=5.0)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=True), judged_from=3)
        a, woke = _make(Path(d), api)
        base._run(a, _top_level())
        assert len(woke) == 1
        # Three reads, not five seconds of them.
        assert api.limits == [adapter.BREADCRUMB_LIMIT] * 3


def test_a_message_never_judged_stops_at_the_cap(monkeypatch):
    _fast(monkeypatch, wait=0.05)
    with tempfile.TemporaryDirectory() as d:
        api = _ChannelAPI(_row("$trigger", addressed=True), judged_from=10_000)
        a, woke = _make(Path(d), api)
        base._run(a, _top_level())
        assert woke == []
        assert 1 <= len(api.limits) <= 10
        assert a._addressing_holds == {}


def test_all_and_off_channels_do_not_wait(monkeypatch):
    _fast(monkeypatch)
    # "all" wakes without waiting (its one read is the turn's history);
    # "off" never wakes and reads nothing.
    for mode, expected_wakes, expected_reads in (("all", 1, 1), ("off", 0, 0)):
        with tempfile.TemporaryDirectory() as d:
            api = _ChannelAPI(_row("$trigger", addressed=True))
            a, woke = _make(Path(d), api)
            (Path(d) / "wake.json").write_text(json.dumps({"reactive_wake": mode}))
            base._run(a, _top_level())
            assert len(api.limits) == expected_reads
            assert len(woke) == expected_wakes
