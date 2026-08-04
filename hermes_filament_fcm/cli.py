"""The ``hermes filament`` subcommand.

Hermes lets a plugin add real top-level CLI commands
(``PluginContext.register_cli_command``), and ``main.py`` builds them into its
argparse tree after ``discover_plugins()``. So once the plugin is installed and
enabled, the next ``hermes`` invocation has ``hermes filament connect <token>``.

That is what makes the connect flow a plain pair of commands with the token as
an argument, instead of a shell that pipes a token into a prompt or writes .env
by hand:

    hermes plugins install filament-dm/filament-hermes --enable
    hermes filament connect fmcp_...

The command runs in the CLI process, not the gateway, so it can persist the
token where the gateway will find it and then restart the gateway.
"""

from __future__ import annotations

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
            "and restart the gateway. Waits if the agent is still being named "
            "in the app. Run this again with a new token to reconnect."
        ),
    )
    connect.add_argument("token", nargs="?", help="the agent token (fmcp_...)")
    connect.add_argument(
        "-p",
        "--prompt",
        action="store_true",
        dest="from_stdin",
        help="read the token from stdin rather than the command line — asked "
        "for with the input hidden, or piped in. Keeps it out of argv and shell "
        "history, which is what you want on a shared machine.",
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


def _handler(args: Any) -> int:
    """Dispatch target Hermes calls as ``args.func(args)``."""
    if getattr(args, "filament_command", None) != "connect":
        print("usage: hermes filament connect <token>")
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
