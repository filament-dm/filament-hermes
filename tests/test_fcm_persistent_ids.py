"""Tests for received-persistent-id persistence and redelivery dedup.

These guard against the /restart redelivery loop: Google MCS re-pushes any
message whose ack never flushed, so ids must survive a gateway restart
(seeding the next MCS login) and redeliveries must be dropped before
dispatch.

``credentials.py`` is pure-stdlib, so we load it standalone — importing the
package triggers ``__init__`` → the Hermes ``gateway`` package, which isn't
present in a bare test environment.
"""

import importlib.util
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "credentials",
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "credentials.py",
)
credentials = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(credentials)


def test_store_roundtrip_and_missing_file():
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        # Missing file → empty list, no error.
        assert store.load_received_persistent_ids() == []
        store.save_received_persistent_ids(["0:aaa", "0:bbb"])
        assert store.load_received_persistent_ids() == ["0:aaa", "0:bbb"]


def test_store_tolerates_corrupt_file():
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        (Path(d) / "received_persistent_ids.json").write_text("{not json")
        assert store.load_received_persistent_ids() == []
        # Wrong shape (list at top level, non-string entries) → sanitized.
        (Path(d) / "received_persistent_ids.json").write_text('["0:aaa"]')
        assert store.load_received_persistent_ids() == []
        (Path(d) / "received_persistent_ids.json").write_text(
            '{"ids": ["0:aaa", 7, null, "0:bbb"]}'
        )
        assert store.load_received_persistent_ids() == ["0:aaa", "0:bbb"]


def test_store_save_bounds_to_cap():
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        cap = credentials.MAX_RECEIVED_PERSISTENT_IDS
        ids = [f"0:{i}" for i in range(cap + 50)]
        store.save_received_persistent_ids(ids)
        loaded = store.load_received_persistent_ids()
        # Oldest dropped, newest kept.
        assert len(loaded) == cap
        assert loaded[0] == "0:50"
        assert loaded[-1] == f"0:{cap + 49}"


def test_installation_id_persists_across_store_instances():
    with tempfile.TemporaryDirectory() as d:
        first = credentials.CredentialStore(d).load_or_create_installation_id()
        second = credentials.CredentialStore(d).load_or_create_installation_id()

        assert first.startswith("inst_")
        assert second == first


def test_record_new_then_duplicate():
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        tracker = credentials.ReceivedPersistentIds(store)
        # Fresh id → dispatch, and it's persisted immediately.
        assert tracker.record("0:restart") is True
        assert store.load_received_persistent_ids() == ["0:restart"]
        # Same id again (a redelivery) → skip.
        assert tracker.record("0:restart") is False
        # Empty/None can't be deduped → dispatch, never recorded.
        assert tracker.record("") is True
        assert tracker.record(None) is True
        assert store.load_received_persistent_ids() == ["0:restart"]


def test_redelivery_dropped_across_restart():
    """The restart scenario in miniature: a new tracker built from the same
    store (= a new gateway process) must seed the MCS login with the old
    ids AND refuse to re-dispatch a redelivered one."""
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        first = credentials.ReceivedPersistentIds(store)
        assert first.record("0:restart-cmd") is True

        # "Restart": fresh process, fresh tracker, same on-disk store.
        second = credentials.ReceivedPersistentIds(credentials.CredentialStore(d))
        assert "0:restart-cmd" in second.ids  # seeds the MCS login
        assert second.record("0:restart-cmd") is False  # redelivery dropped
        assert second.record("0:genuinely-new") is True


def test_tracker_bounds_in_memory():
    with tempfile.TemporaryDirectory() as d:
        store = credentials.CredentialStore(d)
        tracker = credentials.ReceivedPersistentIds(store, max_ids=3)
        for i in range(5):
            assert tracker.record(f"0:{i}") is True
        assert tracker.ids == ["0:2", "0:3", "0:4"]
        # An evicted id is no longer deduped (matches what MCS could
        # theoretically redeliver far outside the window; acceptable).
        assert tracker.record("0:0") is True
