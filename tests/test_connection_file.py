"""The profile records which Filament agent it is, for the Filament desktop
app's list of Hermes profiles: never the token itself, only its hash.

``credentials.py`` is pure-stdlib, so it is loaded standalone.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "credentials.py"
)


def _credentials():
    spec = importlib.util.spec_from_file_location("credentials_conn", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connection_names_the_agent_and_hashes_the_token(tmp_path):
    credentials = _credentials()
    token = "fmcp_secret_token_value"
    digest = hashlib.sha256(token.encode()).hexdigest()
    store = credentials.CredentialStore(str(tmp_path))
    store.save_connection(
        "@a_1:filament.dm", "https://api.filament.dm/mcp/agents", digest
    )

    text = (tmp_path / credentials.CONNECTION_FILE).read_text()
    assert token not in text
    assert json.loads(text) == {
        "agent_id": "@a_1:filament.dm",
        "mcp_url": "https://api.filament.dm/mcp/agents",
        "token_sha256": digest,
    }
