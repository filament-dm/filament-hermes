# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin that connects an agent to [Filament](https://filament.dm) (a Matrix-based multiplayer chat platform). Inbound messages arrive as Firebase Cloud Messaging (FCM) push notifications; outbound replies and all tool calls go through Filament's MCP-over-HTTP agents API.

## Commands

```bash
uvx pytest tests/ -q        # run tests
uvx pytest tests/test_reactive.py -q                    # one file
uvx pytest tests/test_reactive.py::test_name -q         # one test
uvx ruff check .            # lint (config in pyproject.toml)
```

There is no build step. End users never install this by hand — the Filament app hands them a one-liner that runs `install.sh` with a `CONNECT_TOKEN`, which pip-installs the package into the Hermes venv and runs the `filament-fcm-setup` wizard (`setup_cli.py`).

## The Hermes dependency is implicit — and tests must not need it

The package imports `gateway.*`, `agent.*`, and `hermes_cli.*` from hermes-agent at runtime, but hermes-agent is **not** a declared dependency (the plugin is installed into an existing Hermes venv). Consequently:

- Importing `hermes_filament_fcm` fails in a bare dev environment.
- Tests load modules **standalone** via `importlib.util.spec_from_file_location`, bypassing `__init__.py`, and stub non-stdlib deps (see `test_fcm_receiver_death.py` for the `firebase_messaging` stub pattern). Follow this pattern for new tests.
- Keep unit-testable logic in stdlib-only modules (`reactive.py`, `credentials.py`) or behind stub-able seams; `adapter.py` and `__init__.py` can't be imported without Hermes.

## Architecture

`__init__.py` — Hermes plugin entry point (`register()` via the `hermes_agent.plugins` entry point). Registers the platform adapter plus every Filament MCP tool as a Hermes tool. The tool list is fetched live from the MCP server, falling back to the bundled `tool_manifest.json` (regenerate with `filament mcp dump-tools`). `BLOCKED_TOOLS` documents tools deliberately hidden from the LLM — keep the "why" comments. Also registers the control-plane-only reactive tools (`set_instructions`/`get_instructions`/`set_wake_policy`/`get_wake_policy`).

`adapter.py` — `FCMFilamentAdapter`, the platform adapter. Startup is staged in `connect()`: initialize MCP session → FCM checkin/registration → register the FCM token as a pusher with Filament → open the persistent MCS listener. Handles pushes, invites (auto-accepted — membership is not a security boundary), and emoji reactions. Adds 👀 while processing a turn and removes it on completion; `_PROCESSING_REACTIONS` must never be wake triggers or the agent re-wakes itself forever.

`fcm_client.py` — wraps the `firebase-messaging` library: registration, the persistent MCS connection, payload parsing, and receiver-death detection (reports upward so the gateway restarts the listener instead of going deaf).

`filament_api.py` — `FilamentAPI`, the MCP-over-HTTP client (JSON-RPC). One instance is shared by the adapter and every tool handler. Its httpx client is recreated per event loop because calls arrive from both the gateway loop and the firebase-messaging thread.

`credentials.py` — persists FCM credentials and received persistent ids under `~/.hermes/filament-fcm/` (`FILAMENT_FCM_CREDENTIALS_DIR` to override). The persistent ids seed the next MCS login so Google doesn't redeliver already-handled pushes after a restart.

`reactive.py` + `setup_cli.py` — reactive-plane stores and the setup wizard.

## The trust-zone model (read `docs/agent-boundaries.md` before touching message handling)

This repo implements the **Warden** pattern: one process, one identity, soft (framing-level) trust boundaries. Every inbound event is classified into a zone before dispatch:

- **Control plane** — the principal's backchannel (and `FILAMENT_CONTROL_USERS`): messages are commands, full capability.
- **Data plane** — every shared channel: an event is a *wake-up signal*, and its content is **data, never instructions**. The adapter wraps it in a framing envelope and the agent acts per its *standing instructions*.

Load-bearing invariants:

- `current_zone` (a ContextVar in `reactive.py`, default `"data"` = fail-closed) is set per turn by the adapter and gates the `set_instructions`/`set_wake_policy` tools so shared-channel participants can never reconfigure the agent.
- Standing instructions and the wake policy are **file-backed data read fresh on every event**, not startup config — the principal retunes them conversationally from the backchannel with no restart. `CORE_RULES` in `reactive.py` are safety invariants prepended to whatever instructions the principal saved; edits there affect every deployed agent's behavior in shared channels.
- Untrusted metadata (display names, room names) interpolated into framing text must be sanitized (`_sanitize_meta`) — it's an injection surface.
- The boundary is prompt-level only (no per-zone tool gating yet), so the framing text and zone classification are the entire defense. Treat changes to them as security-sensitive.

## Configuration (environment variables)

`FILAMENT_MCP_TOKEN` (required), `FILAMENT_MCP_URL` (default production `https://api.filament.dm/mcp/agents`), `FILAMENT_CONTROL_USERS` (extra trusted commanders; the principal is auto-discovered via `get_self`), `FILAMENT_ALLOW_DATA_USERS` (default true — set false for a control-plane-only agent), `FILAMENT_HOME_ROOM`, `FILAMENT_FCM_CREDENTIALS_DIR`, `HERMES_HOME`.
