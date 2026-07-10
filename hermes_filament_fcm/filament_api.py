"""Filament MCP tool proxy for sending messages.

Uses httpx (available in Hermes core) to call Filament MCP tools over HTTP.
This module only handles MCP protocol concerns — the MCP token is provided
via the FILAMENT_MCP_TOKEN environment variable.
"""

import asyncio
import contextlib
import json
import logging
import os
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

    async def post_message(self, channel: str, markdown_body: str) -> dict[str, Any]:
        """Send a message to a room. The server renders markdown to HTML."""
        return await self.call_tool(
            "post_message",
            {
                "channel": channel,
                "markdown_body": markdown_body,
            },
        )

    async def reply_in_thread(
        self, message_id: str, markdown_body: str
    ) -> dict[str, Any]:
        """Reply in a thread. The server renders markdown to HTML."""
        return await self.call_tool(
            "reply_in_thread",
            {
                "message_id": message_id,
                "markdown_body": markdown_body,
            },
        )

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
        self, mxc_url: str, dest: str, timeout_ms: int | None = None
    ) -> int:
        """GET /mcp/agents/media — stream an attachment's raw bytes to *dest*.

        MCP tool results are JSON, so media bytes never flow through
        tools/call: the read tools (get_thread, get_recent_messages, ...)
        surface an ``mxc_url`` reference in their ``media`` blocks and this
        side-channel serves the content. ``mxc_url`` is the canonical query
        param on current servers; older deployments only understand ``mxc``,
        so both are sent.

        The response is streamed to disk chunk by chunk (media can be a large
        video/document — buffering it whole could spike the gateway's
        memory), first into ``<dest>.part`` and renamed into place only on
        success, so a failed download never leaves a truncated file at
        *dest*. Returns the number of bytes written. Raises on any non-200
        response.
        """
        url = self._mcp_url.rstrip("/") + "/media"
        params: dict[str, Any] = {"mxc_url": mxc_url, "mxc": mxc_url}
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        tmp = f"{dest}.part"
        written = 0
        timer = Stopwatch.start()
        slog.debug(
            "filament_fcm.http.side_channel.start",
            **current_context(),
            path="/media",
            method="GET",
            timeout_ms=timeout_ms,
        )
        try:
            async with self._client_for_loop().stream(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._mcp_token}"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:200]
                    slog.warning(
                        "filament_fcm.http.side_channel.complete",
                        **current_context(),
                        path="/media",
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
                        f.write(chunk)
                        written += len(chunk)
            os.replace(tmp, dest)
            slog.debug(
                "filament_fcm.http.side_channel.complete",
                **current_context(),
                path="/media",
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

    async def pong(self, nonce: str) -> dict[str, Any]:
        """POST /mcp/agents/pong — acknowledge a liveness ping.

        Args:
            nonce: The nonce from the ``io.filament.ping`` FCM push.
        """
        return await self._side_channel_post("/pong", body={"nonce": nonce})

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
            **current_context(),
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
            **current_context(),
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
        client, self._client, self._client_loop = self._client, None, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("filament-fcm: error closing http client", exc_info=True)

    def _next_rpc_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _post(self, body: dict) -> dict[str, Any] | None:
        """Send a JSON-RPC POST to the MCP endpoint."""
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
            **current_context(),
            rpc_id=rpc_id,
            method=method,
            tool_name=tool_name,
            has_session_id=bool(self._session_id),
        )
        resp = await self._client_for_loop().post(
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
                **current_context(),
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
                **current_context(),
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
                **current_context(),
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
            **current_context(),
            rpc_id=rpc_id,
            method=method,
            tool_name=tool_name,
            http_status=resp.status_code,
            jsonrpc_error_code=error_code,
            duration_ms=duration_ms,
        )
        return parsed
