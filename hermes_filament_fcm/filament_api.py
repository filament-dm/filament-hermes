"""Filament MCP tool proxy for sending messages.

Uses httpx (available in Hermes core) to call Filament MCP tools over HTTP.
This module only handles MCP protocol concerns — the MCP token is provided
via the FILAMENT_MCP_TOKEN environment variable.
"""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ._version import DIST_NAME, PLUGIN_VERSION, version_headers
from .observability import Stopwatch, current_context, get_logger

logger = logging.getLogger("gateway.filament_fcm")
slog = get_logger()

# Standard MCP initialize params. clientInfo tells the server exactly what
# plugin version this agent runs (the same version also rides on every
# request as User-Agent / X-Filament-Plugin-Version — see version_headers).
_INITIALIZE_PARAMS: dict[str, Any] = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": DIST_NAME, "version": PLUGIN_VERSION},
}

# The signed-URL media tools (filament-dm/synapse#1157). A server that offers
# them gets media moved through them; one that doesn't gets the bearer
# side-channels (/media, /upload), which every server has. See fetch_media and
# upload_media.
MEDIA_DOWNLOAD_URL_TOOL = "get_media_download_url"
MEDIA_UPLOAD_TOOL = "create_media_upload"

_UPLOAD_CHUNK_BYTES = 64 * 1024


