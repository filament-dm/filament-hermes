"""Auto-status: narrate the current turn via the ``set_status`` tool.

While a turn runs, the agent shows a short status line in the channel (or
thread) it is answering in — "searching the web for \"deploy failures\"" —
composed deterministically from each tool call's name and primary argument.
No model call is involved.

Lifecycle: dispatch parks a :class:`TurnScope` (room, thread, prompting
message) in a pending queue and publishes the opening line. The turn's
first tool call claims a pending scope and binds it to the hook's
``session_id``; later calls follow the binding. (Turns run inside
long-lived session tasks, so ContextVars set at dispatch do not reach tool
execution.) Completion clears the status, on the error path too. The one
exception: the gateway emits a spurious completion right after dispatch
for a message queued into an active session, so a completion inside the
grace window is held until the window closes. The server timeout backstops
anything that leaks.

Set ``FILAMENT_STATUS_UPDATES=0`` to disable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("gateway.filament_fcm")

# One status per this many seconds when the phrase is unchanged: a resend
# only exists to outlive the server timeout.
REFRESH_SECONDS = 20.0
# Floor between publishes even when the phrase changes; tool bursts should
# not spend the server's rate budget.
MIN_INTERVAL_SECONDS = 2.0
# Server-side expiry for each publish; refreshed while the turn runs.
TIMEOUT_MS = 60_000

_MAX_ARG_CHARS = 40
_MAX_LINE_CHARS = 60


@dataclass(frozen=True)
class TurnScope:
    """Where the current turn's status lives."""

    room_id: str
    thread_id: str | None = None
    prompt_event_id: str | None = None


# Completions arriving this soon after dispatch are the gateway's spurious
# early completion for a message queued into an active session, not the turn
# actually ending.
COMPLETION_GRACE_SECONDS = 2.0
# Drop pending/bound entries older than this; the server timeout has long
# since cleared their statuses.
STALE_SECONDS = 15 * 60


def enabled() -> bool:
    return os.environ.get("FILAMENT_STATUS_UPDATES", "1").strip() != "0"


# ── Phrases ──────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _basename(path: Any) -> str:
    return _clip(str(path).rstrip("/").rsplit("/", 1)[-1], _MAX_ARG_CHARS)


def _domain(url: Any) -> str:
    m = re.match(r"^\w+://(?:[^/@]*@)?([^/]+)", str(url))
    return _clip(m.group(1) if m else str(url), _MAX_ARG_CHARS)


def _command_summary(args: dict) -> str:
    command = str(args.get("command", ""))
    try:
        # Hermes-only dependency; the plugin must import without Hermes.
        from agent.display import summarize_shell_command  # noqa: PLC0415

        summary = summarize_shell_command(command)
    except Exception:
        summary = command.split("\n", 1)[0]
    return _clip(summary, _MAX_ARG_CHARS)


def _quoted(key: str) -> Callable[[dict], str]:
    return lambda args: '"' + _clip(args.get(key, ""), _MAX_ARG_CHARS) + '"'


# tool name -> (template, argument renderer). ``{}`` takes the rendered
# argument; templates without ``{}`` ignore it.
_PHRASES: dict[str, tuple[str, Callable[[dict], str] | None]] = {
    # Conversation (Filament MCP tools)
    "get_thread": ("reading the thread", None),
    "get_recent_messages": ("reading the channel", None),
    "search_messages": ("searching messages for {}", _quoted("query")),
    "search_members": ("looking up members", None),
    "get_user_profile": ("looking up a profile", None),
    "list_mentions": ("checking mentions", None),
    "list_reactions": ("checking reactions", None),
    "download_media": ("looking at an attachment", None),
    # Workspace (hermes-cli tools)
    "web_search": ("searching the web for {}", _quoted("query")),
    "web_extract": (
        "reading {}",
        lambda a: _domain(
            (a.get("urls") or [""])[0]
            if isinstance(a.get("urls"), list)
            else a.get("urls", "")
        ),
    ),
    "browser_navigate": ("reading {}", lambda a: _domain(a.get("url", ""))),
    "terminal": ("running {}", _command_summary),
    "read_file": ("reading {}", lambda a: _basename(a.get("path", "a file"))),
    "write_file": ("editing {}", lambda a: _basename(a.get("path", "a file"))),
    "patch": ("editing {}", lambda a: _basename(a.get("path", "a file"))),
    "search_files": ("searching files for {}", _quoted("pattern")),
    "execute_code": ("running code", None),
    "delegate_task": ("delegating tasks", None),
    "image_generate": ("generating an image", None),
    "vision_analyze": ("looking at an image", None),
    "text_to_speech": ("generating audio", None),
    "recall": ("recalling notes", None),
    "todo": ("planning", None),
    "process": ("planning", None),
}

