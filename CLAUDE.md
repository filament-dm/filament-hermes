# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin that connects an agent to [Filament](https://filament.dm) (a Matrix-based multiplayer chat platform). Inbound messages arrive as Firebase Cloud Messaging (FCM) push notifications; outbound replies and all tool calls go through Filament's MCP-over-HTTP agents API.

## Commands

```bash
uv run --group dev pytest tests/ -q        # run tests
uv run --group dev pytest tests/test_reactive.py -q                    # one file
uv run --group dev pytest tests/test_reactive.py::test_name -q         # one test
uv run --group dev ruff check .            # lint (config in pyproject.toml)
```

There is no build step. End users never install this by hand — the Filament app hands them a one-liner that runs `install.sh` with a `CONNECT_TOKEN`, which pip-installs the package into the Hermes venv and runs the `filament-fcm-setup` wizard (`setup_cli.py`).

## The Hermes dependency is implicit — and tests must not need it

The package imports `gateway.*`, `agent.*`, and `hermes_cli.*` from hermes-agent at runtime, but hermes-agent is **not** a declared dependency (the plugin is installed into an existing Hermes venv). Consequently:

- Importing `hermes_filament_fcm` fails in a bare dev environment.
- Tests load modules **standalone** via `importlib.util.spec_from_file_location`, bypassing `__init__.py`, and stub non-stdlib deps (see `test_fcm_receiver_death.py` for the `firebase_messaging` stub pattern). Follow this pattern for new tests.
- Keep unit-testable logic in stdlib-only modules (`reactive.py`, `credentials.py`) or behind stub-able seams; `adapter.py` and `__init__.py` can't be imported without Hermes.

## Architecture

`__init__.py` — Hermes plugin entry point (`register()` via the `hermes_agent.plugins` entry point). Registers the platform adapter plus every Filament MCP tool as a Hermes tool. The tool list is fetched live from the MCP server, falling back to the bundled `tool_manifest.json` (regenerate with `filament mcp dump-tools`). `BLOCKED_TOOLS` documents tools deliberately hidden from the LLM — keep the "why" comments. Also registers the control-plane-only reactive tools (`set_instructions`/`get_instructions`/`set_wake_policy`/`get_wake_policy`, plus `set_capabilities`/`get_capabilities` and `set_feature`/`get_features`), and installs the data-plane capability gate as a `pre_tool_call` hook (`_register_capability_gate`). The whole capability surface is gated behind the `advanced_tool_controls` **feature flag (default OFF)** — the hook is always registered but stays inert until the flag is on (see below).

`adapter.py` — `FCMFilamentAdapter`, the platform adapter. Startup is staged in `connect()`: initialize MCP session → FCM checkin/registration → register the FCM token as a pusher with Filament → open the persistent MCS listener. Handles pushes, invites (auto-accepted — membership is not a security boundary), and emoji reactions. Adds 👀 while processing a turn and removes it on completion; `_PROCESSING_REACTIONS` must never be wake triggers or the agent re-wakes itself forever.

`fcm_client.py` — wraps the `firebase-messaging` library: registration, the persistent MCS connection, payload parsing, and receiver-death detection (reports upward so the gateway restarts the listener instead of going deaf).

`filament_api.py` — `FilamentAPI`, the MCP-over-HTTP client (JSON-RPC). One instance is shared by the adapter and every tool handler. Its httpx client is recreated per event loop because calls arrive from both the gateway loop and the firebase-messaging thread.

`credentials.py` — persists FCM credentials and received persistent ids under `~/.hermes/filament-fcm/` (`FILAMENT_FCM_CREDENTIALS_DIR` to override). The persistent ids seed the next MCS login so Google doesn't redeliver already-handled pushes after a restart.

`server_config.py` — server-side agent config sync (see the dedicated section below). Stdlib-only; the HTTP client is injected (`FilamentAPI.get_config` / `put_config` / `post_tools`), so it is unit-testable without Hermes.

