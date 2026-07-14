"""Tests for vouch (knock-invite) handling in ``fcm_client.py``.

A member vouching the agent into a loop arrives as a ``knock_invite_received``
push — distinct from an ``add_to_*`` invite. These tests verify the push is
parsed into a ``VouchMessage`` and dispatched to the ``on_vouch`` callback, so
the adapter can accept it (turning the vouch into a member proposal a loop admin
approves). Loaded standalone like ``test_fcm_receiver_death.py`` — importing the
package would pull in Hermes, which isn't present in a bare test environment.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


# ── firebase_messaging stub ─────────────────────────────────────────


class _StubRegisterConfig:
    def __init__(self, *args, **kwargs):
        self.args = args


class _StubPushClient:
    def __init__(self, callback=None, **kwargs):
        self.tasks = []


def _load_fcm_client_module():
    stub = types.ModuleType("firebase_messaging")
    stub.FcmPushClient = _StubPushClient
    stub.FcmRegisterConfig = _StubRegisterConfig
    sys.modules["firebase_messaging"] = stub

    pkg = types.ModuleType("hermes_filament_fcm")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament_fcm"] = pkg

    for name in ("credentials", "fcm_client"):
        spec = importlib.util.spec_from_file_location(
            f"hermes_filament_fcm.{name}", _PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"hermes_filament_fcm.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["hermes_filament_fcm.fcm_client"]


fcm_client = _load_fcm_client_module()


class _MemoryCredentials:
    def load_fcm_credentials(self):
        return {"stub": True}

    def save_fcm_credentials(self, creds):
        pass

    def load_received_persistent_ids(self):
        return []

    def save_received_persistent_ids(self, ids):
        pass


def _make_client(**kwargs):
    return fcm_client.FilamentFCMClient(
        config=fcm_client.FCMConfig(
            project_id="p", app_id="a", api_key="k", sender_id="s"
        ),
        on_message=lambda msg: None,
        credentials=_MemoryCredentials(),
        **kwargs,
    )


def _vouch_push(**branch_overrides):
    """An FCM data message carrying a ``knock_invite_received`` payload."""
    branch = {
        "type": "knock_invite_received",
        "inviter": "Alice",
        "inviter_id": "@alice:example.org",
        "inviter_avatar_url": None,
        "loop": "Acme Loop",
        "loop_id": "!loop:example.org",
        "loop_avatar_url": None,
        "token": "TOK123",
    }
    branch.update(branch_overrides)
    payload = {"branch": branch, "room_id": "!loop:example.org"}
    return {"data": {"body": json.dumps(payload)}}


# ── Tests ───────────────────────────────────────────────────────────


def test_build_vouch_message_from_envelope():
    env = fcm_client.parse_envelope(_vouch_push()["data"])
    assert env is not None
    vouch = fcm_client._build_vouch_message(env)
    assert vouch is not None
    assert vouch.loop_id == "!loop:example.org"
    assert vouch.inviter == "Alice"
    assert vouch.inviter_id == "@alice:example.org"
    assert vouch.loop_name == "Acme Loop"


def test_build_vouch_message_needs_branch():
    env = fcm_client.Envelope(payload={}, branch=None, branch_type="")
    assert fcm_client._build_vouch_message(env) is None


def test_knock_invite_dispatches_to_on_vouch():
    received = []
    client = _make_client(on_vouch=received.append)
    client._handle_notification(_vouch_push(), "pid-vouch-1")
    assert len(received) == 1
    assert isinstance(received[0], fcm_client.VouchMessage)
    assert received[0].loop_id == "!loop:example.org"
    assert received[0].inviter == "Alice"


def test_vouch_not_delivered_as_invite():
    """A vouch must not fire the invite path — that would JOIN directly,
    consuming the vouch so the loop admin never sees a proposal."""
    invites = []
    vouches = []
    client = _make_client(on_invite=invites.append, on_vouch=vouches.append)
    client._handle_notification(_vouch_push(), "pid-vouch-2")
    assert invites == []
    assert len(vouches) == 1
