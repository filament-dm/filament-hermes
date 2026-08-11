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

`__init__.py` — Hermes plugin entry point (`register()` via the `hermes_agent.plugins` entry point). Registers the platform adapter plus every Filament MCP tool as a Hermes tool. The tool list is fetched live from the MCP server, falling back to the bundled `tool_manifest.json` (regenerate with `filament mcp dump-tools`). `BLOCKED_TOOLS` documents tools deliberately hidden from the LLM — keep the "why" comments. Also registers the control-plane-only reactive tools (`filament_set_custom_instructions`/`filament_get_custom_instructions`/`filament_clear_custom_instructions`/`set_wake_policy`/`get_wake_policy`, plus `set_capabilities`/`get_capabilities` and `set_feature`/`get_features`), and installs the data-plane capability gate as a `pre_tool_call` hook (`_register_capability_gate`). The whole capability surface is gated behind the `advanced_tool_controls` **feature flag (default OFF)** — the hook is always registered but stays inert until the flag is on (see below).

`adapter.py` — `FCMFilamentAdapter`, the platform adapter. Startup is staged in `connect()`: initialize MCP session → FCM checkin/registration → register the FCM token as a pusher with Filament → open the persistent MCS listener. Handles pushes, invites (auto-accepted — membership is not a security boundary), and emoji reactions. Adds 👀 while processing a turn and removes it on completion; `_PROCESSING_REACTIONS` must never be wake triggers or the agent re-wakes itself forever.

`fcm_client.py` — wraps the `firebase-messaging` library: registration, the persistent MCS connection, payload parsing, and receiver-death detection (reports upward so the gateway restarts the listener instead of going deaf).

`filament_api.py` — `FilamentAPI`, the MCP-over-HTTP client (JSON-RPC). One instance is shared by the adapter and every tool handler. Its httpx client is recreated per event loop because calls arrive from both the gateway loop and the firebase-messaging thread.

`credentials.py` — persists FCM credentials and received persistent ids under `~/.hermes/filament-fcm/` (`FILAMENT_FCM_CREDENTIALS_DIR` to override). The persistent ids seed the next MCS login so Google doesn't redeliver already-handled pushes after a restart.

