"""Server-side agent config: fetch-and-apply sync for the reactive stores.

The Filament server holds one revisioned config document per agent, served on
bearer-authenticated side-channels next to ``/mcp/agents/heartbeat`` (same
token, same HTTP stack — see ``FilamentAPI.get_config`` / ``put_config``):

    GET /mcp/agents/config
        → {"agent_user_id", "config": <object|null>,
           "revision": <int, 0 if never written>, "updated_ms", "updated_by"}
    PUT /mcp/agents/config  {"config": {...}, "base_revision": <int, optional>}
        → 200 {"agent_user_id", "revision"}
        | 409 {"error": "revision_conflict", "current_revision": N}
        | 400 {"error": "invalid_config", "detail": ...}

The document's only allowed top-level sections map 1:1 onto the local
reactive-store files, **verbatim**:

    capability_policy    → capability_policy.json    (CapabilityPolicyStore)
    wake_policy          → wake_policy.json          (WakePolicyStore)
    instructions         → instructions.md contents  (InstructionsStore; a string)
    feature_flags        → feature_flags.json        (FeatureFlagStore)
    channel_instructions → channel_instructions.json (ChannelInstructionsStore)

The sync is fetch-and-apply: applying a fetched document writes each present
section to the corresponding store file atomically (each store's writer is
tmp+rename), so every existing read-fresh-per-event consumer picks it up
unchanged — resolution semantics are byte-identical whether the config came
from disk or from the server. When the server holds no document yet (config
null / revision 0), the local files SEED it exactly once: a PUT with
``base_revision=0``, so a concurrent seeder loses cleanly (409), in which case
we refetch and apply the winner's document instead.

If the server is unreachable or predates these endpoints, the plugin behaves
exactly as before: the local files remain the working copy (one warning, then
quiet; a 404 disables the sync for the rest of the process). Setting
``FILAMENT_SERVER_CONFIG=off`` disables all of this outright.

Stdlib-only (the HTTP client is injected), so the module is unit-testable
without Hermes.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .reactive import (
    CapabilityPolicyStore,
    ChannelInstructionsStore,
    FeatureFlagStore,
    InstructionsStore,
    WakePolicyStore,
)

logger = logging.getLogger("gateway.filament_fcm")

SECTION_CAPABILITY_POLICY = "capability_policy"
SECTION_WAKE_POLICY = "wake_policy"
SECTION_INSTRUCTIONS = "instructions"
SECTION_FEATURE_FLAGS = "feature_flags"
SECTION_CHANNEL_INSTRUCTIONS = "channel_instructions"

# Per-wake syncs are TTL-cached: a burst of events costs at most one HTTP
# round-trip per window, and the window is short enough that a backchannel
# edit made elsewhere still lands on effectively the next event.
SYNC_TTL_SECONDS = 5.0

# The tool inventory changes only on plugin/config changes, so once per hour
# (piggybacked on the adapter's heartbeat timer) is plenty.
TOOLS_REPORT_INTERVAL_SECONDS = 3600.0

# Health values a reported tool entry can carry (the app's tool-health axis).
# Derived from live connector state at report time, never stored locally.
TOOL_HEALTH_OK = "ok"
TOOL_HEALTH_NEEDS_REAUTH = "needs_reauth"
TOOL_HEALTH_DISCONNECTED = "disconnected"

# Lower-cased substrings of a server's connect error that mean the fix is
# re-authentication (expired/rejected OAuth), not a retry or a restart.
_AUTH_ERROR_MARKERS = (
    "401",
    "unauthorized",
    "authentication",
    "oauth",
    "reauth",
    "invalid_grant",
    "token",
)


def derive_tool_health(
    toolset: str,
    mcp_statuses: dict[str, Any],
    filament_connected: "bool | None",
) -> "str | None":
    """Health of a toolset's tools, or ``None`` when there is no signal.

    Only connector-backed toolsets carry a signal: the plugin's own
    ``filament`` toolset (rides the shared MCP session) and Hermes MCP
    servers (registered as toolset ``mcp-<server>``, matched by server name
    against ``get_mcp_status()`` rows). In-process toolsets return ``None``
    — absence of a signal, not good health.
    """
    if toolset == "filament":
        if filament_connected is None:
            return None
        return TOOL_HEALTH_OK if filament_connected else TOOL_HEALTH_DISCONNECTED
    if toolset.startswith("mcp-"):
        status = mcp_statuses.get(toolset[len("mcp-") :])
        if not isinstance(status, dict):
            return None
        if status.get("connected"):
            return TOOL_HEALTH_OK
        error = str(status.get("error") or "").lower()
        if any(marker in error for marker in _AUTH_ERROR_MARKERS):
            return TOOL_HEALTH_NEEDS_REAUTH
        return TOOL_HEALTH_DISCONNECTED
    return None


def server_config_disabled() -> bool:
    """True if the ``FILAMENT_SERVER_CONFIG=off`` escape hatch is set.

    Any of off/false/0/no/disabled counts; unset or anything else leaves the
    sync enabled.
    """
    value = os.environ.get("FILAMENT_SERVER_CONFIG", "").strip().lower()
    return value in ("off", "false", "0", "no", "disabled")


def _read_json_file(path: Path) -> Any:
    """The parsed JSON object in *path*, or None if absent/unreadable.

    Unreadable is treated like absent (with a warning): the document section
    is simply omitted, never invented, so a corrupt local file can't poison
    the server copy.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning(
            "filament-fcm: unreadable %s — omitting from server-config document",
            path,
            exc_info=True,
        )
        return None