# Tools not worth narrating: sub-second reads and the turn's own plumbing.
_SILENT = frozenset(
    {
        "get_self",
        "mark_read",
        "get_backchannel",
        "list_channels",
        "list_loop_channels",
        "get_channel_details",
        "set_status",
        "clarify",
        "tool_search",
        "tool_describe",
    }
)


# Leading verbs of MCP tool names, turned into present tense.
_VERBS = {
    "list": "listing",
    "get": "fetching",
    "fetch": "fetching",
    "read": "reading",
    "search": "searching",
    "query": "querying",
    "find": "finding",
    "create": "creating",
    "save": "saving",
    "update": "updating",
    "edit": "editing",
    "write": "writing",
    "delete": "deleting",
    "add": "adding",
    "remove": "removing",
    "send": "sending",
    "post": "posting",
    "reply": "replying",
    "merge": "merging",
    "resolve": "resolving",
    "submit": "submitting",
    "prepare": "preparing",
    "extract": "extracting",
    "download": "downloading",
    "upload": "uploading",
    "run": "running",
    "execute": "running",
    "mark": "marking",
    "set": "setting",
    "apply": "applying",
    "move": "moving",
    "copy": "copying",
    "duplicate": "duplicating",
    "convert": "converting",
    "respond": "responding",
    "suggest": "suggesting",
    "complete": "completing",
    "cancel": "cancelling",
    "check": "checking",
}

# For unmapped tools, the first of these args present is worth quoting.
_GENERIC_QUOTABLE_ARGS = ("query", "search", "title", "name", "goal", "question")


def _generic_arg(args: dict | None) -> str:
    """A quoted rendering of the first recognizable primary argument."""
    for key in _GENERIC_QUOTABLE_ARGS:
        value = (args or {}).get(key)
        if isinstance(value, str) and value.strip():
            return ' "' + _clip(value, _MAX_ARG_CHARS) + '"'
    return ""


def _mcp_phrase(tool_name: str, args: dict | None) -> str | None:
    """"mcp_linear_list_issues" -> "listing issues in Linear"."""
    parts = tool_name.split("_")
    if len(parts) < 3 or parts[0] != "mcp":
        return None
    server = parts[1].title()
    verb, rest = parts[2], " ".join(parts[3:])
    doing = _VERBS.get(verb)
    if doing is None:
        return _clip(f"using {server}", _MAX_LINE_CHARS)
    subject = f"{doing} {rest}".strip() + _generic_arg(args)
    return _clip(f"{subject} in {server}", _MAX_LINE_CHARS)


def phrase_for(
    tool_name: str, args: dict | None, scope_room: str | None = None
) -> str | None:
    """The status line for a tool call, or None when it should not publish."""
    if tool_name in _SILENT:
        return None
    # Reading the room the turn is answering in is catching up on the
    # conversation, not visiting some other channel.
    if (
        tool_name == "get_recent_messages"
        and scope_room is not None
        and (args or {}).get("channel") == scope_room
    ):
        return "catching up on the conversation"
    entry = _PHRASES.get(tool_name)
    if entry is None:
        mcp = _mcp_phrase(tool_name, args)
        if mcp is not None:
            return mcp
        # e.g. "browser_click" -> "using browser click"
        return _clip("using " + tool_name.replace("_", " "), _MAX_LINE_CHARS)
    template, render = entry
    if render is None:
        return template
    try:
        rendered = render(args or {})
    except Exception:
        rendered = '""'
    if rendered in ('""', ""):
        return _clip(template.replace(" {}", "").replace("{}", ""), _MAX_LINE_CHARS)
    return _clip(template.format(rendered), _MAX_LINE_CHARS)


# ── Publishing ───────────────────────────────────────────────────────


def should_publish(
    prev_phrase: str | None, prev_ts: float, phrase: str, now: float
) -> bool:
    """Coalescing rule: new phrases wait out the floor interval; repeats
    publish only to outlive the server timeout."""
    if now - prev_ts < MIN_INTERVAL_SECONDS:
        return False
    if phrase == prev_phrase:
        return now - prev_ts >= REFRESH_SECONDS
    return True


