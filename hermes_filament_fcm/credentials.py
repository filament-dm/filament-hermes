"""FCM credential persistence.

Saves and loads Firebase Cloud Messaging registration credentials so the
plugin doesn't re-register with Google on every startup, and the
persistent ids of already-received pushes so Google MCS doesn't
redeliver them after a gateway restart.

Credentials are stored at ~/.hermes/filament-fcm/fcm_credentials.json
and received ids at ~/.hermes/filament-fcm/received_persistent_ids.json
(or the directory specified by FILAMENT_FCM_CREDENTIALS_DIR).

Note: The MCP token is NOT persisted here — it is provided by the user
via the FILAMENT_MCP_TOKEN environment variable and can be rotated
independently. See README.md for how to generate one.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("gateway.filament_fcm")

_DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".hermes", "filament-fcm")

# Cap on how many received persistent ids we keep. MCS only redelivers
# recent unacked messages, so a bounded tail is plenty; this just keeps
# the file (and the login payload built from it) from growing forever.
MAX_RECEIVED_PERSISTENT_IDS = 1000


class CredentialStore:
    """Manages persisted FCM credentials for the filament-fcm plugin."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._dir = Path(
            base_dir or os.environ.get("FILAMENT_FCM_CREDENTIALS_DIR", _DEFAULT_DIR)
        )

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
