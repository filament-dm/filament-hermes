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
import subprocess
import time
from pathlib import Path

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

from .filament_api import FilamentAPI


def _find_hermes_home() -> Path:
    """Resolve the Hermes home directory."""
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


def _enable_plugin() -> None:
    """Add 'filament-fcm' to plugins.enabled in config.yaml."""
    config_path = _find_hermes_home() / "config.yaml"

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("plugins:\n  enabled:\n  - filament-fcm\n")
        print_info(f"Created {config_path} with filament-fcm enabled")
        return

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    plugins = config.setdefault("plugins", {})
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if "filament-fcm" in enabled:
        print_info("Plugin filament-fcm is already enabled")
        return

    enabled.append("filament-fcm")
    plugins["enabled"] = enabled

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    print_info(f"Enabled filament-fcm in {config_path}")


# JSON-RPC codes from the agents MCP. -32002: token valid but the account
# doesn't exist yet ("reserved" — the principal hasn't finished the connect
# flow). Anything else (e.g. -32001) means the token isn't usable.
_RESERVED_CODE = -32002


def _wait_for_finalization(token: str, url: str) -> tuple[bool, str | None]:
    """Block until the agent is finalized in the Filament app.

    Returns ``(ready, principal_id)``:

    - ``(True, "<@owner:server>")`` when the agent is finalized — the
      principal (owner) is extracted from the same ``get_self`` payload so
      the caller can seed the sender allowlist without prompting for a user
      ID. ``principal_id`` may be ``None`` if the payload lacked an owner.
    - ``(False, None)`` when the token is definitively rejected (auth
      error) or the user pressed Ctrl+C.

    While the agent is reserved, ``get_self`` returns -32002; we show a
    one-time nudge and keep polling, so the flow connects automatically
    once the user finishes naming the agent.  Transient errors (transport
    failures, HTTP 500, non-dict error responses) are retried — only a
    well-formed JSON-RPC auth rejection (-32001) aborts.
    """
    # Only this specific error code means the token itself is bad and
    # retrying won't help. Everything else is transient or reserved.
    _AUTH_REQUIRED_CODE = -32001

    async def _poll() -> tuple[bool, str | None]:
        api = FilamentAPI(url, token)
        nudged = False
        try:
            while True:
                try:
                    resp = await api.get_self()
                except Exception:
                    await asyncio.sleep(3)  # transient transport error — retry
                    continue
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


def _run_interactive_setup() -> bool:
    """Run the interactive setup prompts.

    Returns ``True`` when setup completed successfully (the agent is
    finalized and the gateway should be restarted), ``False`` when setup
    was skipped, aborted, or finalization failed.
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
            return False

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

    # Token validated — persist all configuration.
    save_env_value("FILAMENT_MCP_TOKEN", token)
    save_env_value("FILAMENT_MCP_URL", url)

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

    _enable_plugin()
    print()
    ready = _run_interactive_setup()
    print()

    if ready:
        _restart_gateway()

    print()
    print_info("Setup complete." if ready else "Setup incomplete.")
    print_info("Check status: hermes gateway status")
    print_info("View logs:    tail -f ~/.hermes/logs/gateway.log")
    print()


if __name__ == "__main__":
    main()
