"""The ``poll_work`` transport: inbound work by long poll instead of FCM.

Opt-in with ``FILAMENT_TRANSPORT=poll``. The plugin calls the ``poll_work``
MCP tool in a loop forever: the call blocks server-side until there is work or
``wait_seconds`` pass, so the next call goes out the moment the previous one
returns. No Firebase project, push registration or MCS connection is
involved, which is what makes local and staging servers work without the
FILAMENT_FIREBASE_* settings.

The server's contract (synapse ``agents_mcp/tools_poll.py``):

- ``work`` items are grouped by the place a reply goes and have a ``kind``:
  ``message`` (``messages``: the same rows ``get_recent_messages`` returns),
  ``reaction`` (``reactions``), ``invite`` and ``vouch`` (a loop to accept).
- ``cursor`` acknowledges: passing a delivery's cursor on the next poll
  finishes that delivery's work, and omitting it does the same from the
  server's saved position. Work a poll hands over stays unfinished until a
  reply, an ``ack``, or a poll passing its cursor (or none) finishes it, so a
  poll passing the cursor from *before* a delivery gets that delivery's
  unfinished work back. That is how work a draining gateway could not run
  reaches the next one (see ``PollLoop``).
- ``reply_with`` names the tool and target a reply must use, or is null where
  the agent may not answer. The adapter keeps routing replies the way it does
  for pushes; the server enforces the same rules on the tools it calls.

Each item is turned into the dataclass the FCM path already produces
(``PushMessage``, ``ReactionMessage``, ``InviteMessage``, ``VouchMessage``),
so the wake gate, trust zones, slash commands and the hello run unchanged. A
poll row lacks some of what a push carried — the room's name, the sender's
display name, whether the room is a DM — unless the server puts them on
the item (filament-dm/synapse#1156; ``room_info``). For an older server the
adapter fills them from ``get_channel_details`` (``RoomInfo`` here), cached
per room.

Stdlib-only; the tool call, the dispatcher and the sleep are injected, so the
whole loop is unit-tested standalone in ``tests/test_poll_transport.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gateway.filament_fcm")

TRANSPORT_ENV = "FILAMENT_TRANSPORT"
TRANSPORT_FCM = "fcm"
TRANSPORT_POLL = "poll"

# The server's default and the longest a reverse proxy reliably lets a request
# sit; it allows up to 60. The client's read timeout (70 s) must exceed it.
DEFAULT_WAIT_SECONDS = 30
DEFAULT_MAX_ITEMS = 10

_BACKOFF_FIRST_S = 1.0
_BACKOFF_MAX_S = 60.0
# Consecutive failures before one is logged as a warning (then every tenth).
_WARN_AFTER_FAILURES = 3

# How often a loop held by a draining gateway looks again. The gateway's
# disconnect() cancels it; this only bounds how long a drain that ends
# without a disconnect leaves it idle.
_HOLD_S = 1.0

_EVERYONE = re.compile(r"(?<![\w@])@(room|everyone|here)\b", re.I)


def transport_from_env(environ: Any) -> str:
    """``poll`` when FILAMENT_TRANSPORT says so, else ``fcm`` (the default)."""
    value = str(environ.get(TRANSPORT_ENV) or "").strip().lower()
    return TRANSPORT_POLL if value == TRANSPORT_POLL else TRANSPORT_FCM


def everyone_mentioned(body: str) -> bool:
    """A body-text guess at @everyone: poll rows don't carry ``m.mentions``.

    Only informational — an @everyone is never a mention of the agent
    (reactive.is_agent_mention)."""
    return bool(_EVERYONE.search(body or ""))


@dataclass
class PollItem:
    """One ``work`` item. Fields a kind doesn't use stay empty."""

    kind: str
    raw: dict
    channel_id: str = ""
    thread_id: str | None = None
    is_backchannel: bool = False
    # What a push said about the room and senders, from servers that send
    # it (filament-dm/synapse#1156); None from older ones. See room_info.
    is_direct: bool | None = None
    channel_name: str | None = None
    loop_name: str | None = None
    sender_names: dict[str, str] | None = None
    messages: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    loop_id: str = ""
    name: str | None = None
    sender: str = ""  # who invited or vouched