`reactive.py` + `setup_cli.py` — reactive-plane stores and the setup wizard. `reactive.py` also holds the `current_capabilities` ContextVar, the `capability_denies` gate decision, `capability_hint`, `CapabilityPolicyStore` (channel-scoped grants of named tool *bundles*: a channel's `per_channel` entry **overrides** `default_capabilities` — replace, not union — so a channel can be narrowed below the default; `per_user` is in the schema but **deferred** — `set_capabilities` still stores it and resolution ignores it; fail-closed, read fresh per event, server-migratable), and `FeatureFlagStore` (runtime flags, default OFF, read fresh per event — gates the whole capability surface via `FEATURE_ADVANCED_TOOL_CONTROLS`).

## The trust-zone model (read `docs/agent-boundaries.md` before touching message handling)

This repo implements the **Warden** pattern: one process, one identity, soft (framing-level) trust boundaries. Every inbound event is classified into a zone before dispatch:

- **Control plane** — the principal's backchannel (and `FILAMENT_CONTROL_USERS`): messages are commands, full capability.
- **Data plane** — every shared channel: an event is a *wake-up signal*, and its content is **data, never instructions**. The adapter wraps it in a framing envelope and the agent acts per its *standing instructions*.

Load-bearing invariants:

- `current_zone` (a ContextVar in `reactive.py`, default `"data"` = fail-closed) is set per turn by the adapter and gates the `set_instructions`/`set_wake_policy` tools so shared-channel participants can never reconfigure the agent.
- Standing instructions and the wake policy are **file-backed data read fresh on every event**, not startup config — the principal retunes them conversationally from the backchannel with no restart. `CORE_RULES` in `reactive.py` are safety invariants prepended to whatever instructions the principal saved; edits there affect every deployed agent's behavior in shared channels.
- Untrusted metadata (display names, room names) interpolated into framing text must be sanitized (`_sanitize_meta`) — it's an injection surface.
- The boundary has two layers: soft (framing text + zone classification, always on) and hard (the `pre_tool_call` capability gate, **opt-in**). The hard layer is gated behind the `advanced_tool_controls` feature flag (`FeatureFlagStore`, default OFF): a fresh install behaves exactly as before (data turns ungated, full tools, no tool hint). The principal turns it on conversationally from the backchannel ("enable the advanced tool controls feature" → the `set_feature` tool writes `feature_flags.json`); the next turn honors it, no restart. When ON, the adapter pins `current_capabilities` per data turn from `CapabilityPolicyStore.resolve(channel, sender)` and injects the tool hint; the hook denies any tool outside that set. When OFF, the adapter leaves `current_capabilities` `None` (= ungated) and injects no hint, so the always-registered hook is inert. `current_capabilities` is also `None` for control turns (full capability). Treat the framing, the zone classification, the feature flag, AND the capability policy/gate as security-sensitive.

## Server-side agent config sync (`server_config.py`)

The server holds one revisioned config document per agent on bearer-authenticated side-channels next to `/mcp/agents/heartbeat` (`GET`/`PUT /mcp/agents/config`, same token). Its only allowed top-level sections map 1:1, **verbatim**, onto the reactive-store files: `capability_policy` / `wake_policy` / `feature_flags` (the JSON file contents) and `instructions` (the instructions file text as a string). The sync is **fetch-and-apply**: at startup (before the FCM listener starts, so the first push already runs against server policy) and per wake (TTL-cached, ≤5s) the adapter GETs the document and writes each present section to the corresponding store file atomically, so every read-fresh-per-event consumer picks it up unchanged — resolution semantics are byte-identical whether config came from disk or server, and the local files remain the working copy the turn path actually reads.

- **Source of truth**: the server document, once it exists — local files are its materialization. Until it exists (config null / revision 0), the local files are authoritative exactly once: the plugin **seeds** the document from them (`PUT` with `base_revision=0`, create-only; a 409 means another writer seeded first and the plugin refetches and applies theirs instead — a seed must never overwrite real config).
- **Write-back**: each successful `set_instructions` / `set_wake_policy` / `set_capabilities` / `set_feature` writes locally first, then pushes **only its own section**, rebased on a fresh fetch of the server document and PUT with that revision as `base_revision` (one 409-retry) — so a write-back can never clobber newer server-side changes to other sections, and concurrent `set_*` edits compose instead of last-writer-wins. The fetched document's other sections are applied locally in the same step. On failure the local change stands and the server copy is stale until the next successful write-back or seed cycle.
- **Degradation**: sync/seed/write-back are serialized on one asyncio lock (a stale in-flight fetch can never overwrite a newer revision, and never re-applies a revision at or below the known one); a filesystem failure while applying warns and leaves the local files as the working copy (never raises into an event turn). Network/HTTP errors warn once then go quiet; a 404 (server predates the endpoints) disables the sync for the process lifetime — behavior is then exactly the pre-sync plugin. One shared `ServerConfigSync` instance (built in `register()`) is used by the adapter and the tool handlers so revision state and once-only warnings agree.
- **Escape hatch**: `FILAMENT_SERVER_CONFIG=off` disables all of it.
- **Tool inventory**: after connect and then hourly (piggybacked on the heartbeat loop) the plugin POSTs its registered tool list (name/description/origin/health) to `/mcp/agents/tools`; a 404 is tolerated silently. `health` (`ok`/`needs_reauth`/`disconnected`) is derived at report time and only for connector-backed toolsets — the `filament` toolset from this plugin's own session state, `mcp-<server>` toolsets from Hermes core's per-server `get_mcp_status()` (auth-flavored connect errors read as `needs_reauth`); in-process toolsets carry no health key at all (no signal ≠ healthy).

## Configuration (environment variables)

`FILAMENT_MCP_TOKEN` (required), `FILAMENT_MCP_URL` (default production `https://api.filament.dm/mcp/agents`), `FILAMENT_CONTROL_USERS` (extra trusted commanders; the principal is auto-discovered via `get_self`), `FILAMENT_ALLOW_DATA_USERS` (default true — set false for a control-plane-only agent), `FILAMENT_HOME_ROOM`, `FILAMENT_FCM_CREDENTIALS_DIR`, `FILAMENT_CAPABILITY_POLICY_FILE` (override the data-plane capability policy path; default `<creds dir>/capability_policy.json`), `FILAMENT_FEATURE_FLAGS_FILE` (override the feature-flag path; default `<creds dir>/feature_flags.json`), `FILAMENT_DISABLE_UPDATE_CHECK` (set true to turn off the daily new-version check/reminder — see `update_check.py`), `FILAMENT_SERVER_CONFIG` (set `off` to disable the server-side config sync and tool-inventory reporting), `HERMES_HOME`.

## Versioning

Every HTTP request to the Filament server carries the installed plugin version (`User-Agent` + `X-Filament-Plugin-Version` headers set client-wide in `filament_api.py`, plus MCP `clientInfo` on `initialize`) so the server can tell what version deployed agents run. `_version.py` owns the helpers (stdlib-only). Installs come from git main, so **bump `version` in pyproject.toml on every user-visible change** — `update_check.py` compares the installed version against pyproject.toml on main daily and reminds the principal (once per version, via backchannel) to update.