def _read_text_file(path: Path) -> str | None:
    """The text contents of *path*, or None if absent/unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning(
            "filament-fcm: unreadable %s — omitting from server-config document",
            path,
            exc_info=True,
        )
        return None


class ServerConfigSync:
    """Fetch-and-apply synchronization between the server-held agent config
    document and the local reactive-store files.

    One instance is shared by the adapter (per-wake ``sync``) and the ``set_*``
    tool handlers (``write_back``), so the remembered revision and the
    once-only failure warnings are process-wide.

    ``api`` is duck-typed: it needs async ``get_config()``, ``put_config(body)``
    and ``post_tools(tools)`` methods each returning ``(status_code, dict)``
    (``FilamentAPI`` provides them). ``inventory_provider`` is a zero-arg
    callable returning the registered-tool list to report, or None to disable
    inventory reporting.
    """

    def __init__(
        self,
        api: Any,
        *,
        capability_store: CapabilityPolicyStore | None = None,
        wake_store: WakePolicyStore | None = None,
        instructions_store: InstructionsStore | None = None,
        feature_store: FeatureFlagStore | None = None,
        channel_instructions_store: ChannelInstructionsStore | None = None,
        inventory_provider: Callable[[], list[dict]] | None = None,
        ttl_seconds: float = SYNC_TTL_SECONDS,
        tools_interval_seconds: float = TOOLS_REPORT_INTERVAL_SECONDS,
    ) -> None:
        self._api = api
        self._capability_store = capability_store or CapabilityPolicyStore()
        self._wake_store = wake_store or WakePolicyStore()
        self._instructions_store = instructions_store or InstructionsStore()
        self._feature_store = feature_store or FeatureFlagStore()
        self._channel_instructions_store = (
            channel_instructions_store or ChannelInstructionsStore()
        )
        self._inventory_provider = inventory_provider
        self._ttl = float(ttl_seconds)
        self._tools_interval = float(tools_interval_seconds)

        # The escape hatch is read once at construction: disabling is a
        # process-lifetime decision, like the 404 fallback below.
        self._disabled = server_config_disabled()
        if self._disabled:
            logger.info(
                "filament-fcm: server-config sync disabled (FILAMENT_SERVER_CONFIG)"
            )
        # Set when the server answers 404 (endpoint absent — an older server):
        # the sync goes quiet for the rest of the process and the plugin runs
        # on local files exactly as it did before the server grew the endpoint.
        self._config_unavailable = False
        self._tools_unavailable = False

        # The last revision seen from (or written to) the server. None until
        # the first successful exchange.
        self._revision: int | None = None

        # Serializes sync/seed/write-back: concurrent turns and set_* handlers
        # otherwise interleave their read-snapshot-PUT sequences and silently
        # lose each other's edits. A waiter re-checks the TTL after acquiring,
        # so it skips instead of repeating the round-trip just completed.
        self._lock = asyncio.Lock()

        # Sections whose write-back failed and must be retried by the next
        # sync BEFORE it applies the server document (a failed push must not
        # be overwritten by the subsequent fetch).
        self._pending_write_back: set[str] = set()

        self._last_sync: float | None = None
        self._last_tools_report: float | None = None

        # Failure warnings fire once, not per event; repeats drop to debug.
        self._warned_sync = False
        self._warned_tools = False

    # ── Introspection ───────────────────────────────────────────────

    @property
    def revision(self) -> int | None:
        return self._revision

    @property
    def active(self) -> bool:
        """False when the sync is off for this process (escape hatch or the
        server lacks the endpoint)."""
        return not (self._disabled or self._config_unavailable)

    # ── Document ↔ files ────────────────────────────────────────────

    def build_document(self) -> dict:
        """The config document built from the local store files, verbatim: a
        JSON file contributes its parsed object, the instructions file its
        text. A section whose file is absent is omitted (never invented), so
        seeding an untouched install publishes only what was actually written
        locally and the built-in defaults keep resolving client-side exactly
        as before."""
        doc: dict = {}
        for section, path in (
            (SECTION_CAPABILITY_POLICY, self._capability_store.path),
            (SECTION_WAKE_POLICY, self._wake_store.path),
            (SECTION_FEATURE_FLAGS, self._feature_store.path),
            (SECTION_CHANNEL_INSTRUCTIONS, self._channel_instructions_store.path),
        ):
            value = _read_json_file(path)
            if isinstance(value, dict):
                doc[section] = value
        text = _read_text_file(self._instructions_store.path)
        if text is not None:
            doc[SECTION_INSTRUCTIONS] = text
        return doc

    def _apply(self, config: dict, revision: int) -> None:
        """Write each present section to its store file (atomically, via the
        store's own writer — the same serialization the ``set_*`` tools use),
        so every read-fresh consumer picks the new config up unchanged. A
        section of the wrong type is skipped, never partially applied. A
        filesystem failure (read-only, disk full) warns and leaves the local
        files as the working copy — it must never raise into an event turn,
        and the revision stays unremembered so the next sync retries."""
        if not self._write_sections(config):
            return
        self._revision = revision
        logger.info(
            "filament-fcm: applied server config revision %d (sections: %s)",
            revision,
            ", ".join(sorted(config)) or "none",
        )

    def _write_sections(
        self, config: dict, exclude: frozenset[str] = frozenset()
    ) -> bool:
        """Write the present, well-typed, non-excluded sections to their store
        files. True on success; False (after a once-only warning) if any
        write failed."""
        # Each section writes independently (each file write is atomic on its
        # own): one failing section must not block the others, and any failure
        # leaves the revision unremembered so the next sync re-applies. A
        # section absent from the document leaves its local file untouched —
        # sections are replaced, never deleted, by the sync.
        ok = True
        writers = (
            (SECTION_CAPABILITY_POLICY, dict, self._capability_store.write),
            (SECTION_WAKE_POLICY, dict, self._wake_store.write),
            (SECTION_INSTRUCTIONS, str, self._instructions_store.write),
            (SECTION_FEATURE_FLAGS, dict, self._feature_store.write),
            (
                SECTION_CHANNEL_INSTRUCTIONS,
                dict,
                self._channel_instructions_store.write,
            ),
        )
        for section, typ, write in writers:
            if section in exclude:
                continue
            value = config.get(section)
            if not isinstance(value, typ):
                continue
            try:
                write(value)
            except Exception:
                ok = False
                self._warn_sync_failure(
                    f"filament-fcm: applying server-config section {section} "
                    "to its local file failed; continuing on the existing file",
                    exc_info=True,
                )
        return ok

    def _read_section(self, section: str) -> Any:
        """The local file contents for *section* (parsed JSON object, or the
        instructions text), or None when absent/unreadable."""
        if section == SECTION_INSTRUCTIONS:
            return _read_text_file(self._instructions_store.path)
        path = {
            SECTION_CAPABILITY_POLICY: self._capability_store.path,
            SECTION_WAKE_POLICY: self._wake_store.path,
            SECTION_FEATURE_FLAGS: self._feature_store.path,
            SECTION_CHANNEL_INSTRUCTIONS: self._channel_instructions_store.path,
        }[section]
        value = _read_json_file(path)
        return value if isinstance(value, dict) else None

    # ── Fetch-and-apply ─────────────────────────────────────────────

    async def sync(self, *, force: bool = False) -> None:
        """Fetch the server document and apply it to the local files; seed the
        server from the local files if it holds no document yet.

        Called at startup (``force=True``) and per wake. TTL-cached so an
        event burst costs at most one HTTP call per window. Never raises: any
        failure warns once and leaves the local files as the working copy.
        """
        if self._disabled or self._config_unavailable:
            return
        if (
            not force
            and self._last_sync is not None
            and (time.monotonic() - self._last_sync) < self._ttl
        ):
            return
        async with self._lock:
            await self._sync_locked(force=force)

    async def _sync_locked(self, *, force: bool) -> None:
        # A failed write-back retries before fetch-and-apply, so the fetch
        # can't clobber a local edit that never made it to the server.
        for section in sorted(self._pending_write_back):
            if await self._write_back_locked(section):
                self._pending_write_back.discard(section)

        # TTL re-checked under the lock: a caller that waited out an in-flight
        # sync (or write-back) skips instead of repeating the round-trip.
        now = time.monotonic()
        if (
            not force
            and self._last_sync is not None
            and (now - self._last_sync) < self._ttl
        ):
            return
        # Stamp before the call so a burst still costs one round-trip even
        # when that round-trip fails.
        self._last_sync = now

        try:
            status, body = await self._api.get_config()
        except Exception:
            self._warn_sync_failure(
                "filament-fcm: server-config fetch failed; continuing on "
                "local files",
                exc_info=True,
            )
            return
        if status == 404:
            self._config_unavailable = True
            logger.info(
                "filament-fcm: server has no /config endpoint — server-config "
                "sync disabled for this process"
            )
            return
        if status != 200:
            self._warn_sync_failure(
                f"filament-fcm: server-config fetch returned HTTP {status}; "
                "continuing on local files"
            )
            return

        config = body.get("config")
        try:
            revision = int(body.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        if config is None or revision == 0:
            # Never written server-side: the local files are the source of
            # truth exactly once, to create the document.
            await self._seed()
            return
        if not isinstance(config, dict):
            self._warn_sync_failure(
                "filament-fcm: server config document is not an object; ignoring"
            )
            return
        if self._revision is not None and revision <= self._revision:
            # Never apply a revision at or below the one already known: a
            # write-back can bump the server while this fetch was in flight,
            # and applying the stale response would revert the fresh local
            # edit until the next sync window. Equal = already applied.
            return
        self._apply(config, revision)

    async def _seed(self) -> None:
        """Create the server document from the local files, exactly once.

        ``base_revision=0`` makes this a create-only PUT: if another writer
        (a second gateway, the app) seeded between our GET and PUT, the server
        answers 409 and we adopt the winner's document by refetching instead —
        the seed must never overwrite real config.
        """
        doc = self.build_document()
        try:
            status, body = await self._api.put_config(
                {"config": doc, "base_revision": 0}
            )
        except Exception:
            self._warn_sync_failure(
                "filament-fcm: server-config seed failed; continuing on "
                "local files",
                exc_info=True,
            )
            return
        if status == 200:
            rev = body.get("revision")
            if isinstance(rev, int):
                self._revision = rev
            logger.info(
                "filament-fcm: seeded server config from local files "
                "(revision %s, sections: %s)",
                rev,
                ", ".join(sorted(doc)) or "none",
            )
            return
        if status == 409:
            # Someone else seeded first — theirs is the document now.
            try:
                status2, body2 = await self._api.get_config()
            except Exception:
                self._warn_sync_failure(
                    "filament-fcm: server-config refetch after seed conflict "
                    "failed; continuing on local files",
                    exc_info=True,
                )
                return
            config = body2.get("config") if status2 == 200 else None
            try:
                revision = int(body2.get("revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            if isinstance(config, dict) and revision:
                self._apply(config, revision)
            return
        if status == 404:
            self._config_unavailable = True
            logger.info(
                "filament-fcm: server has no /config endpoint — server-config "
                "sync disabled for this process"
            )
            return
        self._warn_sync_failure(
            f"filament-fcm: server-config seed returned HTTP {status}; "
            "continuing on local files"
        )

    # ── Write-back ──────────────────────────────────────────────────

    async def write_back(self, section: str) -> None:
        """Push one locally edited *section* to the server after a ``set_*``
        tool succeeded, rebased on the current server document.

        Fetch the document, replace only *section* with the local file's
        contents, and PUT with the fetched revision as ``base_revision`` — so
        a write-back can never clobber newer server-side changes to *other*
        sections, and two concurrent ``set_*`` handlers compose instead of
        last-writer-wins (the loser's 409 retries on the winner's document).
        The fetched document's other sections are applied locally at the same
        time, so the files never sit stale behind the remembered revision. A
        failure keeps the local change and warns: the server copy stays stale
        until a later write-back or the next process's seed cycle reconciles
        it.
        """
        if self._disabled or self._config_unavailable:
            return
        async with self._lock:
            if not await self._write_back_locked(section):
                # Remember the section so the next sync retries the push
                # BEFORE applying the server document — otherwise the fetch
                # would overwrite the local edit that never made it up.
                self._pending_write_back.add(section)

    async def _write_back_locked(self, section: str) -> bool:
        """The write-back body; caller holds the lock. True on success."""
        for _attempt in (1, 2):
                try:
                    status, body = await self._api.get_config()
                except Exception:
                    self._warn_sync_failure(
                        "filament-fcm: server-config write-back fetch failed; "
                        "local change kept, server copy is stale",
                        exc_info=True,
                    )
                    return False
                if status == 404:
                    self._config_unavailable = True
                    logger.info(
                        "filament-fcm: server has no /config endpoint — "
                        "server-config sync disabled for this process"
                    )
                    return True
                if status != 200:
                    self._warn_sync_failure(
                        f"filament-fcm: server-config write-back fetch returned "
                        f"HTTP {status}; local change kept, server copy is stale"
                    )
                    return False
                server_config = body.get("config")
                try:
                    base_revision = int(body.get("revision") or 0)
                except (TypeError, ValueError):
                    base_revision = 0

                base = dict(server_config) if isinstance(server_config, dict) else {}
                local_value = self._read_section(section)
                if local_value is None:
                    base.pop(section, None)
                else:
                    base[section] = local_value

                try:
                    status, put_body = await self._api.put_config(
                        {"config": base, "base_revision": base_revision}
                    )
                except Exception:
                    self._warn_sync_failure(
                        "filament-fcm: server-config write-back failed; local "
                        "change kept, server copy is stale",
                        exc_info=True,
                    )
                    return False
                if status == 200:
                    # Bring the OTHER sections' local files up to the server
                    # state we just rebased on. The revision is remembered
                    # only if they all applied — otherwise the next sync must
                    # re-apply rather than skip on an already-known revision.
                    applied = True
                    if isinstance(server_config, dict):
                        applied = self._write_sections(
                            server_config, exclude=frozenset({section})
                        )
                    rev = put_body.get("revision")
                    if applied and isinstance(rev, int):
                        self._revision = rev
                    logger.info(
                        "filament-fcm: server config updated (%s, revision %s)",
                        section,
                        rev,
                    )
                    return True
                if status == 409:
                    continue  # rebased on a stale revision — refetch and retry
                if status == 404:
                    self._config_unavailable = True
                    logger.info(
                        "filament-fcm: server has no /config endpoint — "
                        "server-config sync disabled for this process"
                    )
                    return True
                self._warn_sync_failure(
                    f"filament-fcm: server-config write-back returned HTTP "
                    f"{status}; local change kept, server copy is stale"
                )
                return False
        self._warn_sync_failure(
            "filament-fcm: server-config write-back kept conflicting; "
            "local change kept, server copy is stale"
        )
        return False

    # ── Tool-inventory reporting ────────────────────────────────────

    async def maybe_report_tools(self, *, force: bool = False) -> None:
        """POST the registered tool inventory to the server, rate-limited.

        Called once after connect and then piggybacked on the heartbeat timer;
        the interval gate keeps it to at most one POST per hour. Older servers
        without the endpoint 404 — noted once, then silent for the process.
        Never raises.
        """
        if self._disabled or self._tools_unavailable:
            return
        if self._inventory_provider is None:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_tools_report is not None
            and (now - self._last_tools_report) < self._tools_interval
        ):
            return
        self._last_tools_report = now

        try:
            tools = self._inventory_provider() or []
        except Exception:
            logger.debug("filament-fcm: tool inventory unavailable", exc_info=True)
            return
        if not tools:
            return
        try:
            status, _ = await self._api.post_tools(tools)
        except Exception:
            self._warn_tools_failure("filament-fcm: tool-inventory report failed")
            return
        if status == 404:
            self._tools_unavailable = True
            logger.info(
                "filament-fcm: server has no /tools endpoint — tool-inventory "
                "reporting disabled for this process"
            )
        elif status >= 300:
            self._warn_tools_failure(
                f"filament-fcm: tool-inventory report returned HTTP {status}"
            )
        else:
            logger.info(
                "filament-fcm: reported %d registered tool(s) to the server",
                len(tools),
            )

    # ── Once-only warnings ──────────────────────────────────────────

    def _warn_sync_failure(self, message: str, *, exc_info: bool = False) -> None:
        if self._warned_sync:
            logger.debug("%s", message, exc_info=exc_info)
            return
        self._warned_sync = True
        logger.warning(
            "%s (further server-config sync failures will log at debug)",
            message,
            exc_info=exc_info,
        )

    def _warn_tools_failure(self, message: str) -> None:
        if self._warned_tools:
            logger.debug("%s", message)
            return
        self._warned_tools = True
        logger.warning(
            "%s (further tool-inventory failures will log at debug)", message
        )
