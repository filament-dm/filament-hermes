"""The no-reply sentinel: which replies post nothing, and which post in full.

``reactive.is_no_reply`` is a pure function, so it is pinned here directly, with
source-level backstops for the parts of ``send`` that use it.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "reactive",
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "reactive.py",
)
reactive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reactive)

SENTINEL = reactive.NO_REPLY_SENTINEL

_ADAPTER_SRC = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "adapter.py"
).read_text()


def test_empty_bodies_are_no_reply():
    assert reactive.is_no_reply(None) is True
    assert reactive.is_no_reply("") is True
    assert reactive.is_no_reply("   ") is True
    assert reactive.is_no_reply("\n\n\t") is True


def test_bare_sentinel_is_no_reply():
    assert reactive.is_no_reply(SENTINEL) is True
    assert reactive.is_no_reply(f"  {SENTINEL}  ") is True
    assert reactive.is_no_reply("[[no_reply]]") is True


def test_sentinel_wrapped_the_way_models_wrap_it():
    """Fencing, quotes and trailing punctuation around the sentinel still count."""
    for body in (
        f"```{SENTINEL}```",
        f"`{SENTINEL}`",
        f'"{SENTINEL}"',
        f"{SENTINEL}.",
        f"**{SENTINEL}**",
    ):
        assert reactive.is_no_reply(body) is True, body


def test_interleaved_wrappers_are_stripped():
    """Wrappers nest in either order, so they cannot be stripped one kind at a
    time: removing backticks first leaves ``**`X`**`` with backticks at the ends
    that the earlier pass has already gone past."""
    for body in (
        f"**`{SENTINEL}`**",
        f"`**{SENTINEL}**`",
        f"*`{SENTINEL}`*",
        f'"`{SENTINEL}`"',
        f"_`{SENTINEL}`_",
        f"**`{SENTINEL}`**.",
        f"  ** ` {SENTINEL} ` **  ",
    ):
        assert reactive.is_no_reply(body) is True, body


def test_real_content_is_never_suppressed():
    assert reactive.is_no_reply("Here is your poem.") is False
    assert reactive.is_no_reply("a2a: response\ntask: poem-1234") is False


def test_sentinel_alongside_prose_is_not_suppressed():
    """Only a bare sentinel suppresses. Anything else posts in full.

    A model that answers and then appends the sentinel has produced an answer,
    and suppressing the message would discard it."""
    assert reactive.is_no_reply(f"No action needed. {SENTINEL}") is False
    assert reactive.is_no_reply(f"Not a poem request.\n\n{SENTINEL}") is False
    assert reactive.is_no_reply(f"{SENTINEL}\n\nThis was ambient chatter.") is False
    assert reactive.is_no_reply(f"<poem>\n\n{SENTINEL}") is False


def _send_src() -> str:
    return _ADAPTER_SRC.split("async def send(", 1)[1].split(
        "parent_context = current_context()"
    )[0]


def test_send_consults_the_predicate():
    """Suppression belongs in ``send``, which every reply passes through."""
    assert "is_no_reply(content)" in _send_src()


def test_no_reply_is_checked_before_the_connection_guard():
    """Declining to reply needs no API call.

    Behind the guard, a turn that intentionally said nothing while the adapter
    was disconnected would return failure, and the framework would retry it."""
    src = _send_src()
    assert src.index("is_no_reply(content)") < src.index("if not self._filament_api:")


def test_send_warns_when_a_posted_message_still_holds_the_sentinel():
    """A sentinel that reaches the channel is logged, and never stripped."""
    send = _ADAPTER_SRC.split("async def send(", 1)[1]
    body = send.split("parent_context = current_context()")[0]
    assert "sentinel_leak" in body
    assert "NO_REPLY_SENTINEL.lower() in content.lower()" in body
    # ...and it must warn, never mutate: no stripping of the token on the way out.
    assert ".replace(NO_REPLY_SENTINEL" not in _ADAPTER_SRC
    assert "re.sub" not in body


def test_the_sentinel_is_defined_once():
    """One definition, so the token cannot drift between code and instructions."""
    instructions = (
        Path(__file__).resolve().parent.parent
        / "hermes_filament_fcm"
        / "default_instructions.md"
    ).read_text()
    assert reactive.NO_REPLY_SENTINEL in instructions
    assert _ADAPTER_SRC.count('"[[NO_REPLY]]"') == 0  # adapter imports, never literals