@dataclass
class _Pending:
    scope: TurnScope
    created: float
    last_phrase: str | None = None
    last_ts: float = 0.0
    # Set by a completion inside the grace window. A tool call afterward
    # means it was the gateway's spurious completion for a queued message;
    # the grace expiring means the turn really finished.
    completed_early: bool = False
    ended: bool = False
    publishes: set = field(default_factory=set)
    refresh_task: Any = None
    finalize_task: Any = None


class StatusPublisher:
    """Publishes turn statuses through the adapter's Filament API."""

    def __init__(self) -> None:
        self._api: Any = None
        self._loop: Any = None
        # trigger event id -> not-yet-claimed turn (insertion-ordered).
        self._pending: dict[str, _Pending] = {}
        # hermes session id -> claimed turn; trigger id -> session id.
        self._bound: dict[str, _Pending] = {}
        self._trigger_session: dict[str, str] = {}
        # Sessions are per-chat, so a session's first claim fixes which room
        # its later claims may bind to.
        self._session_room: dict[str, str] = {}
        self._tasks: set = set()

    def set_api(self, api: Any) -> None:
        self._api = api
        # May run before the gateway loop exists; begin_turn re-captures
        # the loop on every dispatch.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # -- turn lifecycle (called from the adapter) --

    def begin_turn(self, trigger_event_id: str, scope: TurnScope) -> None:
        if not enabled() or not hasattr(self._api, "set_status"):
            return
        # Dispatch runs on the gateway loop; hook-thread publishes are
        # scheduled onto it.
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        self._prune()
        entry = _Pending(scope=scope, created=time.monotonic())
        self._pending[trigger_event_id] = entry
        self._publish(entry, "reading the conversation")
        # Exempt the first real phrase from the floor, or a single-tool
        # turn's only phrase would be suppressed.
        entry.last_ts = time.monotonic() - MIN_INTERVAL_SECONDS
        # The refresh keeps the line alive past the server timeout during
        # long gaps between tool calls.
        with contextlib.suppress(RuntimeError):
            entry.refresh_task = asyncio.get_running_loop().create_task(
                self._refresh(entry)
            )

    async def end_turn(self, trigger_event_id: str) -> None:
        if not hasattr(self._api, "set_status"):
            return
        entry: _Pending | None = None
        session_id = self._trigger_session.pop(trigger_event_id, None)
        if session_id is not None:
            entry = self._bound.pop(session_id, None)
        else:
            pending = self._pending.get(trigger_event_id)
            if pending is None:
                return
            if time.monotonic() - pending.created < COMPLETION_GRACE_SECONDS:
                pending.completed_early = True
                if pending.finalize_task is None:
                    pending.finalize_task = asyncio.get_running_loop().create_task(
                        self._finalize_after_grace(trigger_event_id)
                    )
                return
            entry = self._pending.pop(trigger_event_id)
        if entry is None:
            return
        self._halt(entry)
        await self._clear(entry)

    # -- per-tool-call (called from the pre_tool_call hook) --

    def on_tool_call(
        self, tool_name: str, args: dict | None, session_id: str
    ) -> None:
        if not enabled() or not hasattr(self._api, "set_status"):
            return
        entry = self._bound.get(session_id)
        if entry is None:
            entry = self._claim(session_id)
        if entry is None:
            return
        phrase = phrase_for(tool_name, args, scope_room=entry.scope.room_id)
        if phrase is None:
            return
        now = time.monotonic()
        if not should_publish(entry.last_phrase, entry.last_ts, phrase, now):
            return
        self._publish(entry, phrase)

    # -- internals --

    def _claim(self, session_id: str) -> _Pending | None:
        """Bind a pending turn to this session.

        Never claim into the wrong room: a session with a known room takes
        only that room's turns, and an unknown session claims only when a
        single turn is pending. Ambiguous turns stay un-narrated.
        """
        if not session_id or not self._pending:
            return None
        known_room = self._session_room.get(session_id)
        if known_room is not None:
            candidates = [
                key
                for key, entry in self._pending.items()
                if entry.scope.room_id == known_room and self._claimable(entry)
            ]
        else:
            candidates = [
                key
                for key, entry in self._pending.items()
                if self._claimable(entry)
            ]
            if len(candidates) != 1:
                return None
        if not candidates:
            return None
        trigger_event_id = candidates[0]
        entry = self._pending.pop(trigger_event_id)
        entry.completed_early = False
        self._bound[session_id] = entry
        self._trigger_session[trigger_event_id] = session_id
        self._session_room[session_id] = entry.scope.room_id
        return entry

    @staticmethod
    def _claimable(entry: _Pending) -> bool:
        # Claimable only inside the grace window, where the completion may
        # have been the gateway's spurious one.
        if not entry.completed_early:
            return True
        return time.monotonic() - entry.created < COMPLETION_GRACE_SECONDS

    def _prune(self) -> None:
        now = time.monotonic()
        cutoff = now - STALE_SECONDS
        for key, entry in list(self._pending.items()):
            finished = entry.completed_early and (
                now - entry.created >= COMPLETION_GRACE_SECONDS
            )
            if entry.created < cutoff or finished:
                del self._pending[key]
                self._halt(entry)
                if finished:
                    self._schedule(self._clear(entry))
        stale_sessions = {
            sid for sid, v in self._bound.items() if v.created < cutoff
        }
        for sid in stale_sessions:
            self._halt(self._bound[sid])
            del self._bound[sid]
        for key in [
            k for k, sid in self._trigger_session.items() if sid in stale_sessions
        ]:
            del self._trigger_session[key]

    def _publish(self, entry: _Pending, phrase: str) -> None:
        entry.last_phrase = phrase
        entry.last_ts = time.monotonic()
        handle = self._schedule(
            self._api.set_status(
                channel=entry.scope.room_id,
                status_text=phrase,
                about_message_id=entry.scope.prompt_event_id,
                thread_id=entry.scope.thread_id,
                timeout_ms=TIMEOUT_MS,
            )
        )
        if handle is not None:
            entry.publishes.add(handle)
            handle.add_done_callback(entry.publishes.discard)

    def _halt(self, entry: _Pending) -> None:
        """Stop a turn's in-flight work so nothing lands after the clear."""
        entry.ended = True
        if entry.refresh_task is not None:
            entry.refresh_task.cancel()
        if entry.finalize_task is not None:
            entry.finalize_task.cancel()
        for handle in list(entry.publishes):
            handle.cancel()

    async def _finalize_after_grace(self, trigger_event_id: str) -> None:
        """Clear a turn whose completion arrived inside the grace window,
        once the window closes with no claim."""
        await asyncio.sleep(COMPLETION_GRACE_SECONDS)
        entry = self._pending.get(trigger_event_id)
        if entry is None or not entry.completed_early:
            return
        del self._pending[trigger_event_id]
        entry.finalize_task = None
        self._halt(entry)
        await self._clear(entry)

    async def _clear(self, entry: _Pending) -> None:
        try:
            await self._api.set_status(
                channel=entry.scope.room_id,
                thread_id=entry.scope.thread_id,
            )
        except Exception:
            logger.debug("filament-fcm: status clear failed", exc_info=True)

    async def _refresh(self, entry: _Pending) -> None:
        while not entry.ended:
            await asyncio.sleep(REFRESH_SECONDS / 2)
            if entry.ended or time.monotonic() - entry.created > STALE_SECONDS:
                return
            if entry.last_phrase is None:
                continue
            if time.monotonic() - entry.last_ts >= REFRESH_SECONDS:
                self._publish(entry, entry.last_phrase)

    def _schedule(self, coro: Any) -> Any:
        """Run a status coroutine on the gateway loop from any context.

        Tool hooks fire on the engine's thread, which has no running loop;
        their calls are handed to the loop captured at dispatch.
        """

        async def _run() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("filament-fcm: status call failed", exc_info=True)

        def _closed_if_cancelled(handle: Any) -> None:
            self._tasks.discard(handle)
            # A handle cancelled before _run started leaves coro unawaited.
            if handle.cancelled():
                with contextlib.suppress(Exception):
                    coro.close()

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(_run(), self._loop)
                future.add_done_callback(_closed_if_cancelled)
                return future
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(_closed_if_cancelled)
        return task


publisher = StatusPublisher()


def pre_tool_call_hook(**kwargs: Any) -> None:
    """Observer hook: never blocks, only narrates."""
    try:
        publisher.on_tool_call(
            str(kwargs.get("tool_name") or ""),
            kwargs.get("args") or {},
            str(kwargs.get("session_id") or ""),
        )
    except Exception:
        logger.debug("filament-fcm: status hook failed", exc_info=True)
    return None
