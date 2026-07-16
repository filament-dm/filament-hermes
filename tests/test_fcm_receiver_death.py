"""Tests for FCM receiver death detection in ``fcm_client.py``.

Like ``test_reactive.py``, the module is loaded standalone — importing the
package triggers ``__init__`` → the Hermes ``gateway`` package, which isn't
present in a bare test environment. ``firebase_messaging`` is stubbed out
before the load so the tests exercise only our detection logic.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


# ── firebase_messaging stub ─────────────────────────────────────────


class _StubRegisterConfig:
    def __init__(self, *args, **kwargs):
        self.args = args


class _StubPushClient:
    """Stands in for firebase_messaging.FcmPushClient.

    ``start()`` spawns two forever-pending internal tasks, mirroring the
    library's ``_listen`` / ``_do_monitor`` pair.
    """

    def __init__(self, callback=None, **kwargs):
        self.tasks: list[asyncio.Task] = []
        self._gates: list[asyncio.Event] = []

    async def checkin_or_register(self):
        return "token-1"

    async def start(self):
        self._gates = [asyncio.Event(), asyncio.Event()]
        self.tasks = [asyncio.ensure_future(g.wait()) for g in self._gates]

    def end_task(self, index: int) -> None:
        """Simulate one internal task finishing (e.g. _listen returning
        after connect-retry exhaustion, which never cancels the monitor)."""
        self._gates[index].set()

    def terminate(self) -> None:
        """Simulate the library's _terminate(): cancel all internal tasks."""
        for task in self.tasks:
            if not task.done():
                task.cancel()


# The plugin imports firebase_messaging lazily (inside checkin_or_register), so
# the stub must be the active sys.modules entry when a test RUNS, not just when
# this module is imported. Other standalone test modules overwrite
# sys.modules["firebase_messaging"] during collection, so keep a reference and
# reinstall it per test via the autouse fixture below.
_FIREBASE_STUB = types.ModuleType("firebase_messaging")
_FIREBASE_STUB.FcmPushClient = _StubPushClient
_FIREBASE_STUB.FcmRegisterConfig = _StubRegisterConfig


@pytest.fixture(autouse=True)
def _use_firebase_stub():
    prev = sys.modules.get("firebase_messaging")
    sys.modules["firebase_messaging"] = _FIREBASE_STUB
    yield
    if prev is not None:
        sys.modules["firebase_messaging"] = prev


def _load_fcm_client_module():
    sys.modules["firebase_messaging"] = _FIREBASE_STUB

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


async def _started_client(**kwargs):
    client = _make_client(**kwargs)
    await client.checkin_or_register()
    await client.start()
    return client


async def _settle():
    """Let pending done-callbacks run."""
    for _ in range(3):
        await asyncio.sleep(0)


# ── Tests ───────────────────────────────────────────────────────────


def test_single_task_exit_reports_death():
    """_listen returning alone (connect-retry exhaustion) must be fatal —
    the monitor task keeps running in that state, so waiting for ALL tasks
    would miss it."""

    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)
        client._push_client.end_task(0)
        await _settle()
        assert len(deaths) == 1
        assert "exited" in deaths[0]

    asyncio.run(scenario())


def test_terminate_reports_death_once():
    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)
        client._push_client.terminate()
        await _settle()
        assert len(deaths) == 1  # both tasks ended, one report
        assert "cancelled" in deaths[0]

    asyncio.run(scenario())


def test_stop_suppresses_death_report():
    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)
        await client.stop()
        await _settle()
        assert deaths == []

    asyncio.run(scenario())


def test_crashed_task_reports_exception_detail():
    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)

        async def _boom():
            raise RuntimeError("kaput")

        # A watched task crashing must surface the exception in the detail.
        crash_task = asyncio.ensure_future(_boom())
        crash_task.add_done_callback(client._on_push_task_done)
        await _settle()
        assert len(deaths) == 1
        assert "kaput" in deaths[0]
        await client.stop()

    asyncio.run(scenario())


def test_death_reported_only_once_across_tasks():
    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)
        client._push_client.end_task(0)
        await _settle()
        client._push_client.end_task(1)
        await _settle()
        assert len(deaths) == 1

    asyncio.run(scenario())


def test_no_callback_is_safe():
    async def scenario():
        client = await _started_client()  # on_receiver_dead omitted
        client._push_client.terminate()
        await _settle()  # must not raise

    asyncio.run(scenario())


def test_restart_after_stop_rearms_death_detection():
    async def scenario():
        deaths: list[str] = []
        client = await _started_client(on_receiver_dead=deaths.append)
        await client.stop()
        await _settle()
        assert deaths == []

        # Restarting the same client must detect deaths again.
        await client.checkin_or_register()
        await client.start()
        client._push_client.terminate()
        await _settle()
        assert len(deaths) == 1
        await client.stop()

    asyncio.run(scenario())
