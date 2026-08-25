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
        async def go():
            pub = self._publisher()
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await pub.end_turn("$m")
            assert "$m" in pub._pending
            entry = pub._pending["$m"]
            assert entry.completed_early
            entry.finalize_task.cancel()

        asyncio.run(go())

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


class TestMcpAndScopePhrases:
    def test_mcp_tools_read_as_actions(self):
        assert phrase_for("mcp_linear_list_issues", {}) == "listing issues in Linear"
        assert phrase_for("mcp_linear_get_issue", {}) == "fetching issue in Linear"
        assert (
            phrase_for("mcp_linear_create_attachment_from_upload", {})
            == "creating attachment from upload in Linear"
        )

    def test_mcp_unknown_verb_falls_back_to_server(self):
        assert phrase_for("mcp_linear_frobnicate_widget", {}) == "using Linear"

    def test_reading_own_room_is_catching_up(self):
        line = phrase_for(
            "get_recent_messages", {"channel": "!cc:hs"}, scope_room="!cc:hs"
        )
        assert line == "catching up on the conversation"

    def test_reading_another_room_stays_generic(self):
        line = phrase_for(
            "get_recent_messages", {"channel": "!other:hs"}, scope_room="!cc:hs"
        )
        assert line == "reading the channel"


class TestGenericMcp:
    def test_any_server_any_verb(self):
        assert phrase_for("mcp_notion_search_pages", {"query": "roadmap"}) == (
            'searching pages "roadmap" in Notion'
        )
        assert phrase_for("mcp_github_create_pull_request", {}) == (
            "creating pull request in Github"
        )

    def test_unknown_verb_names_the_server(self):
        assert phrase_for("mcp_notion_frobnicate", {}) == "using Notion"


class TestUrlPrivacy:
    def test_domain_strips_userinfo(self):
        line = phrase_for(
            "browser_navigate", {"url": "https://user:secret@example.com/x"}
        )
        assert line == "reading example.com"
        assert "secret" not in line


