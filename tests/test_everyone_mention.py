"""@everyone/@here must never count as a mention of the agent.

The wake predicate is a pure function (``reactive.is_agent_mention``) so this
invariant is pinned by tests rather than living in an inline expression a
merge conflict can silently rewrite — which is how it was lost once. The
source-level test is the backstop: it fails if anyone reintroduces the old
inline ``or msg.is_everyone_mention`` in the adapter instead of going through
the predicate.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "reactive",
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "reactive.py",
)
reactive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reactive)

_ADAPTER_SRC = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "adapter.py"
).read_text()


def test_everyone_mention_alone_is_not_a_mention():
    assert reactive.is_agent_mention(False, True, False) is False


def test_direct_mention_wakes():
    assert reactive.is_agent_mention(True, False, False) is True


def test_body_text_match_fallback_wakes():
    assert reactive.is_agent_mention(False, False, True) is True


def test_everyone_flag_changes_nothing_for_any_other_signal():
    for is_mention in (False, True):
        for body_match in (False, True):
            assert reactive.is_agent_mention(
                is_mention, True, body_match
            ) == reactive.is_agent_mention(is_mention, False, body_match)


def test_adapter_routes_the_wake_decision_through_the_predicate():
    # The adapter must hand the everyone flag to is_agent_mention, not OR it
    # into an inline expression.
    assert "or msg.is_everyone_mention" not in _ADAPTER_SRC
    assert "mentioned = is_agent_mention(" in _ADAPTER_SRC