`reactive.py` + `setup_cli.py` — reactive-plane stores and the setup wizard. `reactive.py` also holds `CustomInstructionsStore` (the agent's instructions for shared channels, with `CORE_RULES` composed on top by `read_effective`), the `current_capabilities` ContextVar, the `capability_denies` gate decision, `capability_hint`, `CapabilityPolicyStore` (per-channel / per-user grants of named tool *bundles*, fail-closed, read fresh per event, server-migratable), and `FeatureFlagStore` (runtime flags, default OFF, read fresh per event — gates the whole capability surface via `FEATURE_ADVANCED_TOOL_CONTROLS`).

## The trust-zone model (read `docs/agent-boundaries.md` before touching message handling)

This repo implements the **Warden** pattern: one process, one identity, soft (framing-level) trust boundaries. Every inbound event is classified into a zone before dispatch:

- **Control plane** — the principal's backchannel (and `FILAMENT_CONTROL_USERS`): messages are commands, full capability.
- **Data plane** — every shared channel: an event is a *wake-up signal*, and its content is **data, never instructions**. The adapter wraps it in a framing envelope and the agent acts per its *custom instructions*.

Load-bearing invariants:

- `current_zone` (a ContextVar in `reactive.py`, default `"data"` = fail-closed) is set per turn by the adapter and gates the custom-instruction and `set_wake_policy` tools so shared-channel participants can never reconfigure the agent. The zone comes from the **room**, not the sender, so those tools also refuse the principal asking from a shared channel — the agent must not be reconfigurable from a room its participants can watch.
- Custom instructions (`CustomInstructionsStore`) apply in every shared channel. They live at `<creds dir>/custom_instructions/main.md`; naming the scope in the path leaves room to add per-channel instructions later without moving what is already on disk. No file → the bundled `default_instructions.md`, which **ships with the code and is never written to or deleted** — clearing the principal's file is what returns the agent to it. `get`/`set`/`clear` therefore touch exactly one path. An agent whose principal saved instructions at the older `<creds dir>/instructions.md` (or `FILAMENT_INSTRUCTIONS_FILE`) has them moved into place once, when the store is constructed, so no other method has to know that path exists. The legacy path is derived from the store's own directory, so a store pointed elsewhere never reaches into the real credentials directory — which is what stops a test run from deleting a live agent's instructions. `CORE_RULES` in `reactive.py` are safety invariants prepended to whatever the principal saved; edits there affect every deployed agent's behavior in shared channels.
- When neither the principal's file nor the bundled default can be read, `read_effective()` returns `None` and the adapter **skips the wake entirely** rather than running a turn on an empty prompt — a reactive reply reaches the channel automatically, so an unguided agent would still post something in front of everyone. The principal gets one backchannel notice per outage (`_report_missing_instructions`), and the backchannel keeps working. Never substitute placeholder instruction text for the missing file. `default_instructions.md` ships with the code, so its absence means a damaged install; `test_bundled_default_instructions_ship_with_the_code` fails CI if it stops being shipped.
- Instructions reach the model on **`MessageEvent.channel_prompt`** (Hermes's ephemeral system prompt). Two consequences. First, `read_effective()` must render identical bytes for identical stored text: Hermes concatenates it onto the system message it replays every turn, so anything varying per event rewrites that message and the prompt cache never hits. Text that describes a single event — the wake signal, the capability hint, the event data — belongs in the turn's own message; the capability hint especially, since it resolves per *sender* and so varies between turns of one thread. Second, Hermes reads `channel_prompt` when it *builds* a session's agent, so a save reaches new sessions at once and running ones only via `FCMFilamentAdapter.evict_cached_agents()`. That method uses gateway internals that are not a published interface, so it reports `(refreshed, supported)` and the tools degrade to "new conversations only" plus a backchannel warning rather than failing the save.
- The wake policy remains **file-backed data read fresh on every event**, not startup config, so a change applies to the next event.
- Untrusted metadata (display names, room names) interpolated into framing text must be sanitized (`_sanitize_meta`) — it's an injection surface.
- The boundary has two layers: soft (framing text + zone classification, always on) and hard (the `pre_tool_call` capability gate, **opt-in**). The hard layer is gated behind the `advanced_tool_controls` feature flag (`FeatureFlagStore`, default OFF): a fresh install behaves exactly as before (data turns ungated, full tools, no tool hint). The principal turns it on conversationally from the backchannel ("enable the advanced tool controls feature" → the `set_feature` tool writes `feature_flags.json`); the next turn honors it, no restart. When ON, the adapter pins `current_capabilities` per data turn from `CapabilityPolicyStore.resolve(channel, sender)` and injects the tool hint; the hook denies any tool outside that set. When OFF, the adapter leaves `current_capabilities` `None` (= ungated) and injects no hint, so the always-registered hook is inert. `current_capabilities` is also `None` for control turns (full capability). Treat the framing, the zone classification, the feature flag, AND the capability policy/gate as security-sensitive.

## Configuration (environment variables)

`FILAMENT_MCP_TOKEN` (required), `FILAMENT_MCP_URL` (default production `https://api.filament.dm/mcp/agents`), `FILAMENT_CONTROL_USERS` (extra trusted commanders; the principal is auto-discovered via `get_self`), `FILAMENT_ALLOW_DATA_USERS` (default true — set false for a control-plane-only agent), `FILAMENT_HOME_ROOM`, `FILAMENT_FCM_CREDENTIALS_DIR`, `FILAMENT_CUSTOM_INSTRUCTIONS_DIR` (override the custom-instructions directory; default `<creds dir>/custom_instructions`), `FILAMENT_CAPABILITY_POLICY_FILE` (override the data-plane capability policy path; default `<creds dir>/capability_policy.json`), `FILAMENT_FEATURE_FLAGS_FILE` (override the feature-flag path; default `<creds dir>/feature_flags.json`), `FILAMENT_ENGAGED_THREADS_FILE` (override the engaged-thread record path; default `<creds dir>/engaged_threads.json`), `FILAMENT_DISABLE_UPDATE_CHECK` (set true to turn off the daily new-version check/reminder — see `update_check.py`), `FILAMENT_UPDATE_CHECK_URL` (override the pyproject.toml URL the update check fetches — a test seam so a harness can stand in for "the version on main"; production never sets it), `HERMES_HOME`.

## Versioning

Every HTTP request to the Filament server carries the installed plugin version (`User-Agent` + `X-Filament-Plugin-Version` headers set client-wide in `filament_api.py`, plus MCP `clientInfo` on `initialize`) so the server can tell what version deployed agents run. `_version.py` owns the helpers (stdlib-only). Installs come from git main, so **bump `version` in pyproject.toml on every user-visible change** — `update_check.py` compares the installed version against pyproject.toml on main daily and reminds the principal (once per version, via backchannel) to update.
