"""Update check: detection state and the backchannel notes.

The plugin is installed from git main (`hermes plugins install` clones the
default branch), so "latest available version" means the version currently on
main. Once a day the
adapter fetches the raw pyproject.toml from GitHub and compares it to the
installed version. When a newer one exists the adapter first tries to apply
it in place (see ``self_update.py``); when that isn't possible it falls back
to reminding the principal, once per new version, via the backchannel (every
check still logs, so operators watching logs see it too). This module owns
the detection, the persisted state, and every backchannel note either path
sends.

Set FILAMENT_DISABLE_UPDATE_CHECK=true to turn the whole thing off (e.g.
air-gapped deployments, or devs running a checkout);
FILAMENT_DISABLE_AUTO_UPDATE=true keeps the check and reminders but never
auto-updates.

The "already reminded" marker — and the "auto-update pulled a version the
security scan then disabled" blocker — are persisted next to the FCM
credentials (update_notice.json in the CredentialStore directory) so gateway
restarts don't re-nag or re-pull.
"""

import logging
import os
import time

import httpx

from ._version import (
    LATEST_PYPROJECT_URL,
    PLUGIN_VERSION,
    REPO_URL,
    USER_AGENT,
    is_newer,
    version_from_pyproject,
)
from .credentials import CredentialStore

logger = logging.getLogger("gateway.filament_fcm")

CHECK_INTERVAL_SECONDS = 24 * 60 * 60


def update_check_disabled() -> bool:
    return os.environ.get("FILAMENT_DISABLE_UPDATE_CHECK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def build_reminder(latest: str, current: str, reason: str | None = None) -> str:
    """The small backchannel note shown to the principal.

    ``reason`` is why the plugin couldn't update itself (auto-update is the
    default path; this reminder is its fallback) — user-facing prose from
    ``self_update.eligibility_reason`` / ``UpdateOutcome.reason``.
    """
    note = (
        f"📦 A new version of the Filament↔Hermes plugin is available: "
        f"v{latest} (this agent runs v{current}). To update, run on the "
        f"machine hosting this agent:\n"
        f"```\nhermes plugins update filament && hermes gateway restart\n```\n"
        f"That pulls the plugin's vendored dependencies along with its code, so "
        f"it is the whole update. If it reports any problem, re-run the connect "
        f"command from the Filament app instead — it replaces the plugin outright "
        f"rather than updating it in place, so it recovers from any state."
    )
    if reason:
        note += f"\n\n(I tried to update myself, but couldn't: {reason}.)"
    return note


def build_updated_note(latest: str, current: str) -> str:
    """Posted right before the restart that loads an auto-applied update."""
    return (
        f"📦 I updated the Filament↔Hermes plugin to v{latest} (this agent was "
        f"running v{current}) and I'm restarting the gateway now to load it — "
        f"back in a moment. If I go quiet instead, restart manually on the "
        f"machine hosting this agent:\n```\nhermes gateway restart\n```"
    )


def build_update_disabled_note(latest: str, plugin_id: str) -> str:
    """Posted when the post-update security scan disabled the plugin.

    The one auto-update outcome that must NOT restart: the plugin is out of
    Hermes's enabled set, so a restart would load nothing and take the agent
    dark. The running (pre-update) code keeps working until the next gateway
    restart, so the note asks the principal to resolve it before then.
    """
    return (
        f"⚠️ I pulled Filament↔Hermes plugin v{latest}, but Hermes disabled the "
        f"plugin after its post-update security scan, so I am NOT restarting — "
        f"the version already running keeps working until the next gateway "
        f"restart. On the machine hosting this agent, review the scan findings, "
        f"then re-enable and restart:\n"
        f"```\nhermes plugins enable {plugin_id} && hermes gateway restart\n```"
    )


async def fetch_latest_version(timeout: float = 10.0) -> str | None:
    """Version on main, or None on any failure (network, parse, ...).

    The pyproject.toml URL is ``FILAMENT_UPDATE_CHECK_URL`` when that env var
    is set, else the GitHub raw URL for main. The override exists so a test
    harness can stand in for "the version on main" and observe the reminder
    end to end; production deployments never set it and always hit GitHub.
    Read per call (not at import time) so tests and harnesses can set it
    after the module is loaded.
    """
    url = os.environ.get("FILAMENT_UPDATE_CHECK_URL") or LATEST_PYPROJECT_URL
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug("filament-fcm: update check got HTTP %d", resp.status_code)
                return None
            return version_from_pyproject(resp.text)
    except Exception:
        logger.debug("filament-fcm: update check fetch failed", exc_info=True)
        return None


class UpdateChecker:
    """Decides whether an update is due, and remembers what's been handled.

    ``check()`` returns the newer version string when the caller should act
    (auto-update or remind), else None. The caller then calls
    ``mark_notified()`` once the principal has been told — only successful
    delivery is recorded, so a failed post retries on the next daily check.
    ``mark_blocked``/``blocked_version``/``reconcile_blocked`` track the one
    poisonous auto-update outcome (pulled, then disabled by the security
    scan) across restarts.
    """

    def __init__(
        self,
        store: CredentialStore | None = None,
        current_version: str = PLUGIN_VERSION,
    ) -> None:
        self._store = store or CredentialStore()
        self._current = current_version

    async def check(self) -> str | None:
        latest = await fetch_latest_version()
        if not latest or not is_newer(latest, self._current):
            logger.debug(
                "filament-fcm: update check — running v%s, latest v%s",
                self._current,
                latest,
            )
            return None
        # Always log (operator-visible), remind at most once per version.
        logger.warning(
            "filament-fcm: plugin update available — v%s is out, this agent "
            "runs v%s (%s)",
            latest,
            self._current,
            REPO_URL,
        )
        if self._state().get("notified_version") == latest:
            return None
        return latest

    def _state(self) -> dict:
        # isinstance, not truthiness: a corrupted update_notice.json can hold
        # any JSON value (list, string, ...) — calling .get on it would raise
        # and silently kill the reminder until the file is removed. A non-dict
        # is treated as "never notified", so the reminder self-heals the file
        # on the next successful delivery.
        state = self._store.load_update_notice()
        return state if isinstance(state, dict) else {}

    def _merge_state(self, **updates) -> None:
        # Merge, don't replace: the notified marker and the blocked marker
        # share the file and must not clobber each other. A None value
        # removes its key.
        state = self._state()
        for key, value in updates.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        self._store.save_update_notice(state)

    def mark_notified(self, version: str) -> None:
        self._merge_state(
            notified_version=version, notified_ms=int(time.time() * 1000)
        )

    def mark_blocked(self, version: str) -> None:
        """Record that auto-update pulled ``version`` and the security scan
        then disabled the plugin — the tree must not be restarted into or
        auto-updated again until a human re-enables the plugin."""
        self._merge_state(blocked_version=version)

    def blocked_version(self) -> str | None:
        value = self._state().get("blocked_version")
        return value if isinstance(value, str) else None

    def reconcile_blocked(self, running_version: str) -> None:
        """Clear a stale blocked marker.

        Running the blocked version means a human re-enabled the plugin and
        restarted the gateway — the block resolved itself, so auto-update may
        resume. Called once when the update loop starts.
        """
        if self.blocked_version() == running_version:
            self._merge_state(blocked_version=None)
