"""Per-turn authority, held as one value behind one ContextVar.

One value rather than four, so a dispatch site sets a turn's whole authority or
none of it.

Each field has one reader elsewhere in the plugin:

zone
    The control-plane tools (set_instructions, set_wake_policy,
    set_capabilities, set_feature, set_agent_config, and their getters) refuse
    unless it is Zone.CONTROL, so a shared-channel participant can never
    reconfigure the agent.
capabilities
    The pre_tool_call capability gate denies any tool outside the set. This is
    the hard half of the trust boundary that the envelope framing states
    softly.
cursor_channel
    The get_recent_messages tool proxy records a read cursor only for this
    room.
reply_anchor
    adapter.send threads an unaddressed reply under it.

The two defaults are asymmetric on purpose. zone defaults to Zone.DATA, the
low-privilege value, so a turn that never set a context cannot edit policy.
capabilities defaults to None, meaning ungated, so a turn from outside this
plugin is not restricted by a Filament policy unrelated to it. Data-plane
fail-closure is therefore the dispatch site's job, and only applies while the
advanced_tool_controls flag is on; with it off, the default, a data turn is
ungated too.

ContextVars are task-local, so concurrent turns do not race and no reset is
needed: a turn's task ends and its context goes with it.

Stdlib-only, loads standalone.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final


class Zone(str, Enum):
    """The trust plane a turn runs in.

    Compare with ``is``, as in ``ctx.zone is Zone.CONTROL``. Identity is exact,
    so a raw string in the field reads as not-control and the control-plane
    guards fail closed.

    Attributes:
        CONTROL: The principal's private backchannel, where a message is a
            command. Required by the control-plane set_* tools.
        DATA: A shared channel, where an event is a wake-up signal and its
            content is data rather than instructions.
    """

    CONTROL = "control"
    DATA = "data"

    def __str__(self) -> str:
        """Returns the bare value, such as "control".

        A str-mixin enum's inherited __str__ renders "Zone.CONTROL" instead,
        and which interpolation forms do that varies by Python version. Log
        lines interpolate the zone, so pin it here.
        """
        return self.value


@dataclass(frozen=True)
class TurnContext:
    """The authority granted to one turn.

    Frozen on purpose: a turn's authority is decided once, at dispatch, and
    nothing downstream may widen it. Install one with activate.

    Attributes:
        zone: Which trust plane the turn runs in.
        capabilities: The tool names this turn may call, or None for an ungated
            turn.
        cursor_channel: The room id whose read cursor this turn may record, or
            None to record none.
        reply_anchor: The (room_id, event_id) pair an unaddressed reply threads
            under, or None to post at the top level.
    """

    zone: Zone = Zone.DATA
    capabilities: frozenset[str] | None = None
    cursor_channel: str | None = None
    reply_anchor: tuple[str, str] | None = None

    def with_capabilities(self, capabilities: frozenset[str] | None) -> TurnContext:
        """Returns a copy carrying a different tool grant.

        Args:
            capabilities: The replacement grant, or None for an ungated turn.

        Returns:
            A new TurnContext with every other field unchanged. The receiver is
            never modified, so this cannot widen an active turn's authority.
        """
        return replace(self, capabilities=capabilities)


# What a task no Filament turn claimed sees, such as a plain CLI session in the
# same Hermes process.
UNCLAIMED: Final = TurnContext()

# Every control turn uses this value: full capability, no read-cursor
# authority, and no reply anchor.
CONTROL: Final = TurnContext(
    zone=Zone.CONTROL, capabilities=None, cursor_channel=None, reply_anchor=None
)

_current: contextvars.ContextVar[TurnContext] = contextvars.ContextVar(
    "filament_turn_context", default=UNCLAIMED
)


def data_turn(
    *,
    capabilities: frozenset[str] | None,
    cursor_channel: str | None,
    reply_anchor: tuple[str, str] | None,
) -> TurnContext:
    """Builds a data-plane turn's context.

    No argument has a default, because for each field the safe value depends on
    the channel. A caller that omits one gets a TypeError rather than a quiet
    wrong answer.

    Args:
        capabilities: The tool names this turn may call, or None for an ungated
            turn.
        cursor_channel: The room id whose read cursor this turn may record, or
            None to record none.
        reply_anchor: The (room_id, event_id) pair to thread a reply under, or
            None to post at the top level.

    Returns:
        A TurnContext in Zone.DATA.
    """
    return TurnContext(
        zone=Zone.DATA,
        capabilities=capabilities,
        cursor_channel=cursor_channel,
        reply_anchor=reply_anchor,
    )


def activate(ctx: TurnContext) -> None:
    """Installs a context as the current turn's authority.

    Call this at the dispatch site, as late as possible and with no await
    between it and the handoff to Hermes.

    Args:
        ctx: The context to install.
    """
    _current.set(ctx)


def current() -> TurnContext:
    """Returns the active turn's context, or UNCLAIMED outside a Filament turn."""
    return _current.get()
