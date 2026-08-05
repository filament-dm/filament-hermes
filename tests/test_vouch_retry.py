"""accept_vouch must ride out blips but never argue with a rejection.

A vouch that fails to be accepted leaves no member proposal, so the loop admin
has nothing to approve — to them it looks like the vouch never happened, and
before this the only recovery was restarting the gateway. So a transient failure
is retried. A server *rejection* (knock 403 -> -32602) is a decision, not a
blip: retrying it repeats a settled answer and buries the real reason in noise.

Modules are loaded standalone with the Hermes gateway stubbed, same pattern as
``test_system_notice_skip.py``.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _install_stubs() -> None:
    fb = types.ModuleType("firebase_messaging")
    fb.FcmPushClient = type("FcmPushClient", (), {})
    fb.FcmRegisterConfig = type("FcmRegisterConfig", (), {})
    sys.modules["firebase_messaging"] = fb

    agent_pkg = types.ModuleType("agent")
    async_utils = types.ModuleType("agent.async_utils")
    async_utils.safe_schedule_threadsafe = lambda coro, loop, log_message="": None
    agent_pkg.async_utils = async_utils
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.async_utils"] = async_utils

    gateway_pkg = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = lambda name: name
    platforms_pkg = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _BaseAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

        def build_source(self, **kwargs):
            return kwargs

        async def handle_message(self, event):
            pass

        def _set_fatal_error(self, *args, **kwargs):
            pass

        def _mark_connected(self):
            pass

        def _mark_disconnected(self):
            pass

    base_mod.BasePlatformAdapter = _BaseAdapter
    base_mod.MessageEvent = type("MessageEvent", (), {})
    base_mod.MessageType = types.SimpleNamespace(TEXT="text")
    base_mod.ProcessingOutcome = type("ProcessingOutcome", (), {})
    base_mod.SendResult = type("SendResult", (), {})

    gateway_pkg.config = config_mod
    gateway_pkg.platforms = platforms_pkg
    platforms_pkg.base = base_mod
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_pkg
    sys.modules["gateway.platforms.base"] = base_mod


def _load_modules():
    _install_stubs()
    pkg = types.ModuleType("hermes_filament_fcm")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament_fcm"] = pkg
    for name in ("credentials", "fcm_client", "filament_api", "reactive", "adapter"):
        spec = importlib.util.spec_from_file_location(
            f"hermes_filament_fcm.{name}", _PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"hermes_filament_fcm.{name}"] = module
        spec.loader.exec_module(module)
    return (
        sys.modules["hermes_filament_fcm.filament_api"],
        sys.modules["hermes_filament_fcm.adapter"],
    )


filament_api, adapter = _load_modules()
_Api = filament_api.FilamentAPI

_LOOP = "!loop:filament.example"
_OK = {"result": {"content": []}}
_REJECTED = {"error": {"code": -32602, "message": "You don't have permission"}}
_TRANSIENT = {"error": "HTTP 503"}


class _FakeApi:
    """Replays scripted accept_vouch outcomes, reusing the real classifiers."""

    # The real implementations, not reimplementations — wrapped in staticmethod
    # so a plain function attribute doesn't re-bind and eat `self`.
    result_error = staticmethod(_Api.result_error)
    is_retryable_error = staticmethod(_Api.is_retryable_error)

    def __init__(self, outcomes, vouches=None):
        self._outcomes = list(outcomes)
        self.calls = []
        self._vouches = vouches or []

    async def accept_vouch(self, loop_id):
        self.calls.append(loop_id)
        outcome = self._outcomes.pop(0) if self._outcomes else _OK
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def list_vouches(self):
        return {"result": {}}

    def parse_tool_result(self, result):
        return {"vouches": self._vouches}


def _make_adapter(api):
    a = adapter.FCMFilamentAdapter.__new__(adapter.FCMFilamentAdapter)
    a._filament_api = api
    a._vouch_accepts_in_flight = set()
    return a


def _no_backoff(monkeypatch):
    monkeypatch.setattr(adapter, "_VOUCH_ACCEPT_BACKOFF_S", 0)


# ── the classifier ──────────────────────────────────────────────────


def test_transport_failures_are_retryable():
    assert _Api.is_retryable_error("HTTP 503")
    assert _Api.is_retryable_error("HTTP 500")
    assert _Api.is_retryable_error("HTTP 429")
    assert _Api.is_retryable_error("invalid response")


def test_rejections_and_success_are_not_retryable():
    # A settled server decision, whatever its wording.
    assert not _Api.is_retryable_error("You don't have permission to knock")
    assert not _Api.is_retryable_error("code -32602")
    assert not _Api.is_retryable_error("HTTP 403")
    assert not _Api.is_retryable_error(None)
    assert not _Api.is_retryable_error("")


# ── the retry loop ──────────────────────────────────────────────────


def test_transient_failure_is_retried_then_succeeds(monkeypatch):
    _no_backoff(monkeypatch)
    api = _FakeApi([_TRANSIENT, _OK])
    a = _make_adapter(api)

    assert asyncio.run(a._accept_vouch(_LOOP)) is True
    assert len(api.calls) == 2


def test_raised_exception_is_retried_then_succeeds(monkeypatch):
    _no_backoff(monkeypatch)
    api = _FakeApi([RuntimeError("connection reset"), _OK])
    a = _make_adapter(api)

    assert asyncio.run(a._accept_vouch(_LOOP)) is True
    assert len(api.calls) == 2


def test_server_rejection_is_not_retried(monkeypatch):
    _no_backoff(monkeypatch)
    api = _FakeApi([_REJECTED, _OK])
    a = _make_adapter(api)

    assert asyncio.run(a._accept_vouch(_LOOP)) is False
    # Retrying would have hit the scripted _OK and reported a false success.
    assert len(api.calls) == 1


def test_retries_are_bounded(monkeypatch):
    _no_backoff(monkeypatch)
    api = _FakeApi([_TRANSIENT] * 10)
    a = _make_adapter(api)

    assert asyncio.run(a._accept_vouch(_LOOP)) is False
    assert len(api.calls) == adapter._VOUCH_ACCEPT_ATTEMPTS


def test_missing_api_does_not_raise():
    a = _make_adapter(None)
    assert asyncio.run(a._accept_vouch(_LOOP)) is False


# ── overlap between the startup sweep and a live push ───────────────


def test_concurrent_accepts_for_one_loop_knock_once(monkeypatch):
    """The sweep now runs with the listener up, so both can name one loop."""
    _no_backoff(monkeypatch)
    api = _FakeApi([_TRANSIENT, _OK])
    a = _make_adapter(api)

    async def both():
        return await asyncio.gather(a._accept_vouch(_LOOP), a._accept_vouch(_LOOP))

    results = asyncio.run(both())

    assert len(api.calls) == 2  # the two attempts of one accept, not two accepts
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_in_flight_marker_is_released(monkeypatch):
    _no_backoff(monkeypatch)
    api = _FakeApi([_OK, _OK])
    a = _make_adapter(api)

    assert asyncio.run(a._accept_vouch(_LOOP)) is True
    assert a._vouch_accepts_in_flight == set()
    # A later vouch for the same loop is not mistaken for the earlier one.
    assert asyncio.run(a._accept_vouch(_LOOP)) is True


def test_sweep_accepts_each_mailbox_entry(monkeypatch):
    _no_backoff(monkeypatch)
    other = "!other:filament.example"
    api = _FakeApi([_OK, _OK], vouches=[{"loop_id": _LOOP}, {"loop_id": other}])
    a = _make_adapter(api)

    asyncio.run(a._accept_pending_vouches())

    assert api.calls == [_LOOP, other]
