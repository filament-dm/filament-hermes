#!/usr/bin/env python3
"""Setup CLI for hermes-filament-fcm.

Handles the chicken-and-egg problem where `hermes gateway setup` can't
see the plugin until it's in `plugins.enabled`, but the setup wizard is
supposed to handle enabling it.

This script:
  1. Adds 'filament-fcm' to plugins.enabled in config.yaml
  2. Runs the interactive setup (prompts for token, senders, URL)
  3. Restarts the gateway

Usage:
    filament-fcm-setup
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml
from hermes_cli.setup import (
    get_env_value,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt,
    prompt_yes_no,
    remove_env_value,
    save_env_value,
)

from ._version import version_headers
from .filament_api import FilamentAPI

# The Firebase project the gateway registers with. It must be the same project
# the homeserver pushes from, or FCM rejects every token as cross-project and
# the agent is never woken — it connects, looks healthy, and silently answers
# nothing. The plugin defaults to production (see fcm_client), so only other
# homeservers export these; persist them like FILAMENT_MCP_URL below, because
# the gateway starts from .env and never sees the installer's environment.
_FIREBASE_ENV_KEYS = (
    "FILAMENT_FIREBASE_PROJECT_ID",
    "FILAMENT_FIREBASE_API_KEY",
    "FILAMENT_FIREBASE_APP_ID",
    "FILAMENT_FIREBASE_SENDER_ID",
)


def _find_hermes_home() -> Path:
    """Resolve the Hermes home directory."""
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


# The plugin id: the install directory under $HERMES_HOME/plugins, the entry in
# plugins.enabled, and the argument to `hermes plugins update`.
# LEGACY_PLUGIN_ID is what it was called before, which an older install
# still has enabled.
#
# Neither the platform name nor the state directory ~/.hermes/filament-fcm/
# follows this id — both keep the old spelling, so renaming moves no state.
PLUGIN_ID = "filament"
LEGACY_PLUGIN_ID = "filament-fcm"


def migrate_enabled(enabled: list) -> list:
    """Return *enabled* with the legacy id replaced by the current one.

    Order is preserved so an unrelated plugin's position never moves, and the id
    is not duplicated if both are somehow listed.
    """
    out = []
    for item in enabled:
        replaced = PLUGIN_ID if item == LEGACY_PLUGIN_ID else item
        if replaced not in out:
            out.append(replaced)
    if PLUGIN_ID not in out:
        out.append(PLUGIN_ID)
    return out


def legacy_dir_is_ours(path: Path) -> bool:
    """True if *path* is a directory holding a copy of this plugin.

    The gate on deleting anything. It must be a real directory (not a symlink,
    which rmtree refuses anyway) containing our package, so a directory that
    merely happens to sit at the legacy path is left alone.
    """
    if path.is_symlink() or not path.is_dir():
        return False
    return (path / "hermes_filament_fcm").is_dir()


def running_from(path: Path) -> bool:
    """True if this module is being executed out of *path*."""
    try:
        Path(__file__).resolve().relative_to(path.resolve())
    except (ValueError, OSError):
        return False
    return True


def retire_legacy_plugin_dir() -> bool:
    """Move an install off the legacy plugin id. True if anything changed.

    No current install: the legacy tree IS the install (what `hermes plugins
    update` on the old id leaves behind), so rename it. That keeps the git remote,
    and on POSIX is safe even when this code is running out of it — the rename
    keeps the inode.

    Current install present: the legacy tree is a leftover, so remove it. Left in
    place it re-arms whenever anything enables that id, and being loaded second it
    overwrites the new plugin's platform entry.

    Never the state directory ~/.hermes/filament-fcm/ — that is a separate path.
    """
    plugins = _find_hermes_home() / "plugins"
    legacy, current = plugins / LEGACY_PLUGIN_ID, plugins / PLUGIN_ID
    if not legacy_dir_is_ours(legacy):
        return False

    if not current.exists():
        try:
            shutil.move(str(legacy), str(current))
        except OSError as exc:
            print_warning(
                f"Could not move {legacy} to {current} ({exc}). Re-run the "
                f"connect command from the Filament app to reinstall."
            )
            return False
        print_info(f"Moved the plugin from {LEGACY_PLUGIN_ID} to {PLUGIN_ID}")
        return True

    if running_from(legacy):
        print_info(
            f"Leaving the old {LEGACY_PLUGIN_ID} directory in place for now — "
            f"this command is running out of it. It is disabled, and the next "
            f"run removes it."
        )
        return False

    try:
        shutil.rmtree(legacy)
    except OSError as exc:
        print_warning(
            f"Could not remove the old plugin directory {legacy} ({exc}). "
            f"Remove it by hand, or it may shadow this install."
        )
        return False
    print_info(f"Retired the old {LEGACY_PLUGIN_ID} plugin directory")
    return True


def migrate_legacy_install() -> None:
    """Enable the current plugin id, then move the tree off the legacy one.

    Config first: if the second step never runs, a leftover tree is disabled and
    inert, whereas a removed tree leaves nothing loadable.
    """
    _enable_plugin()
    retire_legacy_plugin_dir()


def _enable_plugin() -> None:
    """Enable the plugin in config.yaml, migrating off the legacy id."""
    config_path = _find_hermes_home() / "config.yaml"

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f"plugins:\n  enabled:\n  - {PLUGIN_ID}\n")
        print_info(f"Created {config_path} with {PLUGIN_ID} enabled")
        return

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    plugins = config.setdefault("plugins", {})
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []

    migrated = migrate_enabled(enabled)
    if migrated == enabled:
        print_info(f"Plugin {PLUGIN_ID} is already enabled")
        return

    plugins["enabled"] = migrated
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    if LEGACY_PLUGIN_ID in enabled:
        print_info(f"Renamed {LEGACY_PLUGIN_ID} to {PLUGIN_ID} in {config_path}")
    else:
        print_info(f"Enabled {PLUGIN_ID} in {config_path}")


# JSON-RPC codes from the agents MCP. -32002: token valid but the account
# doesn't exist yet ("reserved" — the principal hasn't finished the connect
# flow). -32001: auth required — the token itself is bad and retrying won't
# help. Everything else is transient.
_RESERVED_CODE = -32002
_AUTH_REQUIRED_CODE = -32001

# How long to keep trying an endpoint that never answers before giving up.
# Generous, because a laptop waking from sleep or a VPN reconnecting can take
# tens of seconds and neither means the URL is wrong.
UNREACHABLE_BUDGET_S = 60.0


def _wait_for_finalization(token: str, url: str) -> tuple[bool, str | None]:
    """Block until the agent is finalized in the Filament app.

    Returns ``(ready, principal_id)``:

    - ``(True, "<@owner:server>")`` when the agent is finalized — the
      principal (owner) is extracted from the same ``get_self`` payload so
      the caller can seed the sender allowlist without prompting for a user
      ID. ``principal_id`` may be ``None`` if the payload lacked an owner.
    - ``(False, None)`` when the token is definitively rejected (auth
      error) or the user pressed Ctrl+C.

    While the agent is reserved, ``get_self`` returns -32002; we nudge once and
    keep polling, unbounded, because that wait is paced by a human naming the
    agent in the app. Being unreachable is bounded at ``UNREACHABLE_BUDGET_S``
    instead — a bad URL never resolves itself. Any reply resets that budget. An
    endpoint that answers but errs is retried forever; only -32001 aborts.
    """

    async def _poll() -> tuple[bool, str | None]:
        api = FilamentAPI(url, token)
        nudged = False
        # When the endpoint cannot be reached AT ALL, give up after this long.
        # Waiting out the reserved window is unbounded on purpose (see the
        # docstring); waiting on an endpoint that never answers is not, because a
        # typo, a dead host or a TLS failure will not fix itself and an indefinite
        # hang tells the user nothing. Reset on any reply, so a blip partway
        # through a legitimate long wait cannot eat the budget and abort it.
        unreachable_since: float | None = None
        try:
            while True:
                try:
                    resp = await api.get_self()
                except Exception as exc:
                    now = time.monotonic()
                    if unreachable_since is None:
                        unreachable_since = now
                    elif now - unreachable_since >= UNREACHABLE_BUDGET_S:
                        detail = str(exc).strip()
                        print_warning(
                            f"Could not reach {url} for "
                            f"{UNREACHABLE_BUDGET_S:.0f}s "
                            f"({type(exc).__name__}"
                            f"{': ' + detail if detail else ''}). Check the "
                            f"endpoint, then re-run with --url."
                        )
                        return False, None
                    await asyncio.sleep(3)
                    continue
                # It answered, so the endpoint is real — whatever it said.
                unreachable_since = None
                err = (resp or {}).get("error")
                if err is None:
                    # Finalized. Pull the principal (owner) out of the
                    # get_self payload so setup can seed the sender allowlist
                    # without prompting for a user ID (mirrors the runtime
                    # extraction in adapter._initialize_api).
                    principal = None
                    data = api.parse_tool_result(resp)
                    if isinstance(data, dict):
                        owner = data.get("owner")
                        if isinstance(owner, dict) and owner.get("user_id"):
                            principal = owner["user_id"]
                        else:
                            principal = data.get("owner_id")
                    return True, principal

                # Only dict errors carry a JSON-RPC code. String errors
                # come from FilamentAPI._post() for HTTP-level failures
                # (e.g. "HTTP 401", "HTTP 500"). 401/403 are definitive
                # token rejections; everything else is transient.
                if not isinstance(err, dict):
                    if isinstance(err, str) and ("401" in err or "403" in err):
                        print_warning(
                            "The server rejected this token. Reconnect in "
                            "the Filament app to get a fresh one, then "
                            "re-run setup."
                        )
                        return False, None
                    await asyncio.sleep(3)
                    continue

                code = err.get("code")
                if code == _RESERVED_CODE:
                    if not nudged:
                        print_info(
                            "This agent isn't finished setting up yet — please "
                            "go back to the Filament app and finish the connect "
                            "flow (naming your agent creates it). This will "
                            "connect automatically once you're done."
                        )
                        nudged = True
                    await asyncio.sleep(3)
                    continue
                if code == _AUTH_REQUIRED_CODE:
                    print_warning(
                        "The server rejected this token. Reconnect in the "
                        "Filament app to get a fresh one, then re-run setup."
                    )
                    return False, None
                # Unknown JSON-RPC error — likely transient, retry.
                await asyncio.sleep(3)
        finally:
            await api.close()

    try:
        ready, principal = asyncio.run(_poll())
        if ready:
            print_success("Agent is finalized — ready to connect.")
            return True, principal
        return False, None
    except KeyboardInterrupt:
        print_info("Stopped waiting. Re-run setup once the agent is created.")
        return False, None


# RFC 8693 token exchange, with the connect token itself as the subject —
# the server consumes it and returns a fresh bearer (ENG-893).
_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def _pasted_token_if_alive(token: str, url: str) -> str | None:
    """After an ambiguous exchange outcome: keep the paste only if it works.

    The exchange consumes its subject token before replying, so a lost reply
    leaves two possibilities: the request never made it (paste still valid) or
    it did (paste dead, and the replacement died with the reply). Ask the
    server which one happened rather than guessing — persisting a dead
    credential would strand the gateway with silent 401s after restart.

    Returns the pasted token when the server still accepts it, ``None`` when
    it is consumed or the probe itself failed (nothing trustworthy to save).
    """
    try:
        resp = httpx.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_self", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {token}", **version_headers()},
            timeout=15.0,
        )
        # Our server 401s a dead token, but JSON-RPC-over-HTTP servers also
        # signal auth failures as -32001 inside a 200 envelope — honor both
        # before trusting a credential enough to persist it.
        try:
            error = resp.json().get("error")
        except Exception:
            error = None
        auth_rejected = (
            isinstance(error, dict) and error.get("code") == _AUTH_REQUIRED_CODE
        )
        alive = resp.status_code != 401 and not auth_rejected
    except Exception:
        alive = False
    if alive:
        print_info("Keeping the pasted token (the exchange didn't go through).")
        return token
    print_warning(
        "The token exchange reply was lost after the server retired the "
        "pasted token, so neither credential is usable. Reconnect in the "
        "Filament app to get a fresh token, then re-run setup."
    )
    return None


def _exchange_connect_token(token: str, url: str) -> str | None:
    """Swap the pasted connect token for a fresh bearer; return what to persist.

    The connect token rode the copy-pasted one-liner, so it lives in shell
    history and the process environment of everything the installer ran. The
    server's token-exchange grant consumes it (single-use) and returns a
    replacement that never appears on a command line or in shell history —
    that replacement is what we save. Call this only after the token validated
    (the agent is finalized): the server refuses to consume a reservation.

    Best-effort against old servers: a definitive refusal keeps the original
    token, so setup still completes against a server that predates the
    exchange (its token endpoint rejects an fmcp_ subject as an invalid
    Matrix token; nothing is consumed). But when the outcome is ambiguous —
    the request may have reached the server and consumed the paste without a
    reply landing — the paste is probed first, and ``None`` is returned when
    nothing usable remains; the caller must then abort without persisting.
    """
    try:
        resp = httpx.post(
            f"{url}/oauth/token",
            data={
                "grant_type": _TOKEN_EXCHANGE_GRANT,
                "subject_token": token,
                "subject_token_type": _ACCESS_TOKEN_TYPE,
            },
            headers=version_headers(),
            timeout=30.0,
        )
    except httpx.ConnectError:
        # Never reached a server, so nothing was consumed.
        print_info("Keeping the pasted token (the exchange didn't go through).")
        return token
    except Exception:
        # Sent, but the reply was lost (timeout, dropped connection, ...).
        return _pasted_token_if_alive(token, url)

    if resp.status_code == 200:
        try:
            fresh = resp.json().get("access_token")
        except Exception:
            fresh = None
        if isinstance(fresh, str) and fresh.startswith("fmcp_"):
            print_info(
                "Exchanged the connect token for a fresh credential "
                "(the pasted one no longer works)."
            )
            return fresh
        # A 200 means the subject was consumed, but the replacement in the
        # body is unusable — same ambiguity as a lost reply.
        return _pasted_token_if_alive(token, url)

    # A definitive refusal consumes nothing (pre-exchange servers 401 the
    # fmcp_ subject as an invalid Matrix token).
    print_info(
        "Keeping the pasted token (this server doesn't rotate connect "
        "tokens, or the exchange didn't go through)."
    )
    return token


def _run_interactive_setup() -> bool | None:
    """Run the interactive setup prompts.

    Returns ``True`` when setup completed successfully (the agent is
    finalized and the gateway should be restarted), ``None`` when the user
    declined to touch an already-working configuration (a no-op, not a
    failure), and ``False`` when setup was aborted or finalization failed.
    """
    print_header("Filament (FCM)")

    # The app's one-line connect command exports the agent token as
    # CONNECT_TOKEN, so the whole flow is a single paste with no token prompt.
    connect_token = os.environ.get("CONNECT_TOKEN", "").strip()

    existing_token = get_env_value("FILAMENT_MCP_TOKEN")
    if existing_token and not connect_token:
        print_info(
            f"Filament FCM: already configured (token: {existing_token[:12]}...)"
        )
        if not prompt_yes_no("Reconfigure?", False):
            return None

    print_info("Connect Hermes to Filament via FCM push notifications.")
    if not connect_token:
        print_info("You'll need an MCP agent token — see the README for how to")
        print_info("generate one using the token exchange endpoint.")
    print()

    # MCP token (required, secret). Prefer CONNECT_TOKEN from the environment
    # (set by the app's copy-paste command) so no interactive prompt is needed.
    if connect_token:
        token = connect_token
        print_info(f"Using MCP agent token from CONNECT_TOKEN ({token[:12]}...).")
    else:
        token = prompt("MCP agent token (fmcp_...)", password=True)
    if not token:
        print_warning("Token is required — skipping setup")
        return False
    token = token.strip()

    # MCP endpoint URL — never prompted. Use FILAMENT_MCP_URL when set (the
    # connect command exports it; local-dev users can export it or edit
    # ~/.hermes/.env), otherwise default to production.
    url = (
        (get_env_value("FILAMENT_MCP_URL") or "https://api.filament.dm/mcp/agents")
        .strip()
        .rstrip("/")
    )

    # Validate the token before persisting any configuration. If the token
    # is rejected or the user aborts, the previous working config in
    # ~/.hermes/.env is preserved rather than being overwritten with bad
    # credentials. _wait_for_finalization also handles the reserved window
    # (polls until the agent is finalized in the app) and returns the
    # principal (owner) it learned from get_self.
    ready, principal_id = _wait_for_finalization(token, url)
    if not ready:
        return False

    # Retire the pasted (shell-history-exposed) token for a fresh one, then
    # persist all configuration. None means neither token survived the
    # exchange — abort before touching the saved config.
    exchanged = _exchange_connect_token(token, url)
    if exchanged is None:
        return False
    token = exchanged
    save_env_value("FILAMENT_MCP_TOKEN", token)
    save_env_value("FILAMENT_MCP_URL", url)
    # A token names an identity; a FILAMENT_HOME_ROOM carried over from a
    # previous one (a reconnect with a different token, or a profile whose
    # .env was cloned from another agent's) would misroute cron/home-channel
    # delivery to the old agent's backchannel. Clear it — the adapter
    # re-derives and persists the right backchannel at the next connect.
    save_env_value("FILAMENT_HOME_ROOM", "")

    # Carry the Firebase project through to the gateway (see _FIREBASE_ENV_KEYS).
    for key in _FIREBASE_ENV_KEYS:
        value = (get_env_value(key) or "").strip()
        if value:
            save_env_value(key, value)

    # Seed FILAMENT_CONTROL_USERS with the principal we learned from get_self.
    # It is the platform's allowed_users_env, so the gateway admits these senders
    # (the owner reaches the agent with no manual `hermes pairing approve`), and
    # the adapter also reads it as its control-plane trusted set for trust-zone
    # framing. The adapter re-adds the principal at runtime too, but seeding here
    # trusts the owner from the very first message. We derive the ID from the
    # token, so the user is never prompted for it.
    senders: list[str] = []
    if principal_id:
        senders.append(principal_id)
    else:
        print_warning(
            "Could not determine the principal (owner) from the token — "
            "you may have to run `hermes pairing approve` once, or set "
            "FILAMENT_CONTROL_USERS manually."
        )

    if not connect_token:
        # Manual-token path: let operators add extra control-plane users beyond
        # the principal (e.g. teammates who should command the agent).
        print_info(
            "Your principal (owner) is added to the control-plane users "
            "automatically. You can grant additional commanders here."
        )
        # Default to the existing extra users (the previously-saved control set
        # minus the current principal) so pressing Enter on reconfigure
        # preserves teammates without re-pinning a stale principal: when
        # reconfiguring with a *different* owner's token, the old principal is
        # not silently carried over. The current principal is prepended fresh
        # below and the list de-duped.
        prior = get_env_value("FILAMENT_CONTROL_USERS") or ""
        prior_extras = ",".join(
            u for u in (s.strip() for s in prior.split(",")) if u and u != principal_id
        )
        extra = prompt(
            "Additional control-plane user IDs (optional, comma-separated)",
            default=prior_extras,
        )
        if extra:
            senders.extend(s for s in extra.replace(" ", "").split(",") if s)

    if senders:
        # De-dupe, preserving order (principal first).
        seen: set[str] = set()
        ordered = [s for s in senders if not (s in seen or seen.add(s))]
        save_env_value("FILAMENT_CONTROL_USERS", ",".join(ordered))
    else:
        # Nothing to allow — clear any stale value so it doesn't persist.
        remove_env_value("FILAMENT_CONTROL_USERS")

    print()
    print_success("Configuration saved to ~/.hermes/.env")

    return True


def _persist(token: str, url: str, principal_id: str | None) -> None:
    """Write the validated connection to the engine's .env.

    Seeds FILAMENT_CONTROL_USERS with the principal, which is the platform's
    allowed_users_env. The owner then reaches the agent from the first message,
    with no `hermes pairing approve` step.
    """
    save_env_value("FILAMENT_MCP_TOKEN", token)
    save_env_value("FILAMENT_MCP_URL", url)
    # Same stale-identity guard as the interactive path: a cloned or
    # reconnected .env must not keep the previous agent's home room.
    save_env_value("FILAMENT_HOME_ROOM", "")

    # Carry the Firebase project through, as the interactive path does (see
    # _FIREBASE_ENV_KEYS). Against a non-production homeserver these come from
    # the invoking environment, and a gateway that starts without them registers
    # with the wrong project: it connects, looks healthy, and is never woken.
    for key in _FIREBASE_ENV_KEYS:
        value = (get_env_value(key) or "").strip()
        if value:
            save_env_value(key, value)

    # The principal alone, matching what the interactive path writes on this
    # same (CONNECT_TOKEN) branch. That path also prompts for extra commanders,
    # and re-writing this key drops any that were granted — but that is main's
    # behaviour and predates this command, so it is fixed separately.
    if principal_id:
        save_env_value("FILAMENT_CONTROL_USERS", principal_id)
    else:
        remove_env_value("FILAMENT_CONTROL_USERS")
        print_warning(
            "Could not determine the principal (owner) from the token. Run "
            "`hermes pairing approve` once, or set FILAMENT_CONTROL_USERS."
        )


def token_source(token: str, from_stdin: bool) -> str:
    """Where to read the token: "argv", "stdin", or "conflict".

    Both together is a conflict rather than a preference: the one on the command
    line is already in the shell's history whichever we honour.
    """
    if from_stdin and token:
        return "conflict"
    if from_stdin or not token:
        return "stdin"
    return "argv"


def _read_token_without_argv() -> str:
    """The token from stdin when it is piped or redirected, else from a prompt."""
    if not sys.stdin.isatty():
        return (sys.stdin.readline() or "").strip()
    return (prompt("Agent token (fmcp_...)", password=True) or "").strip()


def connect(
    token: str,
    url: str | None = None,
    restart: bool = True,
    from_stdin: bool = False,
) -> int:
    """Connect this agent to Filament with *token*. Returns an exit code.

    Blocks while the agent is reserved, so it may be run before the agent is
    named in the app. Overwrites an existing token, so it is the reconnect path
    too. With *from_stdin*, the token is read from stdin instead of *token*.
    """
    token = (token or "").strip()
    source = token_source(token, from_stdin)
    if source == "conflict":
        print_warning(
            "-p reads the token from stdin, so do not also pass it as an "
            "argument. Drop one of the two."
        )
        return 2
    if source == "stdin":
        token = _read_token_without_argv()
    if not token:
        print_warning("A token is required. Copy it from Filament's connect flow.")
        return 2

    resolved = (
        (
            url
            or get_env_value("FILAMENT_MCP_URL")
            or "https://api.filament.dm/mcp/agents"
        )
        .strip()
        .rstrip("/")
    )

    print_header("Filament (FCM)")

    # Validate before touching anything. A rejected or abandoned token must
    # leave the existing configuration working — and the migration below
    # relocates a directory, which is not something to do on a guess.
    ready, principal_id = _wait_for_finalization(token, resolved)
    if not ready:
        return 1

    migrate_legacy_install()
    # Retire the pasted (shell-history-exposed) token for a fresh one, as
    # late as possible so a failure above never strands an unsaved credential.
    # None means neither token survived the exchange — leave the existing
    # configuration untouched.
    exchanged = _exchange_connect_token(token, resolved)
    if exchanged is None:
        return 1
    _persist(exchanged, resolved, principal_id)
    print_success("Connected. Configuration saved.")

    if restart:
        _restart_gateway()
    else:
        print_info("Restart the gateway to load it: hermes gateway restart")
    return 0


def _restart_gateway() -> None:
    """Restart the gateway immediately, launched DETACHED so setup can exit.

    When no service manager (systemd/launchd) is configured, ``hermes gateway
    restart`` runs the gateway in the FOREGROUND — it prints its banner and
    never returns. Waiting on it (``subprocess.run``) hangs the installer until
    a timeout, and killing it on timeout would tear down the gateway we just
    started. So launch it in its own session with stdio detached and do NOT
    wait: setup returns to the shell immediately while the gateway keeps running
    in the background (logs go to ~/.hermes/logs/gateway.log). Under a service
    manager the command simply exits on its own, which is equally fine.
    """
    print_info("Restarting the gateway...")

    try:
        subprocess.Popen(
            ["hermes", "gateway", "restart"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print_warning("'hermes' command not found. Restart manually:")
        print_info("hermes gateway restart")
        return

    print_info("Gateway restarting in the background...")

    # The health check below is interactive comfort: ~5s of sleep plus a
    # second CLI start purely to print a thumbs-up. A scripted install (the
    # hosted-attach exec, CI) has nobody watching and the server polls the
    # real signal (push registration), so skip it there.
    if not sys.stdout.isatty():
        print_info("Verify it came up with: hermes gateway status")
        return

    # Brief, bounded health check so the installer can give a thumbs-up without
    # blocking on the (possibly foreground) restart. Give the gateway a moment
    # to come up, then ask `hermes gateway status` once — status is a quick,
    # non-daemonizing command, so capturing it with a short timeout is safe.
    time.sleep(3)
    try:
        result = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        print_info("Verify it came up with: hermes gateway status")
        return

    # `hermes gateway status` exits 0 whether up or down, so parse its output:
    # "✓ ... running" vs "✗ ... not running" / "... stopped". Check the
    # negative markers first ("not running" contains "running").
    out = result.stdout or ""
    low = out.lower()
    if "not running" in low or "stopped" in low or "✗" in out:
        print_info("Gateway is still starting; verify with: hermes gateway status")
    elif "running" in low or "✓" in out:
        print_success("Gateway is running.")
    else:
        print_info("Verify the gateway came up with: hermes gateway status")


def main() -> None:
    """Entry point for the filament-fcm-setup command."""
    print()
    print_header("filament-fcm-setup")

    migrate_legacy_install()
    print()
    ready = _run_interactive_setup()
    print()

    # The hosted installer restarts the gateway itself (s6 bounce, or a
    # direct detached spawn) — the wizard's restart would only add a
    # redundant CLI round trip between them. It sets this to say so.
    if ready and not os.environ.get("FILAMENT_SETUP_SKIP_RESTART"):
        _restart_gateway()

    print()
    if ready:
        print_info("Setup complete.")
    elif ready is None:
        print_info("Existing configuration left in place.")
    else:
        print_info("Setup incomplete.")
    print_info("Check status: hermes gateway status")
    print_info("View logs:    tail -f ~/.hermes/logs/gateway.log")
    print()
    if ready is False:
        # Exit nonzero so a scripted install (the hosted-attach exec pipes
        # this through `set -e`) fails loudly instead of reporting an
        # installed agent with no credentials. Observed live: a setup that
        # couldn't reach the MCP URL returned 0, the server recorded
        # hosting_status=installed, and the agent sat connected-looking
        # and tokenless. Declining "Reconfigure?" on a working install is
        # a no-op (None), not a failure — that still exits 0.
        sys.exit(1)


if __name__ == "__main__":
    main()
