"""A dead sign-in stops the reconnect loop with a message that says why.

Once the owner deletes the agent (or it is signed out) in Filament, every MCP
call answers 401. The gateway used to retry Stage 1 every 30-60 s forever and
log "get_self response missing owner.user_id" (live run h20-r4-delete), which
neither the owner nor the model reading the log could act on.

The adapter is loaded with the Hermes stubs ``test_media_send`` installs.
"""

import asyncio
import importlib.util
import types
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_media_send", Path(__file__).with_name("test_media_send.py")
)
_media_send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_media_send)
adapter = _media_send.adapter
_Adapter = adapter.FCMFilamentAdapter


class _Api:
    _mcp_url = "http://server/mcp/agents"

    def __init__(self, init):
        self._init = init
        self.self_calls = 0

    async def initialize(self):
        return self._init

    async def get_self(self):
        self.self_calls += 1
        return {"error": {"code": -32001, "message": "Authentication required."}}


def _stub(init):
    fatal = []
    stub = types.SimpleNamespace(
        _filament_api=_Api(init),
        _reserved=False,
        _auth_refused=None,
        _installation_id="inst",
        _gateway_instance_id="gw",
        _set_fatal_error=lambda code, message, retryable: fatal.append(
            (code, message, retryable)
        ),
    )
    return stub, fatal


def test_a_refused_initialize_is_an_auth_refusal_not_a_get_self_error():
    stub, _ = _stub({"error": "HTTP 401"})
    assert asyncio.run(_Adapter._initialize_api(stub)) is False
    assert stub._auth_refused == "HTTP 401"
    # Stage 1 stops there: no get_self to fail on a missing owner.
    assert stub._filament_api.self_calls == 0


def test_a_refused_sign_in_is_fatal_and_not_retried(monkeypatch):
    stub, fatal = _stub({"error": "HTTP 401"})
    monkeypatch.setattr(adapter, "_seed_display_defaults", lambda: None)

    async def initialize_api():
        return await _Adapter._initialize_api(stub)

    stub._initialize_api = initialize_api
    assert asyncio.run(_Adapter._connect_attempt(stub, False, "conn")) is False
    assert fatal == [("auth_refused", adapter.AUTH_REFUSED_MESSAGE, False)]
    assert "press Connect" in adapter.AUTH_REFUSED_MESSAGE


def test_other_initialize_failures_stay_retryable():
    stub, _ = _stub({"error": "HTTP 502"})
    asyncio.run(_Adapter._initialize_api(stub))
    assert stub._auth_refused is None
