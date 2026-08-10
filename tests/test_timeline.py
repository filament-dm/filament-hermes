"""Compact timeline rendering (timeline.py).

The contract under test: compact rendering drops METADATA, never content —
body text, event id, sender + classification, time, media/reaction/quote
annotations all survive — and any parse surprise falls back to the JSON
form rather than raising or hiding data.

``timeline.py`` is pure-stdlib, loaded standalone like ``slash.py``.
"""

import importlib.util
import json
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


timeline = _load("timeline")


def _msg(**kwargs):
    base = {
        "event_id": "$e1",
        "sender": "@alice:fil",
        "body": "hello there",
        "timestamp": 1754575000000,  # 2026-08-07 UTC-ish; exact day pinned below
        "type": "m.room.message",
        "msgtype": "m.text",
        "is_from_self": False,
        "is_from_principal": False,
        "is_from_agent": False,
        "is_system": False,
        "reactions": [],
    }
    base.update(kwargs)
    return base


# ── render_message_line ──────────────────────────────────────────────


def test_line_keeps_body_sender_time_and_event_id():
    line = timeline.render_message_line(_msg())
    assert "@alice:fil" in line
    assert "hello there" in line
    assert "⟨id $e1⟩" in line
    # A compact timestamp, not epoch millis.
    assert "1754575000000" not in line
    assert ":" in line.split("@alice:fil")[0]  # HH:MM present


def test_line_sender_classification_tags():
    assert "⟨you⟩" in timeline.render_message_line(_msg(is_from_self=True))
    assert "⟨your principal⟩" in timeline.render_message_line(
        _msg(is_from_principal=True)
    )
    assert "⟨agent⟩" in timeline.render_message_line(_msg(is_from_agent=True))
    assert "⟨system⟩" in timeline.render_message_line(_msg(is_system=True))
    assert "(" not in timeline.render_message_line(_msg(event_id="")).split(
        ":", 1
    )[0].replace("(unknown", "")


def test_line_folds_newlines_and_strips_control_chars():
    line = timeline.render_message_line(
        _msg(body="line one\nline two\x07\x00")
    )
    assert "\n" not in line
    assert "line one ⏎ line two" in line
    assert "\x07" not in line and "\x00" not in line


def test_line_neutralizes_unicode_line_separators():
    # NEL and the Unicode line/paragraph separators break lines in many
    # renderers — left intact, content could masquerade as an unlabeled
    # timeline line. They fold to the visible ⏎ like \n; other C1
    # controls are stripped like their C0 siblings.
    line = timeline.render_message_line(
        _msg(body="a\x85b\u2028c\u2029d\x9be")
    )
    assert "a ⏎ b ⏎ c ⏎ d" in line
    assert "\x9b" not in line and "de" in line


def test_line_state_event_renders_type_stub_and_keeps_body():
    # A body on a non-message event still surfaces — compact never hides
    # what the JSON form would have shown.
    line = timeline.render_message_line(
        _msg(type="m.room.member", body="joined the room")
    )
    assert "⟨event: m.room.member⟩" in line
    assert "joined the room" in line


def test_line_state_event_keeps_annotations():
    # Annotations survive on non-message events too: reactions or media
    # riding on a state event must not vanish from compact output.
    line = timeline.render_message_line(
        _msg(
            type="m.room.member",
            body="joined the room",
            reactions=[{"key": "👋"}],
            media=[{"filename": "wave.png", "mxc_url": "mxc://s/abc"}],
        )
    )
    assert "⟨reactions: 👋⟩" in line
    assert "wave.png" in line and "mxc://s/abc" in line


def test_line_annotations_media_reactions_quote():
    line = timeline.render_message_line(
        _msg(
            media=[{"filename": "chart.png"}],
            reactions=[{"key": "👍"}, {"key": "👍"}, {"key": "🔥"}],
            quote={"event_id": "$quoted"},
        )
    )
    assert "⟨media: chart.png⟩" in line
    assert "⟨reactions: 👍×2; 🔥⟩" in line
    assert "⟨quoting $quoted⟩" in line


def test_reaction_senders_survive_compact():
    # Two people, same emoji: the agent must still see WHO reacted.
    line = timeline.render_message_line(
        _msg(
            reactions=[
                {"key": "👍", "sender": "@a:s"},
                {"key": "👍", "sender": "@b:s"},
            ]
        )
    )
    assert "sender=@a:s" in line and "sender=@b:s" in line


def test_misshapen_annotation_field_falls_back_to_json():
    # media/reactions/quote/rechat are in _RENDERED_KEYS, so a misshapen
    # value gets no unknown-field stub — raising into the JSON fallback
    # is the only exit that doesn't hide the data.
    payload = {"messages": [_msg(media="not-a-list")]}
    out = timeline.render_tool_result(
        "get_recent_messages", payload, compact=True
    )
    assert json.loads(out) == payload
    for bad in (
        _msg(reactions="x"),
        _msg(media=["not-an-object"]),
        _msg(quote="q"),
        _msg(rechat=[1]),
    ):
        try:
            timeline.render_message_line(bad)
        except ValueError:
            continue
        raise AssertionError(f"no ValueError for {bad!r}")


