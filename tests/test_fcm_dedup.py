"""Tests for cross-restart FCM redelivery dedup.

``fcm_client`` imports ``firebase_messaging`` and uses a relative import, so we
stub the former and load the package modules under a lightweight ``hfcm``
package — this avoids ``hermes_filament_fcm/__init__`` (which pulls in the
Hermes gateway) while still resolving ``from .credentials import ...``.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"

# Stub the external dep so fcm_client imports without firebase-messaging present.
_fb = types.ModuleType("firebase_messaging")
_fb.FcmPushClient = object
_fb.FcmRegisterConfig = object
sys.modules.setdefault("firebase_messaging", _fb)

# A stand-in package rooted at the real source dir so relative imports resolve
# without executing the real __init__.
_pkg = types.ModuleType("hfcm")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["hfcm"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"hfcm.{name}", _PKG_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"hfcm.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


credentials = _load("credentials")
fcm_client = _load("fcm_client")

CredentialStore = credentials.CredentialStore
FilamentFCMClient = fcm_client.FilamentFCMClient

# Minimal well-formed FCM data message that routes to _dispatch_message.
CHANNEL_MSG = {"data": {"body": json.dumps({"branch": {"type": "channel_message"}})}}


def _make_client(tmp_dir):
    """A client wired to a real (tmp-dir) credential store, with the message
    dispatch stubbed to a counter so we can assert what actually got processed."""
    store = CredentialStore(str(tmp_dir))
    calls = []
    client = FilamentFCMClient(
        config=object(), on_message=lambda m: None, credentials=store
    )
    client._dispatch_message = calls.append
    return client, calls


def test_seen_persistent_ids_roundtrip(tmp_path):
    store = CredentialStore(str(tmp_path))
    assert store.load_seen_persistent_ids() == []  # nothing persisted yet
    store.save_seen_persistent_ids(["a", "b", "c"])
    assert store.load_seen_persistent_ids() == ["a", "b", "c"]
    # A second store over the same dir sees them (i.e. across a restart).
    assert CredentialStore(str(tmp_path)).load_seen_persistent_ids() == ["a", "b", "c"]


def test_redelivery_within_a_run_is_dropped(tmp_path):
    client, calls = _make_client(tmp_path)
    client._handle_notification(CHANNEL_MSG, "pid-1")
    client._handle_notification(CHANNEL_MSG, "pid-1")  # same MCS persistent_id
    assert len(calls) == 1


def test_dedup_survives_restart(tmp_path):
    # First process handles pid-1.
    client1, calls1 = _make_client(tmp_path)
    client1._handle_notification(CHANNEL_MSG, "pid-1")
    assert len(calls1) == 1

    # Process restarts (fresh client, same on-disk store) and MCS redelivers
    # pid-1 — the /restart loop hinges on this being dropped.
    client2, calls2 = _make_client(tmp_path)
    client2._handle_notification(CHANNEL_MSG, "pid-1")
    assert len(calls2) == 0

    # A genuinely new message still gets through after the restart.
    client2._handle_notification(CHANNEL_MSG, "pid-2")
    assert len(calls2) == 1


def test_missing_persistent_id_is_not_deduped(tmp_path):
    # No id to key on → never treated as a duplicate (process every time).
    client, calls = _make_client(tmp_path)
    client._handle_notification(CHANNEL_MSG, "")
    client._handle_notification(CHANNEL_MSG, "")
    assert len(calls) == 2


def test_window_is_bounded(tmp_path):
    client, calls = _make_client(tmp_path)
    n = FilamentFCMClient._MAX_SEEN_PERSISTENT_IDS
    for i in range(n + 5):
        client._handle_notification(CHANNEL_MSG, f"pid-{i}")
    assert len(calls) == n + 5
    # pid-0 aged out of the bounded window, so a (late) redelivery re-processes.
    client._handle_notification(CHANNEL_MSG, "pid-0")
    assert len(calls) == n + 6
