"""FCM credential persistence.

Saves and loads Firebase Cloud Messaging registration credentials so the
plugin doesn't re-register with Google on every startup, and the
persistent ids of already-received pushes so Google MCS doesn't
redeliver them after a gateway restart.

Credentials are stored at $HERMES_HOME/filament-fcm/fcm_credentials.json
and received ids at $HERMES_HOME/filament-fcm/received_persistent_ids.json
(or the directory specified by FILAMENT_FCM_CREDENTIALS_DIR). Keying the
default off HERMES_HOME rather than $HOME matters for Hermes profiles: each
profile is its own HERMES_HOME, and each profile connected to Filament is a
distinct agent that needs its own FCM registration — two profiles sharing
one registration would each receive the other's pushes. Earlier versions
used ~/.hermes/filament-fcm unconditionally; default_state_dir() migrates
that directory forward for the root profile so an upgraded agent keeps its
FCM identity, standing instructions, and dedup state.

Note: The MCP token is NOT persisted here — it is provided by the user
via the FILAMENT_MCP_TOKEN environment variable and can be rotated
independently. See README.md for how to generate one.
"""

import json
import logging
import os
from pathlib import Path
from secrets import token_hex
from typing import Any

logger = logging.getLogger("gateway.filament_fcm")

_STATE_DIR_NAME = "filament-fcm"

# Cap on how many received persistent ids we keep. MCS only redelivers
# recent unacked messages, so a bounded tail is plenty; this just keeps
# the file (and the login payload built from it) from growing forever.
MAX_RECEIVED_PERSISTENT_IDS = 1000


def default_state_dir() -> Path:
    """Resolve (and, once, migrate) the plugin's state directory.

    ``FILAMENT_FCM_CREDENTIALS_DIR`` wins when set. Otherwise the directory is
    ``$HERMES_HOME/filament-fcm`` — per Hermes profile, since every profile is
    its own HERMES_HOME — falling back to ``~/.hermes/filament-fcm`` when
    HERMES_HOME is unset (hermes's own default home, so the path is unchanged
    for plain installs).

    Migration: earlier versions always used ``~/.hermes/filament-fcm``. When
    the resolved directory doesn't exist yet but that legacy one does, it is
    renamed into place so the agent keeps its FCM identity across the upgrade —
    but only for the *root* profile. A named profile (a HERMES_HOME under
    ``profiles/``) is a different agent: it must register fresh, never adopt
    the root profile's identity. If the rename fails (permissions, cross-
    device), stay on the legacy path rather than orphan a working identity.

    ``reactive._default_dir`` mirrors the resolution rules, including the
    prefer-legacy-when-unmigrated fallback (but not the migration itself,
    which this module owns and runs first at gateway start via the adapter's
    CredentialStore) — keep them in sync.
    """
    override = os.environ.get("FILAMENT_FCM_CREDENTIALS_DIR")
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME")
    hermes_home = Path(home) if home else Path.home() / ".hermes"
    state_dir = hermes_home / _STATE_DIR_NAME
    legacy = Path.home() / ".hermes" / _STATE_DIR_NAME
    if state_dir == legacy or state_dir.exists() or not legacy.exists():
        return state_dir
    if hermes_home.parent.name == "profiles":
        return state_dir
    marker = legacy.with_name(_STATE_DIR_NAME + ".moved")
    try:
        state_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, state_dir)
        # A one-way door: warn (not info), and leave a breadcrumb next to
        # where the directory was for anyone later debugging the old agent.
        logger.warning("Migrated filament-fcm state from %s to %s", legacy, state_dir)
        try:
            marker.write_text(
                f"filament-fcm state moved to: {state_dir}\n", encoding="utf-8"
            )
        except OSError:
            logger.debug("Could not write migration marker %s", marker, exc_info=True)
    except OSError:
        logger.warning(
            "Could not migrate filament-fcm state from %s to %s; "
            "continuing on the legacy path",
            legacy,
            state_dir,
            exc_info=True,
        )
        return legacy
    return state_dir


