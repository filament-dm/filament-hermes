"""Self-update: pull a newer plugin version and restart the gateway.

The daily check in ``update_check.py`` used to only *remind* the principal
that a newer version is on main. This module lets the plugin apply that
update itself when it can do so safely, falling back to the reminder when it
can't. The whole thing rides the plugin's install model: a directory plugin
git-cloned into ``$HERMES_HOME/plugins/<id>`` with its Python dependencies
vendored in-tree, so ``hermes plugins update <id>`` (a ``git pull --ff-only``
plus Hermes's post-pull security scan) is the complete update — no separate
dependency step. New code then loads on the next gateway restart, which this
module triggers.

Safety gates, all of which fall back to the reminder rather than force
anything:

- ``FILAMENT_DISABLE_AUTO_UPDATE`` turns auto-update off (reminders stay).
- The running tree must be the installed directory plugin — a git clone
  sitting directly in ``$HERMES_HOME/plugins`` — on branch ``main`` with a
  clean working tree. Dev checkouts, ref-pinned installs, and trees with
  local edits are never touched (``hermes plugins update`` would autostash
  and can ``reset --hard`` on conflict; a human should drive that).
- The update goes through ``hermes plugins update`` — never a raw ``git
  pull`` — so Hermes's post-pull security scan always runs. That command is
  non-interactive-safe (its git layer can't prompt; the only prompt is
  capability re-consent, which fails closed off-TTY). A ``dangerous`` scan
  verdict disables the plugin but still exits 0, announced only in its
  output — the caller must treat ``DISABLED`` as "do NOT restart", because
  restarting would load nothing and take the agent dark.
- After the command succeeds, the on-disk pyproject version is re-read and
  must have reached the target — an origin that doesn't serve the version
  (e.g. a fork) reads as a failure, not an update.

Restarting from *inside* the gateway needs one trick: ``hermes gateway
restart`` refuses to run when it detects it's a child of a supervised
gateway (a guard against restart loops, keyed on the ``_HERMES_GATEWAY``
env var). Hermes core's own in-process restart path spawns the CLI with
that variable scrubbed, so ``spawn_gateway_restart`` does the same —
detached (its own session, stdio to /dev/null) so the restart survives
this process's death, exactly like ``setup_cli._restart_gateway``.

Stdlib-only and standalone-loadable; all subprocess work goes through the
module-level ``_run`` so tests stub one seam.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._version import is_newer, version_from_pyproject

logger = logging.getLogger("gateway.filament_fcm")

# UpdateOutcome.status values.
UPDATED = "updated"  # new version on disk — restart the gateway to load it
DISABLED = "disabled"  # pulled, but the security scan disabled the plugin
SKIPPED = "skipped"  # an eligibility gate said no — fall back to the reminder
FAILED = "failed"  # the update was attempted and didn't take — fall back

# How `hermes plugins update` announces a dangerous post-pull scan verdict
# (exit code stays 0): "Plugin '<name>' has been disabled. Review the
# findings, then re-enable with `hermes plugins enable <name>` ...". A string
# match is the only signal it offers; if the wording ever drifts we'd restart
# into a disabled plugin — a state only reachable when a release of this
# repo was rated dangerous, which the pre-merge scan check exists to prevent.
_DISABLED_MARKER = "has been disabled"

# `hermes plugins update` runs a git pull (60s internal timeout) plus a full
# security scan of the tree; give the whole command room to breathe.
_UPDATE_TIMEOUT = 600


@dataclass
class UpdateOutcome:
    status: str
    reason: str = ""
    disk_version: str | None = None
    # False when the tree already held the target version (a previous pull
    # whose restart never took) — nothing was pulled, only a restart is due.
    pulled: bool = False


def auto_update_disabled() -> bool:
    return os.environ.get("FILAMENT_DISABLE_AUTO_UPDATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def plugin_root() -> Path:
    """The plugin tree this code runs from (parent of the package dir)."""
    return Path(__file__).resolve().parent.parent


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def disk_version(root: Path) -> str | None:
    """The version currently on disk (may be newer than the running one)."""
    try:
        return version_from_pyproject((root / "pyproject.toml").read_text())
    except Exception:
        return None


def _run(args: list[str], cwd: Path | None = None, timeout: float = 60):
    """One seam for every subprocess this module runs (tests stub it)."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def eligibility_reason(root: Path, home: Path | None = None) -> str | None:
    """Why this tree must not be auto-updated, or None when it may be.

    The reasons are user-facing (appended to the fallback reminder), so they
    read as sentences, not error codes.
    """
    home = home or hermes_home()
    if not (root / ".git").exists():
        return "the plugin tree is not a git clone"
    plugins_dir = home / "plugins"
    try:
        installed = root.resolve().parent == plugins_dir.resolve()
    except OSError:
        installed = False
    if not installed:
        return f"the running code is not the directory plugin under {plugins_dir}"
    try:
        branch = _run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
        if branch.returncode != 0:
            return "git could not read the checkout"
        name = (branch.stdout or "").strip()
        if name != "main":
            return f"the checkout is on '{name}', not main"
        status = _run(["git", "-C", str(root), "status", "--porcelain"])
        if status.returncode != 0:
            return "git could not read the working tree"
        if (status.stdout or "").strip():
            return "the working tree has local changes"
    except FileNotFoundError:
        return "git is not installed"
    except Exception as exc:  # timeout, OSError, ...
        return f"git failed ({exc.__class__.__name__})"
    return None