class TestClaimSafety:
    def _publisher(self):
        pub = status.StatusPublisher()
        pub.set_api(_FakeAPI())
        return pub

    def test_ambiguous_claim_narrates_nothing(self):
        pub = self._publisher()
        pub.begin_turn("$a", status.TurnScope(room_id="!A"))
        pub.begin_turn("$b", status.TurnScope(room_id="!B"))
        pub.on_tool_call("web_search", {"query": "x"}, "snew")
        assert "snew" not in pub._bound
        assert "$a" in pub._pending and "$b" in pub._pending

    def test_known_session_claims_only_its_room(self):
        pub = self._publisher()
        pub.begin_turn("$a1", status.TurnScope(room_id="!A"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        _run(pub.end_turn("$a1"))
        pub.begin_turn("$b", status.TurnScope(room_id="!B"))
        pub.begin_turn("$a2", status.TurnScope(room_id="!A"))
        pub.on_tool_call("web_search", {"query": "y"}, "sess1")
        assert pub._bound["sess1"].scope.room_id == "!A"
        assert "$b" in pub._pending

    def test_known_session_never_steals_another_room(self):
        pub = self._publisher()
        pub.begin_turn("$a1", status.TurnScope(room_id="!A"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        _run(pub.end_turn("$a1"))
        pub.begin_turn("$b", status.TurnScope(room_id="!B"))
        pub.on_tool_call("web_search", {"query": "y"}, "sess1")
        assert "sess1" not in pub._bound
        assert "$b" in pub._pending


class TestEarlyCompletion:
    def _publisher(self):
        api = _FakeAPI()
        pub = status.StatusPublisher()
        pub.set_api(api)
        return pub, api

    def test_spurious_completion_still_claimable_within_grace(self):
        async def go():
            pub, _ = self._publisher()
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await pub.end_turn("$m")
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            entry = pub._bound["sess1"]
            assert entry.completed_early is False
            entry.finalize_task.cancel()

        asyncio.run(go())

    def test_early_completed_turn_not_claimable_past_grace(self):
        async def go():
            pub, _ = self._publisher()
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await pub.end_turn("$m")
            entry = pub._pending["$m"]
            entry.created -= status.COMPLETION_GRACE_SECONDS + 1
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            assert "sess1" not in pub._bound
            entry.finalize_task.cancel()

        asyncio.run(go())

    def test_early_completed_turn_is_finalized_by_next_prune(self):
        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await pub.end_turn("$m")
            pub._pending["$m"].created -= status.COMPLETION_GRACE_SECONDS + 1
            pub.begin_turn("$n", status.TurnScope(room_id="!r2"))
            assert "$m" not in pub._pending
            await asyncio.sleep(0.05)
            clears = [
                c
                for c in api.calls
                if c.get("channel") == "!r" and "status_text" not in c
            ]
            assert clears
            await pub.end_turn("$n")

        asyncio.run(go())


class TestPublishLifecycle:
    def test_end_turn_cancels_inflight_publishes(self):
        class SlowAPI:
            def __init__(self):
                self.calls = []

            async def set_status(self, **kwargs):
                if kwargs.get("status_text"):
                    await asyncio.sleep(0.5)
                self.calls.append(kwargs)

        async def go():
            api = SlowAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            await asyncio.sleep(0.01)
            await pub.end_turn("$m")
            await asyncio.sleep(0.1)
            assert not [c for c in api.calls if c.get("status_text")]
            assert api.calls and "status_text" not in api.calls[-1]

        asyncio.run(go())

    def test_refresh_republishes_during_long_gaps(self, monkeypatch):
        monkeypatch.setattr(status, "REFRESH_SECONDS", 0.04)

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await asyncio.sleep(0.15)
            entry = pub._pending["$m"]
            entry.created -= status.COMPLETION_GRACE_SECONDS + 1
            await pub.end_turn("$m")
            texted = [c for c in api.calls if c.get("status_text")]
            assert len(texted) >= 2
            assert all(c["status_text"] == "reading the conversation" for c in texted)
            assert entry.ended and entry.refresh_task.cancelled

        asyncio.run(go())

    def test_fast_toolless_turn_is_cleared_when_grace_expires(self, monkeypatch):
        monkeypatch.setattr(status, "COMPLETION_GRACE_SECONDS", 0.05)

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            await pub.end_turn("$m")
            assert "$m" in pub._pending
            await asyncio.sleep(0.15)
            assert "$m" not in pub._pending
            clears = [
                c
                for c in api.calls
                if c.get("channel") == "!r" and "status_text" not in c
            ]
            assert clears

        asyncio.run(go())


class TestSuppressedPhraseIsNotLost:
    """A phrase the floor suppresses must still reach the channel: the
    tool that trips the floor is often the long one, and dropping its
    phrase leaves the previous operation on screen for the whole turn."""

    def test_queued_phrase_publishes_when_the_floor_expires(self, monkeypatch):
        monkeypatch.setattr(status, "MIN_INTERVAL_SECONDS", 0.05)

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub.on_tool_call("web_search", {"query": "first"}, "sess1")
            # Inside the floor: held, not published yet.
            pub.on_tool_call("terminal", {"command": "cargo build"}, "sess1")
            assert pub._bound["sess1"].queued_phrase is not None
            texts = [c.get("status_text") for c in api.calls]
            assert not any(t and t.startswith("running") for t in texts)
            await asyncio.sleep(0.15)
            texts = [c.get("status_text") for c in api.calls]
            assert any(t and t.startswith("running") for t in texts)

        asyncio.run(go())

    def test_a_later_publish_supersedes_the_queued_phrase(self, monkeypatch):
        monkeypatch.setattr(status, "MIN_INTERVAL_SECONDS", 0.05)

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub.on_tool_call("web_search", {"query": "first"}, "sess1")
            pub.on_tool_call("terminal", {"command": "cargo build"}, "sess1")
            entry = pub._bound["sess1"]
            entry.last_ts -= status.MIN_INTERVAL_SECONDS
            pub.on_tool_call("read_file", {"path": "/a/notes.md"}, "sess1")
            assert entry.queued_phrase is None
            await asyncio.sleep(0.15)
            assert entry.last_phrase == "reading notes.md"

        asyncio.run(go())


class TestPublisherReset:
    def test_a_new_api_drops_the_previous_adapter_s_turns(self):
        pub = status.StatusPublisher()
        pub.set_api(_FakeAPI())
        pub.begin_turn("$m", status.TurnScope(room_id="!old"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        pub.begin_turn("$n", status.TurnScope(room_id="!old"))
        pub.set_api(_FakeAPI())
        assert not pub._pending and not pub._bound
        assert not pub._trigger_session and not pub._session_room

    def test_a_new_session_cannot_claim_a_reset_turn(self):
        pub = status.StatusPublisher()
        pub.set_api(_FakeAPI())
        pub.begin_turn("$m", status.TurnScope(room_id="!old"))
        pub.set_api(_FakeAPI())
        pub.on_tool_call("web_search", {"query": "x"}, "sess-new")
        assert "sess-new" not in pub._bound

    def test_reset_halts_the_refresh_loop(self):
        async def go():
            pub = status.StatusPublisher()
            pub.set_api(_FakeAPI())
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            entry = pub._pending["$m"]
            pub.reset()
            assert entry.ended
            # Let the cancellation actually take effect.
            await asyncio.sleep(0)
            assert entry.refresh_task.cancelled()

        asyncio.run(go())


class TestCompletionRaces:
    def test_completion_after_a_concurrent_claim_still_clears(self):
        """The engine thread can claim between the completion's two dict
        reads. The turn must still be cleared, not left refreshing."""

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub._pending["$m"].created -= status.COMPLETION_GRACE_SECONDS + 1
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            await pub.end_turn("$m")
            assert not pub._bound and not pub._pending
            clears = [c for c in api.calls if "status_text" not in c]
            assert clears

        asyncio.run(go())

    def test_completion_of_an_already_claimed_turn_does_not_raise(self):
        async def go():
            pub = status.StatusPublisher()
            pub.set_api(_FakeAPI())
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            entry = pub._pending["$m"]
            entry.created -= status.COMPLETION_GRACE_SECONDS + 1
            # Simulate the interleaving directly: the trigger's binding is
            # gone (claim not yet recorded) and the pending entry has been
            # taken, exactly the window the old pop() raised KeyError in.
            del pub._pending["$m"]
            await pub.end_turn("$m")

        asyncio.run(go())


class TestGraceWindowTiming:
    def test_finalize_waits_only_the_window_s_remainder(self, monkeypatch):
        monkeypatch.setattr(status, "COMPLETION_GRACE_SECONDS", 0.4)

        async def go():
            pub = status.StatusPublisher()
            pub.set_api(_FakeAPI())
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            # Completion arrives most of the way through the window.
            pub._pending["$m"].created -= 0.3
            await pub.end_turn("$m")
            # The remaining ~0.1s, not another full window.
            await asyncio.sleep(0.25)
            assert "$m" not in pub._pending

        asyncio.run(go())


class TestNoPublishAfterTermination:
    """A tool call racing a completion must not restore a status the
    completion just cleared: the clear is the last word on a turn."""

    def test_tool_call_after_completion_publishes_nothing(self):
        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            entry = pub._bound["sess1"]
            await pub.end_turn("$m")
            before = len(api.calls)
            # The engine thread had this tool call in flight.
            pub.on_tool_call("terminal", {"command": "cargo build"}, "sess1")
            assert entry.ended
            assert len(api.calls) == before

        asyncio.run(go())

    def test_tool_call_after_reset_publishes_nothing(self):
        api = _FakeAPI()
        pub = status.StatusPublisher()
        pub.set_api(api)
        pub.begin_turn("$m", status.TurnScope(room_id="!r"))
        pub.on_tool_call("web_search", {"query": "x"}, "sess1")
        pub.reset()
        before = len(api.calls)
        pub.on_tool_call("terminal", {"command": "cargo build"}, "sess1")
        assert len(api.calls) == before

    def test_queued_phrase_is_dropped_when_the_turn_ends(self, monkeypatch):
        monkeypatch.setattr(status, "MIN_INTERVAL_SECONDS", 0.05)

        async def go():
            api = _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(api)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            pub.on_tool_call("web_search", {"query": "x"}, "sess1")
            pub.on_tool_call("terminal", {"command": "cargo build"}, "sess1")
            await pub.end_turn("$m")
            texted_before = len([c for c in api.calls if c.get("status_text")])
            await asyncio.sleep(0.15)
            texted_after = len([c for c in api.calls if c.get("status_text")])
            assert texted_after == texted_before

        asyncio.run(go())


class TestUrlAuthorityStopsAtQuery:
    def test_query_string_never_reaches_the_status(self):
        # A URL with no path: the authority still ends at "?", or the whole
        # query rides into a channel-visible status line.
        line = phrase_for("browser_navigate", {"url": "https://example.com?token=sec"})
        assert line == "reading example.com"

    def test_fragment_is_dropped_too(self):
        line = phrase_for("browser_navigate", {"url": "https://example.com#tok"})
        assert line == "reading example.com"


class TestTurnKeepsItsOwnApi:
    def test_a_completion_clears_through_the_api_it_started_on(self):
        """A turn awaiting its clear must not deliver it through whatever
        API a rebuilt adapter installed in the meantime."""

        async def go():
            first, second = _FakeAPI(), _FakeAPI()
            pub = status.StatusPublisher()
            pub.set_api(first)
            pub.begin_turn("$m", status.TurnScope(room_id="!r"))
            entry = pub._pending["$m"]
            entry.created -= status.COMPLETION_GRACE_SECONDS + 1
            # The adapter is replaced while the completion is in flight.
            pub._api = second
            await pub._clear(entry)
            assert any("status_text" not in c for c in first.calls)
            assert second.calls == []

        asyncio.run(go())
