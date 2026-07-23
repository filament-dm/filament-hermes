"""Update-available reminder.

The plugin is installed from git main (see install.sh), so "latest
available version" means the version currently on main. Once a day the
adapter fetches the raw pyproject.toml from GitHub, compares it to the
installed version, and — when a newer one exists — logs a warning. The
reminder is log-only for now (there is no backchannel post), so operators
watching logs are the audience.

Set FILAMENT_DISABLE_UPDATE_CHECK=true to turn the whole thing off (e.g.
air-gapped deployments, or devs running a checkout).

The "already reminded" marker is persisted next to the FCM credentials
(update_notice.json in the CredentialStore directory) so gateway restarts
don't re-nag.
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


async def fetch_latest_version(timeout: float = 10.0) -> str | None:
    """Version on main, or None on any failure (network, parse, ...)."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(LATEST_PYPROJECT_URL)
            if resp.status_code != 200:
                logger.debug("filament-fcm: update check got HTTP %d", resp.status_code)
                return None
            return version_from_pyproject(resp.text)
    except Exception:
        logger.debug("filament-fcm: update check fetch failed", exc_info=True)
        return None


class UpdateChecker:
    """Decides whether an update reminder is due.

    ``check()`` returns the newer version string when the principal should
    be reminded, else None. The caller delivers the reminder and then calls
    ``mark_notified()`` — only successful delivery is recorded, so a failed
    post retries on the next daily check.
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
        # isinstance, not truthiness: a corrupted update_notice.json can hold
        # any JSON value (list, string, ...) — calling .get on it would raise
        # and silently kill the reminder until the file is removed. A non-dict
        # is treated as "never notified", so the reminder self-heals the file
        # on the next successful delivery.
        state = self._store.load_update_notice()
        if isinstance(state, dict) and state.get("notified_version") == latest:
            return None
        return latest

    def mark_notified(self, version: str) -> None:
        self._store.save_update_notice(
            {"notified_version": version, "notified_ms": int(time.time() * 1000)}
        )