class FilamentAPI:
    """HTTP client for calling Filament MCP tools.

    Requires a pre-generated MCP token (from the FILAMENT_MCP_TOKEN env var).
    """

    def __init__(self, mcp_url: str, mcp_token: str) -> None:
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 1
        # The httpx client is created lazily and bound to the event loop that
        # first uses it, then recreated if the running loop changes. An
        # AsyncClient's connection pool binds its async primitives (locks/events)
        # to a single loop, so sharing one across loops — the firebase-messaging
        # thread, the gateway loop, a reconnect — raises "bound to a different
        # event loop". See _client_for_loop.
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # poll_work's own client (see poll_work): the long poll never shares
        # a connection, or a client, with anything else.
        self._poll_client: httpx.AsyncClient | None = None
        self._poll_client_loop: asyncio.AbstractEventLoop | None = None
        # Names of the tools the server offers, set once by register() from
        # the tool list it already fetched (empty until then, and when it fell
        # back to the bundled manifest, which predates the media tools). Read
        # by offers_tool so a per-message choice costs no network call.
        self.server_tools: frozenset[str] = frozenset()

    def offers_tool(self, name: str) -> bool:
        """Whether the server's tool list, as registered, includes *name*."""
        return name in self.server_tools

    def _client_for_loop(self) -> httpx.AsyncClient:
        """Return an httpx client bound to the current running loop, recreating
        it if the loop changed (httpx clients can't be shared across loops).

        In steady state the loop is stable, so this creates the client once;
        recreation only fires on a real loop switch. When it does, the old
        client is closed on *its own* loop (aclose() can't run cross-loop) so
        its sockets/fds aren't leaked.
        """
        loop = asyncio.get_running_loop()
        # is_closed guards the reconnect path: close() (adapter disconnect)
        # closes the client, and the reconnect watcher then reuses this same
        # FilamentAPI on the same loop — without the check, every request
        # after a disconnect would die on "client has been closed" forever.
        if (
            self._client is None
            or self._client.is_closed
            or self._client_loop is not loop
        ):
            old, old_loop = self._client, self._client_loop
            # Client-level default headers ride on every request (MCP,
            # media, side-channels) so the server always sees the plugin
            # version; per-request headers merge on top per-key.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, read=70.0),
                headers=version_headers(),
            )
            self._client_loop = loop
            if old is not None and old_loop is not None and old_loop.is_running():
                # Fire-and-forget aclose on the old loop's thread (suppress the
                # race where it stops between the check and the schedule).
                with contextlib.suppress(RuntimeError):
                    asyncio.run_coroutine_threadsafe(old.aclose(), old_loop)
        return self._client

    def _poll_client_for_loop(self) -> httpx.AsyncClient:
        """The client poll_work uses: its own, bound to the polling loop, with
        no idle connections kept.

        The shared client is replaced whenever a call arrives from another
        loop, and closing the old one cuts any request still in flight on it.
        A 30-second long poll is almost always in flight, so it is the call
        that loses. Without keep-alive every poll also opens a fresh
        connection, so it is never sent on one a proxy has already closed for
        idling. Live run h9 logged ``ReadError`` about every 50 s while idle;
        those are the two ways a pooled long poll gets one. That costs
        one TCP connect per poll, once every 30 seconds."""
        loop = asyncio.get_running_loop()
        if (
            self._poll_client is None
            or self._poll_client.is_closed
            or self._poll_client_loop is not loop
        ):
            old, old_loop = self._poll_client, self._poll_client_loop
            self._poll_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, read=70.0),
                headers=version_headers(),
                limits=httpx.Limits(max_keepalive_connections=0),
            )
            self._poll_client_loop = loop
            if old is not None and old_loop is not None and old_loop.is_running():
                with contextlib.suppress(RuntimeError):
                    asyncio.run_coroutine_threadsafe(old.aclose(), old_loop)
        return self._poll_client

    async def poll_work(self, arguments: dict) -> dict[str, Any] | None:
        """Call poll_work on its own client (see _poll_client_for_loop)."""
        if not self.is_connected:
            await self.initialize()
        return await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/call",
                "params": {"name": "poll_work", "arguments": arguments},
            },
            client=self._poll_client_for_loop(),
        )

    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP initialize handshake."""
        result = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "initialize",
                "params": _INITIALIZE_PARAMS,
            }
        )

        # Only latch initialized on a successful handshake — otherwise a failed
        # initialize (error/non-200) would set is_connected True permanently and
        # subsequent calls would skip re-initialization on a dead session.
        if not (isinstance(result, dict) and isinstance(result.get("result"), dict)):
            logger.warning("filament-fcm: MCP initialize did not succeed: %s", result)
            return result

        # Session ID may come from header or body.
        if self._session_id is None:
            sid = result.get("result", {}).get("_sessionId")
            if sid:
                self._session_id = sid

        # Send initialized notification.
        await self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

        self._initialized = True
        logger.info("MCP session initialized (session_id=%s)", self._session_id)
        return result

    @property
    def is_connected(self) -> bool:
        """Whether the MCP initialize handshake has completed.

        Keyed on the handshake itself, not ``_session_id``: the Filament agents
        endpoint doesn't hand out a session id, so keying on it left
        ``is_connected`` permanently False and re-ran ``initialize()`` before
        every tool call. The session id is still captured and sent when the
        server does provide one (see ``_post`` / ``initialize``).
        """
        return self._initialized

    @staticmethod
    def parse_tool_result(response: dict | None) -> Any:
        """Extract the parsed JSON from an MCP tools/call response.

        MCP responses wrap tool output as::

            {"result": {"content": [{"type": "text", "text": "<json>"}]}}

        Returns the parsed inner object, or the raw response if parsing
        fails.
        """
        if not isinstance(response, dict):
            return response
        result = response.get("result") or {}
        content_list = result.get("content", [])
        if content_list and isinstance(content_list[0], dict):
            text = content_list[0].get("text", "")
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                pass
        return response

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch the available MCP tools from the server (``tools/list``).

        Auto-initializes the MCP session on first use, mirroring
        ``call_tool``. Returns the raw tool definitions (MCP format, with
        ``inputSchema``). Raises if the response is malformed.
        """
        if not self.is_connected:
            await self.initialize()
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/list",
                "params": {},
            }
        )
        tools = (response or {}).get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError(f"unexpected tools/list response: {response}")
        return tools

    @classmethod
    def fetch_tools(
        cls, mcp_url: str, mcp_token: str, timeout: float = 15.0
    ) -> list[dict[str, Any]]:
        """Synchronously fetch the MCP tool list (``initialize`` +
        ``tools/list``).

        Used at plugin-registration time, where there is no event loop and
        we need the tool list before the async adapter session exists. Uses
        a short-lived synchronous client so the shared ``AsyncClient`` (and
        its connection pool) is never bound to a throwaway event loop.

        Raises on any failure so the caller can fall back to the static
        manifest.
        """
        headers = {
            "Authorization": f"Bearer {mcp_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **version_headers(),
        }
        with httpx.Client(timeout=timeout) as client:
            init_resp = client.post(
                mcp_url,
                headers=headers,
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": _INITIALIZE_PARAMS,
                    }
                ),
            )
            init_resp.raise_for_status()
            # Session ID may come from the header or the JSON body — match
            # the async initialize() path, which checks both.
            session_id = init_resp.headers.get("mcp-session-id")
            if not session_id:
                try:
                    session_id = init_resp.json().get("result", {}).get("_sessionId")
                except Exception:
                    session_id = None
            if session_id:
                headers["Mcp-Session-Id"] = session_id

            # Notify the server the session is ready (no response expected).
            client.post(
                mcp_url,
                headers=headers,
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                ),
            )

            list_resp = client.post(
                mcp_url,
                headers=headers,
                content=json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                ),
            )
            list_resp.raise_for_status()
            data = list_resp.json()

        tools = (data or {}).get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError(f"unexpected tools/list response: {data}")
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict[str, Any]:
        """Call an MCP tool and return the result.

        Auto-initializes the MCP session on first use if ``initialize()``
        hasn't been called yet (e.g. in a CLI session where the gateway
        connect lifecycle doesn't run).
        """
        if not self.is_connected:
            await self.initialize()
        return await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    async def set_status(
        self,
        channel: str,
        status_text: str | None = None,
        about_message_id: str | None = None,
        thread_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Set the agent's ephemeral status line; only ``channel`` (and
        ``thread_id``, if any) clears it."""
        arguments: dict[str, Any] = {"channel": channel}
        if status_text is not None:
            arguments["status_text"] = status_text
        if about_message_id is not None:
            arguments["about_message_id"] = about_message_id
        if thread_id is not None:
            arguments["thread_id"] = thread_id
        if timeout_ms is not None:
            arguments["timeout_ms"] = timeout_ms
        return await self.call_tool("set_status", arguments)

    async def post_message(
        self,
        channel: str,
        markdown_body: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Send a message to a room. The server renders markdown to HTML.

        ``attachments`` is ``[{"mxc_url", "filename"?}]`` of already-uploaded
        media (see upload_media); ``markdown_body`` becomes its caption."""
        arguments: dict[str, Any] = {
            "channel": channel,
            "markdown_body": markdown_body,
        }
        if attachments:
            arguments["attachments"] = attachments
        return await self.call_tool("post_message", arguments)

    async def reply_in_thread(
        self,
        message_id: str,
        markdown_body: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Reply in a thread. The server renders markdown to HTML.
        ``attachments`` as for post_message."""
        arguments: dict[str, Any] = {
            "message_id": message_id,
            "markdown_body": markdown_body,
        }
        if attachments:
            arguments["attachments"] = attachments
        return await self.call_tool("reply_in_thread", arguments)

    async def get_thread(self, message_id: str) -> dict[str, Any]:
        """Fetch a thread (root + replies) by message id.

        Works for bare (non-threaded) messages too — the message comes back
        as the root with no replies. Used to look up attachment metadata for
        media messages, which push payloads never include (ENG-603).
        """
        return await self.call_tool("get_thread", {"message_id": message_id})

    async def react(self, message_id: str, key: str) -> dict[str, Any]:
        """Add a reaction (emoji) to a message."""
        return await self.call_tool(
            "react",
            {
                "message_id": message_id,
                "key": key,
            },
        )

    async def unreact(self, message_id: str, key: str) -> dict[str, Any]:
        """Remove a reaction the agent previously added to a message."""
        return await self.call_tool(
            "unreact",
            {
                "message_id": message_id,
                "key": key,
            },
        )

    async def mark_read(self, channel: str, up_to: str) -> dict[str, Any]:
        """Record the agent's read receipt in *channel* through *up_to*.

        The server writes it as an unthreaded receipt, which is positional
        across the room: every earlier event in every thread reads as seen."""
        return await self.call_tool("mark_read", {"channel": channel, "up_to": up_to})

    async def get_self(self) -> dict[str, Any]:
        """Get the authenticated agent's own profile (mxid, display name)."""
        return await self.call_tool("get_self", {})

    async def register_push_token(
        self, token: str, platform: str = "android"
    ) -> dict[str, Any]:
        """Register an FCM push token with the server."""
        return await self.call_tool(
            "register_push_token",
            {
                "token": token,
                "platform": platform,
            },
        )

    async def list_pending_invites(self) -> dict[str, Any]:
        """List loops the agent has been invited to but hasn't joined yet."""
        return await self.call_tool("list_pending_invites", {})

    async def list_vouches(self) -> dict[str, Any]:
        """List pending vouches (loop members who vouched the agent into a loop)."""
        return await self.call_tool("list_vouches", {})

    @staticmethod
    def result_error(result: Any) -> str | None:
        """The JSON-RPC error in a tool response, or None on success.

        call_tool returns the raw envelope; a server-side rejection (e.g.
        accept_vouch's knock 403 → -32602) comes back as {"error": {...}}
        with HTTP 200. Callers that log success without checking this
        report actions that never happened — live-observed: 'accepted
        vouch' logged while every knock 403'd, making a broken flow
        invisible to the principal, the admin, and the logs alike."""
        if isinstance(result, dict):
            err = result.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or f"code {err.get('code')}"
                return str(msg)
            if isinstance(err, str) and err:
                return err
        return None

    # _post reports transport-shaped failures as a plain string; anything else
    # reaching result_error came back as a JSON-RPC rejection from the server.
    _RETRYABLE_ERRORS = frozenset({"invalid response", "HTTP 408", "HTTP 429"})

    @classmethod
    def is_retryable_error(cls, err: str | None) -> bool:
        """Whether a ``result_error`` message is worth retrying.

        Only transport-shaped failures are: ``_post`` turns a non-200 into
        ``"HTTP <code>"`` and an unparseable body into ``"invalid response"``.
        A server *rejection* arrives as a JSON-RPC error dict instead (the
        knock 403 → -32602 case); retrying that just repeats a decision the
        server already made, and buries the real reason in noise. Coupled to
        ``_post``'s strings by design — both live in this module.
        """
        if not err:
            return False
        if err in cls._RETRYABLE_ERRORS:
            return True
        return err.startswith("HTTP 5")

    async def accept_vouch(self, loop_id: str) -> dict[str, Any]:
        """Accept a vouch: knock on the loop so a loop admin can approve the agent."""
        return await self.call_tool("accept_vouch", {"loop_id": loop_id})

    async def accept_invite(self, loop_id: str) -> dict[str, Any]:
        """Accept a pending invite (join a loop)."""
        return await self.call_tool("accept_invite", {"loop_id": loop_id})

    # ── Side-channel endpoints (not MCP tools) ────────────────────────
    # These bypass the MCP JSON-RPC surface entirely. They share the
    # same bearer token but hit dedicated HTTP endpoints that are never
    # exposed in the LLM's tool list.

    async def download_media(
        self,
        mxc_url: str,
        dest: str,
        timeout_ms: int | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """GET /mcp/agents/media — stream an attachment's raw bytes to *dest*.

        MCP tool results are JSON, so media bytes never flow through
        tools/call: the read tools (get_thread, get_recent_messages, ...)
        surface an ``mxc_url`` reference in their ``media`` blocks and this
        side-channel serves the content. ``mxc_url`` is the canonical query
        param on current servers; older deployments only understand ``mxc``,
        so both are sent.

        Streamed to disk as ``_stream_to_file`` describes. Returns the number
        of bytes written. Raises on any non-200 response, or once the body
        passes *max_bytes*.
        """
        params: dict[str, Any] = {"mxc_url": mxc_url, "mxc": mxc_url}
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return await self._stream_to_file(
            self._mcp_url.rstrip("/") + "/media",
            dest,
            path="/media",
            params=params,
            headers={"Authorization": f"Bearer {self._mcp_token}"},
            max_bytes=max_bytes,
        )

    async def fetch_media(
        self,
        mxc_url: str,
        dest: str,
        message_id: str | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """Save the attachment *mxc_url* to *dest*, by whichever route the
        server offers. Returns the number of bytes written; raises on failure.

        With ``get_media_download_url`` on the server and the *message_id*
        the media came from, the tool mints a short-lived URL after
        authorizing this read of that message, and the URL is fetched with no
        bearer. The call is then audited and capability-checked against the
        message's room like any other tool call, which the bearer GET is not
        (it knows only the mxc). Without the tool, or without a message id
        (the tool serves only the agent's own uploads without one), the
        bearer side-channel serves it as before. A refusal from the tool is
        raised, never retried on the bearer route: that would route around
        the decision.
        """
        if message_id is None or not self.offers_tool(MEDIA_DOWNLOAD_URL_TOOL):
            return await self.download_media(mxc_url, dest, max_bytes=max_bytes)
        info = await self._media_tool(
            MEDIA_DOWNLOAD_URL_TOOL, {"mxc_url": mxc_url, "message_id": message_id}
        )
        # Of the result, download_url is fetched and bytes checked against
        # the cap before the transfer starts. filename/content_type describe
        # the file the caller already chose a name for (from the same media
        # descriptor); expires_at/expires_in_seconds don't matter to a fetch
        # made immediately; `how` is curl advice for a model's shell.
        url = info.get("download_url")
        if not isinstance(url, str) or not url:
            raise RuntimeError(f"{MEDIA_DOWNLOAD_URL_TOOL} returned no download_url")
        size = info.get("bytes")
        if max_bytes is not None and isinstance(size, int) and size > max_bytes:
            raise RuntimeError(f"media is {size} bytes, over the {max_bytes} cap")
        # No Authorization header: the URL is the credential. It is not
        # logged either, for the same reason.
        return await self._stream_to_file(
            url, dest, path="/media/signed", max_bytes=max_bytes
        )

    async def upload_media(
        self,
        path: str,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """Upload the local file *path* as Filament media and return its
        ``mxc_url``, for a message's ``attachments``. Raises on failure.

        The bytes go from this process to the server: never through the
        model, and never with the token in anything the model sees. With
        ``create_media_upload`` on the server, the tool reserves the media
        and returns a single-use URL taking the bytes with no bearer;
        otherwise they are POSTed to the bearer side-channel /upload.
        """
        name = filename or os.path.basename(path)
        ctype = content_type or mimetypes.guess_type(name)[0]
        size = os.path.getsize(path)
        if not self.offers_tool(MEDIA_UPLOAD_TOOL):
            headers = {
                "Authorization": f"Bearer {self._mcp_token}",
                "Content-Type": ctype or "application/octet-stream",
            }
            body = await self._send_file(
                "POST",
                self._mcp_url.rstrip("/") + "/upload",
                path,
                size,
                label="/upload",
                params={"filename": name},
                headers=headers,
            )
            mxc = body.get("mxc_url")
        else:
            arguments: dict[str, Any] = {"filename": name, "bytes": size}
            if ctype:
                arguments["content_type"] = ctype
            info = await self._media_tool(MEDIA_UPLOAD_TOOL, arguments)
            # Of the result, upload_url/method/upload_headers say how to send
            # the bytes and max_bytes is checked first (the server refuses
            # a declared oversize file itself, but says so here too).
            # expires_* don't matter to an upload made immediately; `how` is
            # curl advice for a model's shell. The result's mxc_url is the
            # reserved id, used only if the upload response omits its own.
            url = info.get("upload_url")
            if not isinstance(url, str) or not url:
                raise RuntimeError(f"{MEDIA_UPLOAD_TOOL} returned no upload_url")
            limit = info.get("max_bytes")
            if isinstance(limit, int) and size > limit:
                raise RuntimeError(f"file is {size} bytes, over the {limit} limit")
            extra = info.get("upload_headers")
            headers = (
                {str(k): str(v) for k, v in extra.items()}
                if isinstance(extra, dict)
                else {}
            )
            method = str(info.get("method") or "PUT").upper()
            body = await self._send_file(
                method, url, path, size, label="/upload/signed", headers=headers
            )
            mxc = body.get("mxc_url") or info.get("mxc_url")
        if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
            raise RuntimeError("media upload returned no mxc_url")
        return mxc

    async def _media_tool(self, name: str, arguments: dict) -> dict[str, Any]:
        """Call a media tool and return its result object, raising on a
        JSON-RPC error or an ``{"error": ...}`` result."""
        raw = await self.call_tool(name, arguments)
        err = self.result_error(raw)
        info = self.parse_tool_result(raw) if err is None else None
        if err is None and isinstance(info, dict) and info.get("error"):
            err = str(info["error"])
        if err is not None:
            raise RuntimeError(f"{name} failed: {err}")
        if not isinstance(info, dict):
            raise RuntimeError(f"{name} returned an unexpected result")
        return info

    async def _send_file(
        self,
        method: str,
        url: str,
        path: str,
        size: int,
        *,
        label: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Stream the file at *path* as a request body and return the parsed
        JSON response; raises on any non-200. Read in chunks, not whole, with
        an explicit Content-Length so the body is not sent chunked."""

        async def chunks() -> AsyncIterator[bytes]:
            with open(path, "rb") as f:
                while chunk := f.read(_UPLOAD_CHUNK_BYTES):
                    yield chunk

        timer = Stopwatch.start()
        resp = await self._client_for_loop().request(
            method,
            url,
            params=params,
            content=chunks(),
            headers={**(headers or {}), "Content-Length": str(size)},
        )
        slog.debug(
            "filament_fcm.http.side_channel.complete",
            path=label,
            method=method,
            http_status=resp.status_code,
            duration_ms=timer.elapsed_ms(),
            bytes_sent=size,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"media upload failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        parsed: Any = {}
        with contextlib.suppress(Exception):
            parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {}

    async def _stream_to_file(
        self,
        url: str,
        dest: str,
        *,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """GET *url* and stream the body to *dest*; *path* labels the logs.

        Written chunk by chunk (media can be a large video/document —
        buffering it whole could spike the gateway's memory), first into
        ``<dest>.part`` and renamed into place only on success, so a failed
        or oversize download never leaves a truncated file at *dest*.
        Returns the number of bytes written.
        """
        tmp = f"{dest}.part"
        written = 0
        timer = Stopwatch.start()
        slog.debug(
            "filament_fcm.http.side_channel.start",
            path=path,
            method="GET",
            timeout_ms=(params or {}).get("timeout_ms"),
        )
        try:
            async with self._client_for_loop().stream(
                "GET",
                url,
                params=params,
                headers=headers,
                follow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:200]
                    slog.warning(
                        "filament_fcm.http.side_channel.complete",
                        path=path,
                        method="GET",
                        http_status=resp.status_code,
                        duration_ms=timer.elapsed_ms(),
                        bytes_written=written,
                    )
                    raise RuntimeError(
                        f"media download failed: HTTP {resp.status_code} "
                        f"{detail.decode(errors='replace')}"
                    )
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            raise RuntimeError(
                                f"media is over the {max_bytes}-byte cap"
                            )
                        f.write(chunk)
            os.replace(tmp, dest)
            slog.debug(
                "filament_fcm.http.side_channel.complete",
                path=path,
                method="GET",
                http_status=200,
                duration_ms=timer.elapsed_ms(),
                bytes_written=written,
            )
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
        return written

    async def heartbeat(self) -> None:
        """POST /mcp/agents/heartbeat — periodic keep-alive.

        Sets the agent's presence to online. No request body.
        """
        await self._side_channel_post("/heartbeat")

    async def request_probe(self) -> dict[str, Any]:
        """POST /mcp/agents/probe-request — ask the server to ping us.

        The inverse of every other call in this class. Those all prove we can
        reach the server, and keep proving it while our own push listener is
        dead; this asks the server to send one ``io.filament.ping`` push so the
        inbound path is exercised too. The nonce comes back down that push and
        goes home via ``pong``, so asking is not the same as passing.

        The server applies its own minimum interval and answers
        ``{"probed": false, "reason": "too_soon"}`` rather than erroring.
        """
        return await self._side_channel_post("/probe-request")

    async def pong(self, nonce: str) -> dict[str, Any]:
        """POST /mcp/agents/pong — acknowledge a liveness ping.

        Args:
            nonce: The nonce from the ``io.filament.ping`` FCM push.
        """
        return await self._side_channel_post("/pong", body={"nonce": nonce})

    async def get_config(self) -> tuple[int, dict[str, Any]]:
        """GET /mcp/agents/config — the server-held agent config document.

        Returns ``(status_code, parsed_json)``. A 200 body carries
        ``{"agent_user_id", "config": <object|null>, "revision": <int, 0 if
        never written>, "updated_ms", "updated_by"}``. The raw status is
        surfaced (not collapsed to an ``error`` dict) because the caller
        branches on it: 404 means the server predates the endpoint.
        """
        return await self._side_channel_request("GET", "/config")

    async def put_config(self, body: dict) -> tuple[int, dict[str, Any]]:
        """PUT /mcp/agents/config — replace the agent config document.

        ``body`` is ``{"config": {...}}`` plus an optional ``base_revision``
        for compare-and-set (the create-once seed uses ``base_revision=0``; a
        plain replace omits it). Returns ``(status_code, parsed_json)`` — 200
        carries the new ``revision``, 409 carries ``current_revision`` on a
        revision conflict, 400 an ``invalid_config`` detail.
        """
        return await self._side_channel_request("PUT", "/config", body=body)

    async def post_tools(self, tools: list[dict]) -> tuple[int, dict[str, Any]]:
        """POST /mcp/agents/tools — report the registered tool inventory.

        ``tools`` is a list of ``{"name", "description"?, "origin"?}`` dicts.
        Returns ``(status_code, parsed_json)``; older servers 404 (the caller
        tolerates that).
        """
        return await self._side_channel_request("POST", "/tools", body={"tools": tools})

    async def _side_channel_request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Send a request to a side-channel endpoint under the MCP base URL,
        returning ``(status_code, parsed_json_dict)``.

        Unlike ``_side_channel_post`` this surfaces the raw status code and
        body so callers can branch on protocol-level responses (404 = endpoint
        absent, 409 = revision conflict) instead of a flattened error string.
        A non-JSON / non-object body parses to ``{}``. Transport failures
        raise (httpx exceptions) — the caller owns the fallback behavior.
        """
        url = self._mcp_url.rstrip("/") + path
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._mcp_token}",
        }
        content: str | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body)
        timer = Stopwatch.start()
        slog.debug(
            "filament_fcm.http.side_channel.start",
            **current_context(),
            path=path,
            method=method,
            has_body=body is not None,
        )
        resp = await self._client_for_loop().request(
            method,
            url,
            content=content,
            headers=headers,
        )
        slog.debug(
            "filament_fcm.http.side_channel.complete",
            **current_context(),
            path=path,
            method=method,
            http_status=resp.status_code,
            duration_ms=timer.elapsed_ms(),
        )
        parsed: Any = {}
        if resp.text:
            with contextlib.suppress(Exception):
                parsed = resp.json()
        return resp.status_code, parsed if isinstance(parsed, dict) else {}

    async def _side_channel_post(
        self, path: str, body: dict | None = None
    ) -> dict[str, Any]:
        """POST to a side-channel endpoint under the MCP base URL."""
        url = self._mcp_url.rstrip("/") + path
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._mcp_token}",
        }
        content: str | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body)
        timer = Stopwatch.start()
        slog.debug(
            "filament_fcm.http.side_channel.start",
            path=path,
            method="POST",
            has_body=body is not None,
        )
        resp = await self._client_for_loop().post(
            url,
            content=content,
            headers=headers,
        )
        slog.debug(
            "filament_fcm.http.side_channel.complete",
            path=path,
            method="POST",
            http_status=resp.status_code,
            duration_ms=timer.elapsed_ms(),
        )
        if resp.status_code in (200, 204):
            if resp.status_code == 204 or not resp.text:
                return {}
            try:
                return resp.json()
            except Exception:
                return {}
        logger.error(
            "Side-channel %s failed: %d %s", path, resp.status_code, resp.text[:200]
        )
        return {"error": f"HTTP {resp.status_code}"}

    async def close(self) -> None:
        """Close the HTTP client (best-effort — it may be bound to another loop).

        The cached client is dropped *before* aclose so a later call (the
        gateway reconnecting with this same instance) builds a fresh client
        instead of reusing a closed one.
        """
        clients = (self._client, self._poll_client)
        self._client = self._client_loop = None
        self._poll_client = self._poll_client_loop = None
        for client in clients:
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:
                logger.debug("filament-fcm: error closing http client", exc_info=True)

    def _next_rpc_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _post(
        self, body: dict, client: httpx.AsyncClient | None = None
    ) -> dict[str, Any] | None:
        """Send a JSON-RPC POST to the MCP endpoint (on ``client``, else the
        shared one)."""
        method = body.get("method")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        tool_name = params.get("name") if method == "tools/call" else None
        rpc_id = body.get("id")
        headers = {
            "Authorization": f"Bearer {self._mcp_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        timer = Stopwatch.start()
        slog.debug(
            "filament_fcm.mcp.request.start",
            rpc_id=rpc_id,
            method=method,
            tool_name=tool_name,
            has_session_id=bool(self._session_id),
        )
        resp = await (client or self._client_for_loop()).post(
            self._mcp_url,
            content=json.dumps(body),
            headers=headers,
        )
        duration_ms = timer.elapsed_ms()

        # Capture session ID from response header.
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        # 202 (MCP 2025-03-26) and 204 (MCP 2024-11-05) both indicate
        # a notification was accepted — no response body expected.
        if resp.status_code in (202, 204):
            slog.debug(
                "filament_fcm.mcp.request.complete",
                rpc_id=rpc_id,
                method=method,
                tool_name=tool_name,
                http_status=resp.status_code,
                duration_ms=duration_ms,
                notification=True,
            )
            return None

        if resp.status_code != 200:
            logger.error("MCP request failed: %d %s", resp.status_code, resp.text[:200])
            slog.warning(
                "filament_fcm.mcp.request.complete",
                rpc_id=rpc_id,
                method=method,
                tool_name=tool_name,
                http_status=resp.status_code,
                duration_ms=duration_ms,
                error="http_error",
            )
            return {"error": f"HTTP {resp.status_code}"}

        try:
            parsed = resp.json()
        except Exception:
            logger.error("Failed to parse MCP response: %s", resp.text[:200])
            slog.warning(
                "filament_fcm.mcp.request.complete",
                rpc_id=rpc_id,
                method=method,
                tool_name=tool_name,
                http_status=resp.status_code,
                duration_ms=duration_ms,
                error="invalid_json",
            )
            return {"error": "invalid response"}
        error = parsed.get("error") if isinstance(parsed, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        event = (
            "filament_fcm.mcp.request.error"
            if error_code is not None
            else "filament_fcm.mcp.request.complete"
        )
        log = slog.warning if error_code is not None else slog.debug
        log(
            event,
            rpc_id=rpc_id,
            method=method,
            tool_name=tool_name,
            http_status=resp.status_code,
            jsonrpc_error_code=error_code,
            duration_ms=duration_ms,
        )
        return parsed