def test_line_missing_fields_are_tolerated():
    line = timeline.render_message_line({"body": "just a body"})
    assert "just a body" in line
    assert "(unknown sender)" in line


# ── render_recent_messages / render_thread ───────────────────────────


def test_recent_messages_header_order_and_observed_note():
    payload = {
        "messages": [_msg(event_id="$old", body="first"),
                     _msg(event_id="$new", body="second")],
        "next_cursor": "tok123",
    }
    text = timeline.render_recent_messages(payload)
    assert text.startswith(timeline.OBSERVED_NOTE)
    assert "2 message(s), oldest first:" in text
    assert text.index("first") < text.index("second")
    assert "next_cursor (older history): tok123" in text


def test_recent_messages_raises_on_unexpected_shape():
    for bad in ({}, {"messages": "nope"}, {"messages": [42]}):
        try:
            timeline.render_recent_messages(bad)
        except ValueError:
            continue
        raise AssertionError(f"no ValueError for {bad!r}")


def test_thread_renders_root_anchor_then_replies():
    payload = {
        "root": _msg(event_id="$root", body="the question"),
        "replies": [_msg(event_id="$r1", body="the answer")],
    }
    text = timeline.render_thread(payload)
    assert text.startswith(timeline.OBSERVED_NOTE)
    assert "Thread root:" in text
    assert text.index("the question") < text.index("the answer")
    assert "1 reply:" in text


def test_thread_header_carries_channel_provenance():
    # Same rule as render_recent_messages: no rendering is ever
    # location-ambiguous when the caller knows the channel.
    payload = {"root": _msg(), "replies": []}
    text = timeline.render("get_thread", payload, channel="!room:s")
    assert "channel !room:s — Thread root:" in text


# ── render_tool_result (the proxy's decision) ────────────────────────


def test_tool_result_compact_off_is_json():
    payload = {"messages": [_msg()]}
    out = timeline.render_tool_result(
        "get_recent_messages", payload, compact=False
    )
    assert json.loads(out) == payload  # byte-faithful JSON path


def test_tool_result_compact_on_renders_lines():
    out = timeline.render_tool_result(
        "get_recent_messages", {"messages": [_msg()]}, compact=True
    )
    assert timeline.OBSERVED_NOTE in out
    assert "hello there" in out


def test_tool_result_unrenderable_tool_stays_json_even_when_compact():
    out = timeline.render_tool_result(
        "get_user_profile", {"user": "x"}, compact=True
    )
    assert json.loads(out) == {"user": "x"}


def test_tool_result_falls_back_to_json_on_bad_shape():
    # A surprise shape must degrade to JSON, never raise or drop data.
    payload = {"messages": "not-a-list"}
    out = timeline.render_tool_result(
        "get_recent_messages", payload, compact=True
    )
    assert json.loads(out) == payload


def test_compact_is_actually_compact():
    payload = {"messages": [_msg(event_id=f"$e{i}") for i in range(15)]}
    fat = timeline.render_tool_result(
        "get_recent_messages", payload, compact=False
    )
    slim = timeline.render_tool_result(
        "get_recent_messages", payload, compact=True
    )
    assert len(slim) < len(fat) / 4  # the point of the whole exercise


def test_msgtype_rendered_when_it_changes_meaning():
    assert "⟨m.emote⟩" in timeline.render_message_line(
        _msg(msgtype="m.emote")
    )
    assert "⟨m.notice⟩" in timeline.render_message_line(
        _msg(msgtype="m.notice")
    )
    # Plain text carries no marker.
    assert "⟨m.text⟩" not in timeline.render_message_line(_msg())


def test_media_annotation_keeps_download_details():
    line = timeline.render_message_line(
        _msg(
            media=[
                {
                    "filename": "chart.png",
                    "mxc_url": "mxc://fil/abc",
                    "mimetype": "image/png",
                }
            ]
        )
    )
    assert "chart.png" in line
    assert "mxc_url=mxc://fil/abc" in line
    assert "mimetype=image/png" in line


def test_quote_annotation_keeps_all_fields():
    line = timeline.render_message_line(
        _msg(quote={"event_id": "$q", "sender": "@a:s", "body": "the ask"})
    )
    assert "⟨quoting $q" in line
    assert "sender=@a:s" in line
    assert "body=the ask" in line


def test_unknown_fields_are_not_truncated():
    long = "x" * 400
    line = timeline.render_message_line(_msg(huge_field=long))
    assert long in line


def test_clean_preserves_falsy_values_and_lone_cr():
    line = timeline.render_message_line(_msg(edited=False, score=0))
    assert "⟨edited: False⟩" in line
    assert "⟨score: 0⟩" in line
    line = timeline.render_message_line(_msg(body="first\rsecond"))
    assert "first ⏎ second" in line

def test_renderers_stub_unknown_top_level_payload_fields():
    # "Never hide data" covers the envelope too: a server-attached field
    # neither renderer knows (warning, truncation marker) must surface.
    rendered = timeline.render_recent_messages(
        {"messages": [_msg()], "server_warning": "history truncated"}
    )
    assert "⟨server_warning: history truncated⟩" in rendered
    rendered = timeline.render_thread(
        {"root": _msg(), "replies": [], "truncated": True}
    )
    assert "⟨truncated: True⟩" in rendered
