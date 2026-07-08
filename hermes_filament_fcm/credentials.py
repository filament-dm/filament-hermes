"""FCM credential persistence.

Saves and loads Firebase Cloud Messaging registration credentials so the
plugin doesn't re-register with Google on every startup.

Credentials are stored at ~/.hermes/filament-fcm/fcm_credentials.json
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

    def load_seen_persistent_ids(self) -> list[str]:
        """Load the recently-processed FCM ``persistent_id`` list.

        Persisted so message-level idempotency survives a gateway restart (an
        MCS redelivery arrives in a fresh process). Returns oldest-first.
        """
        data = self._read_json("seen_persistent_ids.json")
        if isinstance(data, dict):
            ids = data.get("ids")
            if isinstance(ids, list):
                return [str(x) for x in ids]
        return []

    def save_seen_persistent_ids(self, ids: list[str]) -> None:
        """Persist the recently-processed FCM ``persistent_id`` list."""
        self._write_json("seen_persistent_ids.json", {"ids": list(ids)})
