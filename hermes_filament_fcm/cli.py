"""Installs the ``hermes filament`` top-level CLI command.

hermes plugins install filament-dm/filament-hermes --enable --force
hermes filament connect fmcp_YOURTOKEN
hermes filament login
hermes filament restart
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

logger = logging.getLogger("gateway.filament_fcm")


def _setup(parser: Any) -> None:
    """Build the argparse tree. Called by Hermes with our subparser."""
    sub = parser.add_subparsers(dest="filament_command", metavar="<command>")

    connect = sub.add_parser(
        "connect",
        help="Connect this agent to Filament using a token from the app",
        description=(
            "Validate the agent token from Filament's connect flow, save it, "
            "and start the gateway (installing it as a service when there is "
            "none) or restart it. Waits if the agent is still being named "
            "in the app. Run this again with a new token to reconnect."
        ),
    )
    connect.add_argument("token", nargs="?", help="the agent token (fmcp_...)")
    connect.add_argument(
        "-p",
        "--prompt",
        action="store_true",
        dest="from_stdin",
        help="read the token from stdin rather than the command line",
    )
    connect.add_argument(
        "--url",
        default=None,
        help="MCP endpoint (default: the saved value, else production). Point "
        "this at a dev or staging cluster.",
    )
    connect.add_argument(
        "--no-restart",
        action="store_true",
        help="save the configuration but leave the gateway alone",
    )

    login = sub.add_parser(
        "login",
        help="Connect this agent to Filament by signing in, with no token",
        description=(
            "Sign in to Filament in a browser, then save the configuration and "
            "start the gateway (installing it as a service when there is none) "
            "or restart it."
        ),
    )
    login.add_argument(
        "--url",
        default=None,
        help="MCP endpoint (default: the saved value, else production)",
    )
    flow = login.add_mutually_exclusive_group()
    flow.add_argument(
        "--device",
        action="store_const",
        const="device",
        dest="flow",
        default="auto",
        help="sign in from another device with a short link",
    )
    flow.add_argument(
        "--browser",
        action="store_const",
        const="browser",
        dest="flow",
        help="sign in with a browser and a local callback",
    )
    login.add_argument(
        "--no-open",
        action="store_true",
        help="print the sign-in link rather than opening a browser",
    )
    login.add_argument(
        "--no-restart",
        action="store_true",
        help="save the configuration but leave the gateway alone",
    )
    restart = sub.add_parser(
        "restart",
        help="Restart the Hermes gateway the way this host restarts it",
        description=(
            "Restart this profile's gateway: through its s6 slot or service "
            "when it has one, otherwise by stopping it and starting a new one; "
            "start one when none is running. Waits for running turns to end "
            "first and never interrupts one. Run inside the gateway (from an "
            "agent's terminal), it restarts from outside it once the turn ends."
        ),
    )
    restart.add_argument(
        "--wait",
        type=float,
        default=900,
        metavar="SECONDS",
        help="how long to wait for running turns to end (default: 900)",
    )
    restart.add_argument(
        "--ensure",
        action="store_true",
        help="only start the gateway when none is running; never restart one",
    )
    restart.add_argument(
        "--grace",
        type=float,
        default=20,
        metavar="SECONDS",
        help="with --ensure: how long to give a supervisor to bring an exited "
        "gateway back first (default: 20)",
    )
    # Internal: set by the copy that runs outside the gateway.
    restart.add_argument("--after-pid", type=int, default=None, help=argparse.SUPPRESS)
    restart.add_argument(
        "--after-timeout", type=float, default=120, help=argparse.SUPPRESS
    )
    restart.add_argument("--settle", type=float, default=0, help=argparse.SUPPRESS)
    restart.add_argument(
        "--install-service", action="store_true", help=argparse.SUPPRESS
    )
    # Internal, from plugin versions before `restart`: a login started inside
    # the gateway ran this to restart it from outside.
    handoff = sub.add_parser("gateway-handoff")
    handoff.add_argument("--after-pid", type=int, default=None)


def _handler(args: Any) -> int:
    """Dispatch target Hermes calls as ``args.func(args)``."""
    command = getattr(args, "filament_command", None)
    if command == "login":
        from .setup_cli import login  # noqa: PLC0415

        return login(
            url=args.url,
            flow=args.flow,
            open_browser=not args.no_open,
            restart=not args.no_restart,
        )
    if command == "restart":
        from .gateway_restart import run  # noqa: PLC0415

        return run(
            after_pid=args.after_pid,
            after_timeout=args.after_timeout,
            settle=args.settle,
            service=args.install_service,
            only_start=args.ensure,
            grace_s=args.grace,
            wait_s=args.wait,
        )
    if command == "gateway-handoff":
        from .gateway_restart import run  # noqa: PLC0415

        return run(after_pid=args.after_pid, settle=20, service=True)
    if command != "connect":
        print(
            "usage: hermes filament connect <token> | hermes filament login | "
            "hermes filament restart"
        )
        return 2

    from .setup_cli import connect  # noqa: PLC0415 — keep CLI import off the load path

    return connect(
        args.token,
        url=args.url,
        restart=not args.no_restart,
        from_stdin=args.from_stdin,
    )


def register_cli(ctx: Any) -> None:
    """Add ``hermes filament`` if this Hermes supports plugin CLI commands.

    Older versions have no ``register_cli_command``. Degrade quietly: the
    plugin still works, and ``hermes plugins install`` still collects the token
    through the manifest's ``requires_env`` prompt.
    """
    register = getattr(ctx, "register_cli_command", None)
    if register is None:
        logger.debug(
            "filament-fcm: this Hermes has no plugin CLI commands; "
            "`hermes filament connect` unavailable"
        )
        return
    try:
        register(
            name="filament",
            help="Connect this agent to Filament",
            description="Filament agent connection.",
            setup_fn=_setup,
            handler_fn=_handler,
        )
    except Exception:
        logger.warning(
            "filament-fcm: could not register the `hermes filament` command",
            exc_info=True,
        )
