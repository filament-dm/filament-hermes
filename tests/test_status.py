"""Tests for the auto-status narrator (hermes_filament_fcm.status)."""

import asyncio
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "status",
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "status.py",
)
status = importlib.util.module_from_spec(_spec)
sys.modules["status"] = status
_spec.loader.exec_module(status)

MIN_INTERVAL_SECONDS = status.MIN_INTERVAL_SECONDS
REFRESH_SECONDS = status.REFRESH_SECONDS
phrase_for = status.phrase_for
should_publish = status.should_publish


class TestPhrases:
    def test_parameterized_search(self):
        assert (
            phrase_for("search_messages", {"query": "deploy failures"})
            == 'searching messages for "deploy failures"'
        )

    def test_web_search(self):
        assert (
            phrase_for("web_search", {"query": "matrix sliding sync"})
            == 'searching the web for "matrix sliding sync"'
        )

    def test_file_paths_reduce_to_basename(self):
        line = phrase_for("read_file", {"path": "/a/b/c/notes.md"})
        assert line == "reading notes.md"
        assert phrase_for("patch", {"path": "src/lib.rs"}) == "editing lib.rs"

    def test_terminal_uses_command_summary(self):
        line = phrase_for("terminal", {"command": "grep -r foo ."})
        assert line is not None and line.startswith("running ")

    def test_long_args_are_clipped(self):
        line = phrase_for("search_messages", {"query": "x" * 500})
        assert line is not None and len(line) <= 60

    def test_newlines_collapse(self):
        line = phrase_for("search_messages", {"query": "a\nb\n\nc"})
        assert line == 'searching messages for "a b c"'

    def test_missing_arg_falls_back_to_bare_template(self):
        assert phrase_for("search_messages", {}) == "searching messages for"
        assert phrase_for("web_extract", {}) == "reading"

    def test_unmapped_tool_is_humanized(self):
        assert phrase_for("browser_click", {"ref": "x"}) == "using browser click"

    def test_silent_tools_publish_nothing(self):
        for tool in ("get_self", "mark_read", "set_status", "tool_search"):
            assert phrase_for(tool, {}) is None


class TestCoalescing:
    def test_new_phrase_publishes_after_floor(self):
        assert should_publish("a", 0.0, "b", MIN_INTERVAL_SECONDS + 0.1)

    def test_floor_suppresses_bursts(self):
        assert not should_publish("a", 10.0, "b", 10.0 + MIN_INTERVAL_SECONDS / 2)

    def test_same_phrase_waits_for_refresh(self):
        assert not should_publish("a", 0.0, "a", REFRESH_SECONDS - 1)
        assert should_publish("a", 0.0, "a", REFRESH_SECONDS + 0.1)

    def test_first_publish_goes_out(self):
        assert should_publish(None, 0.0, "a", MIN_INTERVAL_SECONDS + 0.1)


class _FakeAPI:
    def __init__(self):
        self.calls = []

    async def set_status(self, **kwargs):
        self.calls.append(kwargs)


def _run(coro):
    asyncio.new_event_loop().run_until_complete(coro)


class TestBinding:
    def _publisher(self):
        pub = status.StatusPublisher()
        pub.set_api(_FakeAPI())
        return pub

    def test_first_tool_call_claims_pending_scope(self):
        pub = self._publisher()
        scope = status.TurnScope(room_id="!r", thread_id="$t", prompt_event_id="$m")
        pub.begin_turn("$m", scope)
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        assert pub._bound["sess1"].scope == scope
        assert "$m" not in pub._pending

    def test_early_completion_within_grace_is_ignored(self):
        pub = self._publisher()
        pub.begin_turn("$m", status.TurnScope(room_id="!r"))
        _run(pub.end_turn("$m"))
        assert "$m" in pub._pending

    def test_completion_after_grace_clears_unclaimed_turn(self):
        pub = self._publisher()
        pub.begin_turn("$m", status.TurnScope(room_id="!r"))
        pub._pending["$m"].created -= status.COMPLETION_GRACE_SECONDS + 1
        _run(pub.end_turn("$m"))
        assert "$m" not in pub._pending

    def test_completion_of_bound_turn_clears_it(self):
        pub = self._publisher()
        pub.begin_turn("$m", status.TurnScope(room_id="!r"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        _run(pub.end_turn("$m"))
        assert "sess1" not in pub._bound
        assert "$m" not in pub._trigger_session

    def test_duplicate_completion_is_a_noop(self):
        pub = self._publisher()
        pub.begin_turn("$m", status.TurnScope(room_id="!r"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        _run(pub.end_turn("$m"))
        _run(pub.end_turn("$m"))