class CredentialStore:
    """Manages persisted FCM credentials for the filament-fcm plugin."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._dir = Path(base_dir) if base_dir else default_state_dir()

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, filename: str) -> dict[str, Any] | None:
        path = self._dir / filename
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to read %s", path, exc_info=True)
            return None

    def _write_json(self, filename: str, data: dict[str, Any]) -> None:
        self._ensure_dir()
        path = self._dir / filename
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Wrote %s", path)
        except Exception:
            logger.warning("Failed to write %s", path, exc_info=True)

    def load_fcm_credentials(self) -> dict[str, Any] | None:
        """Load saved FCM registration credentials."""
        return self._read_json("fcm_credentials.json")

    def save_fcm_credentials(self, creds: dict[str, Any]) -> None:
        """Persist FCM registration credentials."""
        self._write_json("fcm_credentials.json", creds)

    def load_update_notice(self) -> dict[str, Any] | None:
        """Load the update-reminder state (last version the principal was
        told about) — see update_check.py."""
        return self._read_json("update_notice.json")

    def save_update_notice(self, data: dict[str, Any]) -> None:
        """Persist the update-reminder state."""
        self._write_json("update_notice.json", data)

    def load_pending_upgrade(self) -> dict[str, Any] | None:
        """Load the in-flight upgrade marker — see self_update.py."""
        return self._read_json("pending_upgrade.json")

    def save_pending_upgrade(self, data: dict[str, Any]) -> None:
        """Persist the in-flight upgrade marker.

        Written before the gateway restarts and read by the process that
        comes back, which is the only way the "upgrade complete" message can
        be sent by a process that knows the upgrade happened.
        """
        self._write_json("pending_upgrade.json", data)

    def clear_pending_upgrade(self) -> None:
        """Drop the marker once the upgrade result has been announced.

        Missing file is success: the marker must never outlive the one
        restart it describes, or the agent re-announces on every start.
        """
        path = self._dir / "pending_upgrade.json"
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed to remove %s", path, exc_info=True)

    def load_or_create_installation_id(self) -> str:
        """Return a stable, report-safe id for this Hermes plugin install."""
        data = self._read_json("installation.json")
        if isinstance(data, dict):
            installation_id = data.get("installation_id")
            if isinstance(installation_id, str) and installation_id:
                return installation_id
        installation_id = f"inst_{token_hex(5)}"
        self._write_json("installation.json", {"installation_id": installation_id})
        return installation_id

    def load_received_persistent_ids(self) -> list[str]:
        """Load the persistent ids of pushes we've already received."""
        data = self._read_json("received_persistent_ids.json")
        if not isinstance(data, dict):
            return []
        ids = data.get("ids")
        if not isinstance(ids, list):
            return []
        return [i for i in ids if isinstance(i, str)]

    def save_received_persistent_ids(self, ids: list[str]) -> None:
        """Persist the received-push persistent ids (bounded tail)."""
        self._write_json(
            "received_persistent_ids.json",
            {"ids": ids[-MAX_RECEIVED_PERSISTENT_IDS:]},
        )


class ReceivedPersistentIds:
    """Tracks which FCM pushes have already been received, across restarts.

    Google MCS redelivers any push it hasn't seen acknowledged. If the
    gateway exits before the ack flushes (e.g. a ``/restart`` command kills
    the process mid-handling), the same push arrives again on the next
    connect — and a redelivered ``/restart`` restarts the gateway in an
    infinite loop. Two defenses, both fed from this store:

    - ``ids`` is passed to ``FcmPushClient(received_persistent_ids=...)``
      so the MCS login tells Google not to redeliver them.
    - ``record()`` gates dispatch, dropping any redelivery that slips
      through anyway (the library does no callback-level dedup).

    ``record()`` persists *before* the message is dispatched, so the id is
    durable even when handling the message kills the process.
    """

    def __init__(
        self, store: CredentialStore, max_ids: int = MAX_RECEIVED_PERSISTENT_IDS
    ) -> None:
        self._store = store
        self._max = max_ids
        self._ids = store.load_received_persistent_ids()[-max_ids:]
        self._seen = set(self._ids)

    @property
    def ids(self) -> list[str]:
        """The received ids, oldest first."""
        return list(self._ids)

    def record(self, persistent_id: str | None) -> bool:
        """Record *persistent_id*; return True if it's new (safe to dispatch).

        Returns False for an already-seen id (a redelivery — skip it).
        Ids that are empty/None can't be deduped and are treated as new
        without being recorded.
        """
        if not persistent_id:
            return True
        if persistent_id in self._seen:
            return False
        self._ids.append(persistent_id)
        self._seen.add(persistent_id)
        if len(self._ids) > self._max:
            dropped = self._ids[: -self._max]
            self._ids = self._ids[-self._max :]
            self._seen.difference_update(dropped)
        self._store.save_received_persistent_ids(self._ids)
        return True