def room_info(item: PollItem) -> RoomInfo | None:
    """The item's own account of its room, when the server gave one.

    A server that sends ``is_direct`` and ``sender_names`` sends the room's
    name and loop with them, so the adapter needs no get_channel_details
    call; an older server's item returns None and the adapter looks."""
    if item.is_direct is None or item.sender_names is None:
        return None
    return RoomInfo(
        name=item.channel_name or "",
        loop_name=item.loop_name,
        is_direct=item.is_direct,
        members=dict(item.sender_names),
    )


@dataclass
class PollResponse:
    work: list[PollItem]
    cursor: str | None
    next_poll_ms: int = 0


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_item(raw: Any) -> PollItem | None:
    """A work item, or None for one this plugin can't act on."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    # A server from before `kind` only handed over messages.
    if kind is None and isinstance(raw.get("messages"), list):
        kind = "message"
    if kind in ("message", "reaction"):
        channel_id = _str(raw.get("channel_id"))
        if not channel_id:
            return None
        rows_key = "messages" if kind == "message" else "reactions"
        rows = [
            r
            for r in raw.get(rows_key) or []
            if isinstance(r, dict) and _str(r.get("event_id"))
        ]
        if not rows:
            return None
        is_direct = raw.get("is_direct")
        names = raw.get("sender_names")
        return PollItem(
            kind=kind,
            raw=raw,
            channel_id=channel_id,
            thread_id=_str(raw.get("thread_id")) or None,
            is_backchannel=raw.get("is_backchannel") is True,
            is_direct=is_direct if isinstance(is_direct, bool) else None,
            channel_name=_str(raw.get("channel_name")) or None,
            loop_name=_str(raw.get("loop_name")) or None,
            sender_names=(
                {k: _str(v) for k, v in names.items() if isinstance(k, str)}
                if isinstance(names, dict)
                else None
            ),
            messages=rows if kind == "message" else [],
            reactions=rows if kind == "reaction" else [],
        )
    if kind in ("invite", "vouch"):
        loop_id = _str(raw.get("loop_id"))
        if not loop_id:
            return None
        return PollItem(
            kind=kind,
            raw=raw,
            loop_id=loop_id,
            name=_str(raw.get("name")) or None,
            sender=_str(raw.get("invited_by") or raw.get("vouched_by")),
        )
    return None


def parse_response(data: Any) -> PollResponse | None:
    """A poll_work result, or None when it isn't one."""
    if not isinstance(data, dict) or not isinstance(data.get("work"), list):
        return None
    work = [item for item in map(parse_item, data["work"]) if item is not None]
    skipped = len(data["work"]) - len(work)
    if skipped:
        logger.info("filament-fcm: poll_work: skipped %d unusable item(s)", skipped)
    cursor = data.get("cursor")
    next_poll_ms = data.get("next_poll_ms")
    return PollResponse(
        work=work,
        cursor=cursor if isinstance(cursor, str) and cursor else None,
        next_poll_ms=next_poll_ms if isinstance(next_poll_ms, int) else 0,
    )


@dataclass
class RoomInfo:
    """What a push said about its room and sender, from get_channel_details."""

    name: str = ""
    loop_name: str | None = None
    is_direct: bool = False
    members: dict[str, str] = field(default_factory=dict)  # mxid → display name


