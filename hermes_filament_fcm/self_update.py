"""Unattended self-update (stdlib-only, unit-testable).

``update_check.py`` decides *whether* a newer version is out. This module
performs the update without asking: git-pull the plugin tree, then restart
the gateway so the new code is loaded. The principal sees two messages in
the backchannel — one when it starts, one after the gateway is back up.

Why a git pull and nothing else: the plugin is a *directory plugin*
(git-cloned into ``~/.hermes/plugins/filament``) that carries its Python
dependencies in its own ``vendor/`` tree, which the root ``__init__.py``
prepends to ``sys.path``. Code and deps move together in the same commit,
so ``git pull --ff-only`` — exactly what ``hermes plugins update`` runs — is
the whole update. See ``deps.py``. A release that adds a *new* third-party
dependency without vendoring it is the one case this can't finish; the
existing ``check_requirements`` dep check catches that after the restart and
prints the manual remediation.

The restart is the part that has to survive the process ending. The
completion message can't be sent by the process that performs the upgrade —
it dies mid-way — so the target version and the room to announce in are
written to ``pending_upgrade.json`` *before* the restart is requested, and
the next process reads that marker on connect and posts the result. That
also makes the announcement honest: it reports the version actually running
after the restart, not the one we hoped to land on.

Turn it off with ``FILAMENT_DISABLE_AUTO_UPDATE=true`` and the plugin falls
back to reminding the principal to update by hand.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from ._version import version_from_pyproject
from .credentials import CredentialStore

logger = logging.getLogger("gateway.filament_fcm")

# git pull over a slow link, with the vendor tree in the same repo.
GIT_PULL_TIMEOUT_SECONDS = 120

MANUAL_FALLBACK = (
    'copy the reconnect command (from "View Profile" sidebar → Connected … '
    "→ reconnect) and paste that into your Hermes terminal, like you did "
    "during setup"
)


def auto_update_disabled() -> bool:
    """True when the principal (or the operator) opted out of self-update."""
    return os.environ.get("FILAMENT_DISABLE_AUTO_UPDATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# ── The two messages the principal sees ─────────────────────────────


def build_start_notice(latest: str) -> str:
    return f"New version of the Filament Plugin detected (v{latest}), upgrading..."


def build_complete_notice(version: str) -> str:
    return f"Upgrade to Filament Plugin version v{version} complete."


def build_failure_notice(latest: str, reason: str) -> str:
    """Shown instead of the completion message when the upgrade can't run.

    Deliberately no ``hermes gateway restart`` command: typed into a
    terminal that restarts the gateway in the *foreground* of that terminal,
    which is not what the instruction reads like it does. The reconnect
    one-liner is the same command that installed the plugin, so it recovers
    from any state.
    """
    return (
        f"⚠️ Couldn't auto-update to v{latest}: {reason}. To update by hand, "
        f"{MANUAL_FALLBACK}."
    )


# ── The plugin tree ──────────────────────────────────────────────────


def plugin_root() -> Path:
    """The plugin's own directory — this package's parent.

    ``<plugin_root>/hermes_filament_fcm/self_update.py``, so two levels up
    is the tree ``hermes plugins install`` cloned (and the repo root in a
    dev checkout).
    """
    return Path(__file__).resolve().parent.parent


def is_git_checkout(root: Path | None = None) -> bool:
    """True when the plugin tree is a git clone we can pull into.

    False for a bare pip install of the package (no repo to pull), which is
    the case the reminder-instead-of-upgrade path exists for.
    """
    return ((root or plugin_root()) / ".git").exists()


def _git_executable() -> str | None:
    """Resolve git even when the gateway runs with a minimal PATH."""
    found = shutil.which("git")
    if found:
        return found
    for candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
        if os.path.exists(candidate):
            return candidate
    return None


def git_pull(root: Path | None = None) -> tuple[bool, str]:
    """``git pull --ff-only`` the plugin tree. Returns (ok, output-or-reason).

    ``--ff-only`` mirrors ``hermes plugins update``: it refuses rather than
    merging, so a tree with local commits or edits fails loudly instead of
    being silently rewritten under a running agent.
    """
    target = root or plugin_root()
    git_exe = _git_executable()
    if not git_exe:
        return False, "git is not installed or not on PATH"
    try:
        result = subprocess.run(
            [git_exe, "pull", "--ff-only"],
            check=False,  # a failed pull is a message to the principal, not a raise
            capture_output=True,
            text=True,
            timeout=GIT_PULL_TIMEOUT_SECONDS,
            cwd=str(target),
        )
    except FileNotFoundError:
        return False, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"git pull timed out after {GIT_PULL_TIMEOUT_SECONDS}s"
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"git pull failed to run ({exc})"

    if result.returncode != 0:
        reason = (result.stderr or "").strip() or (result.stdout or "").strip()
        return False, reason or "git pull failed"
    return True, (result.stdout or "").strip()


def version_on_disk(root: Path | None = None) -> str | None:
    """The version in the plugin's pyproject.toml *right now*.

    Read fresh from disk, unlike ``_version.PLUGIN_VERSION``, which is a
    module constant frozen at import. After the pull these differ, and the
    difference is what tells us the pull actually landed new code — worth
    knowing before we restart the gateway for it.
    """
    try:
        return version_from_pyproject(
            ((root or plugin_root()) / "pyproject.toml").read_text()
        )
    except Exception:
        return None


# ── The marker that survives the restart ─────────────────────────────


class PendingUpgradeStore:
    """Records an in-flight upgrade across the restart that completes it.

    Written before the restart is requested, read by the next process on
    connect, and cleared once the result has been announced (or found to be
    unannounceable) — so a marker never outlives one restart and can't put
    the agent in a loop.
    """

    def __init__(self, store: CredentialStore | None = None) -> None:
        self._store = store or CredentialStore()

    def load(self) -> dict | None:
        data = self._store.load_pending_upgrade()
        # isinstance, not truthiness: a corrupted pending_upgrade.json can
        # hold any JSON value, and .get on a list would raise on every
        # connect. A non-dict reads as "nothing pending".
        return data if isinstance(data, dict) else None

    def save(self, target_version: str, room_id: str | None) -> None:
        self._store.save_pending_upgrade(
            {
                "target_version": target_version,
                "room_id": room_id,
                "started_ms": int(time.time() * 1000),
            }
        )

    def clear(self) -> None:
        self._store.clear_pending_upgrade()


# ── Restarting ourselves ─────────────────────────────────────────────


def _under_service_manager() -> bool:
    """True when something will restart us if we exit (systemd/launchd/container).

    Mirrors the decision Hermes' own ``/restart`` slash command makes:
    systemd sets INVOCATION_ID, launchd sets XPC_SERVICE_NAME to the job
    label (interactive macOS shells inherit "0", which is not a service).
    Under a supervisor the gateway exits and is restarted for us; without
    one it has to spawn its own detached relaunch helper.
    """
    if os.environ.get("INVOCATION_ID"):
        return True
    if os.environ.get("XPC_SERVICE_NAME", "0") not in ("", "0"):
        return True
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def request_gateway_restart() -> bool:
    """Ask the running gateway to restart itself. False if we can't.

    Goes through the live ``GatewayRunner`` rather than shelling out to
    ``hermes gateway restart``: that command refuses to run from inside the
    gateway process (its own loop guard), and typed by hand it restarts the
    gateway in the foreground of the caller's terminal. ``request_restart``
    is the same path Hermes' ``/restart`` command uses — it drains in-flight
    agent turns before exiting, so an upgrade never cuts off a reply
    mid-sentence.

    Imported lazily and defensively: ``_gateway_runner_ref`` is a Hermes
    internal, so if it moves we degrade to "tell the principal to update by
    hand" instead of crashing the update path.
    """
    try:
        from gateway.run import _gateway_runner_ref  # noqa: PLC0415
    except Exception:
        logger.warning(
            "filament-fcm: cannot reach the gateway runner to restart "
            "(hermes internals moved?)",
            exc_info=True,
        )
        return False

    runner = _gateway_runner_ref()
    if runner is None:
        logger.warning("filament-fcm: no live gateway runner to restart")
        return False

    via_service = _under_service_manager()
    try:
        return bool(
            runner.request_restart(detached=not via_service, via_service=via_service)
        )
    except Exception:
        logger.warning("filament-fcm: gateway restart request failed", exc_info=True)
        return False