def attempt_update(
    latest: str,
    current: str,
    root: Path | None = None,
    home: Path | None = None,
) -> UpdateOutcome:
    """Try to bring the plugin tree to ``latest``. Never raises.

    Blocking (subprocesses) — call it off the event loop. The caller owns
    all messaging and the restart; this only mutates the tree (and only via
    ``hermes plugins update``).
    """
    root = root or plugin_root()
    try:
        reason = eligibility_reason(root, home)
        if reason:
            return UpdateOutcome(SKIPPED, reason)

        on_disk = disk_version(root)
        if on_disk and not is_newer(latest, on_disk):
            # A previous cycle already pulled this (or newer) and the restart
            # never took — nothing to pull, the restart is the missing step.
            return UpdateOutcome(UPDATED, "already on disk", on_disk, pulled=False)

        proc = _run(
            ["hermes", "plugins", "update", root.name], timeout=_UPDATE_TIMEOUT
        )
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if proc.returncode != 0:
            tail = output.strip().splitlines()[-1] if output.strip() else "no output"
            return UpdateOutcome(
                FAILED, f"`hermes plugins update {root.name}` failed: {tail}"
            )
        if _DISABLED_MARKER in output:
            return UpdateOutcome(
                DISABLED,
                "the post-update security scan disabled the plugin",
                disk_version(root),
                pulled=True,
            )
        on_disk = disk_version(root)
        if not on_disk or is_newer(latest, on_disk):
            return UpdateOutcome(
                FAILED,
                f"the update completed but the tree holds "
                f"v{on_disk or 'unknown'}, not v{latest}",
            )
        return UpdateOutcome(UPDATED, "", on_disk, pulled=True)
    except FileNotFoundError:
        return UpdateOutcome(FAILED, "the hermes CLI is not on PATH")
    except Exception as exc:
        logger.warning("filament-fcm: auto-update attempt errored", exc_info=True)
        return UpdateOutcome(FAILED, f"unexpected error ({exc.__class__.__name__})")


def spawn_gateway_restart() -> bool:
    """Trigger a gateway restart from inside the gateway, detached.

    ``_HERMES_GATEWAY`` is scrubbed from the child's env so the CLI's
    inside-the-gateway guard doesn't refuse (Hermes core's own in-process
    restart does the same); every other variable is kept so the restart
    routes correctly (s6 / systemd / launchd / manual). Detached and not
    waited on: on unsupervised installs the command becomes the new
    foreground gateway and never exits, and this process is about to die.
    """
    env = dict(os.environ)
    env.pop("_HERMES_GATEWAY", None)
    try:
        subprocess.Popen(
            ["hermes", "gateway", "restart"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception:
        logger.warning(
            "filament-fcm: could not spawn `hermes gateway restart`", exc_info=True
        )
        return False
    return True