def room_info_from_details(data: Any) -> RoomInfo:
    """``get_channel_details`` → ``RoomInfo``; empty for anything unreadable.

    ``is_direct`` is a guess until poll_work says so: the server's own rule
    (the create event's ``dm_users``) is exposed by no tool. A DM has no loop,
    no name and two members; a group chat or channel fails at least one of
    those. A wrong guess only matters for the principal's own messages in an
    unnamed two-person room, which then run as control turns without an
    @-mention — what a DM gets."""
    if not isinstance(data, dict):
        return RoomInfo()
    members = {}
    for member in data.get("members") or []:
        if isinstance(member, dict) and _str(member.get("mxid")):
            members[member["mxid"]] = _str(member.get("display_name"))
    member_count = data.get("member_count")
    if not isinstance(member_count, int):
        member_count = len(members)
    name = _str(data.get("name"))
    loop_id = data.get("loop_id")
    return RoomInfo(
        name=name,
        loop_name=_str(data.get("loop_name")) or None,
        is_direct=not loop_id and not name and 0 < member_count <= 2,
        members=members,
    )


# One poll_work call: (parsed result, error). The error is a JSON-RPC
# rejection or a transport failure in FilamentAPI.result_error's words.
PollCall = Callable[[dict], Awaitable[tuple[Any, "str | None"]]]


