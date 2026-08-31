"""Upgrading the plugin in place (stdlib-only, unit-testable).

The principal asks for it - ``/fil-upgrade`` in the backchannel - and this
module does the work: git-pull the plugin tree, then restart the gateway so
the new code is loaded. Two messages bracket it, one when it starts and one
after the gateway is back up.

Why a git pull and nothing else: the plugin is a *directory plugin*
(git-cloned into ``~/.hermes/plugins/filament``) that carries its Python
dependencies in its own ``vendor/`` tree, which the root ``__init__.py``
prepends to ``sys.path``. Code and deps move together in the same commit,
so ``git pull --ff-only`` - exactly what ``hermes plugins update`` runs - is
the whole update. See ``deps.py``. A release that adds a *new* third-party
dependency without vendoring it is the one case this can't finish; the
existing ``check_requirements`` dep check catches that after the restart and
prints the manual remediation.

The restart is the part that has to survive the process ending. The
completion message cannot come from the process that upgrades - it dies
mid-way - so the target version and the room to announce in are written to
``pending_upgrade.json`` *before* the restart is requested, and the next
process reads that marker on connect and posts the result. That also makes
the announcement honest: it reports the version actually running after the
restart, not the one we hoped to land on.
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


# ── The two messages the principal sees ─────────────────────────────


def build_start_notice(latest: str) -> str:
    return f"Upgrading the Filament plugin to v{latest} and restarting..."


def build_complete_notice(version: str) -> str:
    return f"Upgrade to Filament Plugin version v{version} complete."


def build_failure_notice(latest: str | None, reason: str) -> str:
    """Shown instead of the completion message when the upgrade can't run.

    Deliberately no ``hermes gateway restart`` command: typed into a
    terminal that restarts the gateway in the *foreground* of that terminal,
    which is not what the instruction reads like it does. The reconnect
    one-liner is the same command that installed the plugin, so it recovers
    from any state.
    """
    target = f" to v{latest}" if latest else ""
    return (
        f"⚠️ Couldn't upgrade{target}: {git_error_summary(reason)}. "
        f"To update by hand, {MANUAL_FALLBACK}."
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


# A failed pull can print hundreds of "[new branch]" lines before the one
# sentence that says what went wrong. The whole thing used to go to the
# principal verbatim, burying the reason under fetch chatter.
_MAX_REASON_CHARS = 300


def git_error_summary(text: str, limit: int = _MAX_REASON_CHARS) -> str:
    """The tail of git's output - where it puts the actual error - capped."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # Fetch progress is noise whatever position it lands in.
    lines = [ln for ln in lines if not ln.startswith(("*", "remote:", "From "))]
    summary = " ".join(lines[-3:]) if lines else (text or "").strip()
    return summary[: limit - 1] + "…" if len(summary) > limit else summary


def _git(git_exe: str, target: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git_exe, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_PULL_TIMEOUT_SECONDS,
        cwd=str(target),
    )


def working_tree_is_clean(root: Path | None = None) -> tuple[bool, str]:
    """Whether the plugin tree has no uncommitted changes.

    ``--ff-only`` alone is not the guard it reads like: git refuses only when
    the incoming commits touch a file that is locally modified. Edits to any
    *other* file fast-forward happily, and the gateway then restarts into a
    mixture of pulled and local code. Checking the tree first is what makes
    "your edits are never silently rewritten" true.
    """
    target = root or plugin_root()
    git_exe = _git_executable()
    if not git_exe:
        return False, "git is not installed or not on PATH"
    try:
        result = _git(git_exe, target, "status", "--porcelain")
    except Exception:
        return False, "could not read the plugin tree's git status"
    if result.returncode != 0:
        return False, "could not read the plugin tree's git status"
    changed = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if changed:
        preview = ", ".join(ln[3:] for ln in changed[:3])
        more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
        return False, f"the plugin tree has uncommitted changes: {preview}{more}"
    return True, ""


def on_a_tracking_branch(root: Path | None = None) -> tuple[bool, str]:
    """Whether the tree is on a branch that tracks a remote.

    ``FILAMENT_FCM_REF`` accepts a commit as well as a branch, and installing
    at a commit leaves a detached HEAD. ``git pull`` there fails with "you are
    not currently on a branch" after printing its whole fetch - naming the
    situation is far more use to the principal than that.
    """
    target = root or plugin_root()
    git_exe = _git_executable()
    if not git_exe:
        return False, "git is not installed or not on PATH"
    try:
        result = _git(git_exe, target, "rev-parse", "--abbrev-ref", "HEAD")
    except Exception:
        return False, "could not read the plugin tree's branch"
    branch = (result.stdout or "").strip()
    if result.returncode != 0 or not branch or branch == "HEAD":
        return False, (
            "this plugin is checked out at a fixed commit rather than a "
            "branch, so there is nothing to pull"
        )
    return True, ""


def git_pull(root: Path | None = None) -> tuple[bool, str]:
    """``git pull --ff-only`` the plugin tree. Returns (ok, output-or-reason).

    ``--ff-only`` mirrors ``hermes plugins update``: it refuses rather than
    merging, so local commits fail loudly instead of being silently rewritten
    under a running agent. Uncommitted edits need the separate
    ``working_tree_is_clean`` check — see there.
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

    def save(self, target_version: str, room_id: str | None) -> bool:
        """Write the marker and confirm it is readable. False when it is not.

        ``CredentialStore._write_json`` logs filesystem errors and returns
        normally, so the only way to know the marker survived is to read it
        back. A restart with no durable marker leaves the principal with the
        start notice and then silence, which is worse than not restarting.
        """
        self._store.save_pending_upgrade(
            {
                "target_version": target_version,
                "room_id": room_id,
                "started_ms": int(time.time() * 1000),
            }
        )
        written = self.load()
        return bool(written) and written.get("target_version") == target_version

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