class PollFatal(Exception):
    """Polling can't go on as things stand.

    ``retryable``: whether a reconnect might fix it (a token that is refused
    now may be fine once the owner signs in again; a server without poll_work
    stays without it)."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def classify_error(error: str) -> str:
    """``auth``, ``unsupported`` or ``retry`` for a poll_work error."""
    lowered = error.lower()
    if (
        error in ("HTTP 401", "HTTP 403")
        or "unauthorized" in lowered
        or "authentication required" in lowered
    ):
        return "auth"
    if "unknown tool" in lowered or "method not found" in lowered:
        return "unsupported"
    if "poll_work" in lowered and "not found" in lowered:
        return "unsupported"
    return "retry"


class PollLoop:
    """Call poll_work forever, handing each item to ``dispatch``.

    ``dispatch`` must not block on the turn it starts (the adapter schedules
    it): the next poll goes out as soon as the items are handed over. Errors
    back off exponentially (1 s doubling to 60 s) and recover on the next
    good answer; only an auth failure or a server without poll_work ends the
    loop, by raising ``PollFatal``.

    ``admitting`` says whether the gateway still starts turns. A gateway
    stopping or restarting drains first: it refuses every new turn
    (``runtime_draining``) but keeps the adapter connected, so a poll
    answered then hands over work nobody runs, and the next poll's cursor
    would finish it on the server (live run h30-r2-reconnect-3: the owner's
    DM was lost). While the gateway is not admitting, the loop sends no poll.
    A poll that was already held when the drain began and comes back with
    work hands none of it over: the loop keeps the cursor it sent and gives
    it to ``keep_cursor``, which saves it for the next gateway. That gateway
    starts from it (``cursor``), and the server hands the same work back.
    ``keep_cursor(None)`` drops a saved cursor once a poll has used it.

    Not covered: a turn dispatched just before the drain began and refused
    by it. The next poll has gone out by then and finished its work."""

    def __init__(
        self,
        call: PollCall,
        dispatch: Callable[[PollItem], Awaitable[None]],
        *,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
        max_items: int = DEFAULT_MAX_ITEMS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cursor: str | None = None,
        admitting: Callable[[], bool] = lambda: True,
        keep_cursor: Callable[[str | None], None] | None = None,
    ) -> None:
        self._call = call
        self._dispatch = dispatch
        self._wait_seconds = wait_seconds
        self._max_items = max_items
        self._sleep = sleep
        self._admitting = admitting
        self._keep_cursor = keep_cursor
        self.cursor: str | None = cursor
        # The cursor saved for a next gateway, until a poll has used it.
        self._saved_cursor = cursor
        self.holding = False
        self.failures = 0
        self.polls = 0

    async def poll_once(self) -> None:
        """One poll and its dispatch. Raises PollFatal; backs off on errors."""
        if not self._admitting():
            if not self.holding:
                logger.info(
                    "filament-fcm: the gateway is draining; poll_work paused "
                    "so the next gateway gets the work"
                )
            self.holding = True
            await self._sleep(_HOLD_S)
            return
        self.holding = False
        args: dict[str, Any] = {
            "wait_seconds": self._wait_seconds,
            "max_items": self._max_items,
        }
        if self.cursor:
            args["cursor"] = self.cursor
        elif self.polls == 0:
            # Without a cursor the loop has none to leave the next gateway if
            # a drain catches this poll, so the first poll doesn't wait: its
            # answer brings one at once.
            args["wait_seconds"] = 0
        try:
            data, error = await self._call(args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            error, data = f"{type(e).__name__}: {str(e) or 'connection closed'}", None
        self.polls += 1
        response = None if error else parse_response(data)
        if response is None:
            reason = error or "an unreadable poll_work result"
            kind = classify_error(error) if error else "retry"
            if kind == "auth":
                # Retryable: the reconnect's Stage 1 tells a dead sign-in (the
                # agent deleted or signed out in Filament) from a blip.
                raise PollFatal(
                    f"poll_work was refused ({reason}); sign in again", retryable=True
                )
            if kind == "unsupported":
                raise PollFatal(
                    f"this Filament server has no poll_work ({reason}); unset "
                    f"{TRANSPORT_ENV} to use push delivery",
                    retryable=False,
                )
            await self._back_off(reason)
            return
        if self.failures >= _WARN_AFTER_FAILURES:
            logger.info(
                "filament-fcm: poll_work recovered after %d failure(s)", self.failures
            )
        self.failures = 0
        if response.work and not self._admitting():
            self._leave(len(response.work))
            return
        self.cursor = response.cursor or self.cursor
        if self._saved_cursor is not None:
            self._saved_cursor = None
            self._keep(None)
        for item in response.work:
            try:
                await self._dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "filament-fcm: poll_work: could not dispatch a %s item", item.kind
                )
        if not response.work:
            # The server's pacing hint for an empty answer, capped, and never
            # nothing: a server that stops honoring wait_seconds must not turn
            # this into a busy loop.
            await self._sleep(max(min(response.next_poll_ms, 5_000), 200) / 1000)

    def _leave(self, count: int) -> None:
        """Hand over none of a poll's work: the gateway began draining while
        the poll was held. The cursor stays the one the poll sent, so no
        later poll finishes this work, and is saved for the next gateway."""
        self.holding = True
        # Dropped again by the first poll that uses it, should this gateway
        # take turns again after all.
        self._saved_cursor = self.cursor
        if self.cursor is None:
            logger.warning(
                "filament-fcm: the gateway is draining; %d poll_work item(s) "
                "arrived before the loop had a cursor and may not come back",
                count,
            )
            return
        logger.info(
            "filament-fcm: the gateway is draining; left %d poll_work item(s) "
            "for the next gateway",
            count,
        )
        self._keep(self.cursor)

    def _keep(self, cursor: str | None) -> None:
        if self._keep_cursor is None:
            return
        try:
            self._keep_cursor(cursor)
        except Exception:
            logger.warning(
                "filament-fcm: could not save the poll_work cursor", exc_info=True
            )

    async def _back_off(self, reason: str) -> None:
        self.failures += 1
        delay = min(_BACKOFF_FIRST_S * 2 ** (self.failures - 1), _BACKOFF_MAX_S)
        # A dropped connection now and then is the network, not a fault: the
        # retry passes the same cursor, so the server hands the same work
        # again and nothing is lost or doubled. Only a run of failures is
        # worth a warning.
        log = (
            logger.warning
            if self.failures >= _WARN_AFTER_FAILURES
            and (self.failures - _WARN_AFTER_FAILURES) % 10 == 0
            else logger.debug
        )
        log(
            "filament-fcm: poll_work failed (%s); retrying in %.0fs (failure %d)",
            reason,
            delay,
            self.failures,
        )
        await self._sleep(delay)

    async def run(self) -> None:
        """Poll until cancelled; raises PollFatal when polling can't go on."""
        logger.info(
            "filament-fcm: polling for work (wait %ss, up to %d items)",
            self._wait_seconds,
            self._max_items,
        )
        while True:
            await self.poll_once()
