"""isError tool results read like JSON-RPC errors.

From MCP 2025-11-25 (SEP-1303, filament-dm/synapse#1347) the server reports a
known tool's failure as ``{"result": {"content": [...], "isError": true}}``
instead of ``{"error": {...}}``. FilamentAPI._post turns the plain-text kind
into the error shape callers already read, and leaves an in-band
``{"error": ...}`` result, which callers read from the parsed result, alone.

Loaded standalone like ``test_poll_client``.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_filament_api():
    base = Path(__file__).parent.parent / "hermes_filament_fcm"
    pkg_name = "hfcm_iserror"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(base)]
    sys.modules[pkg_name] = pkg
    for sub in ("_version", "observability", "filament_api"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{sub}", base / f"{sub}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{sub}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{pkg_name}.filament_api"]


fa = _load_filament_api()
FilamentAPI = fa.FilamentAPI


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, body):
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Client:
    def __init__(self, body):
        self.body = body
        self.sent = []

    async def post(self, url, content=None, headers=None):
        self.sent.append(json.loads(content))
        return _Resp(self.body)


def _api(body):
    api = FilamentAPI("http://x/mcp/agents", "tok")
    api._initialized = True
    client = _Client(body)
    api._client_for_loop = lambda: client
    api._poll_client_for_loop = lambda: client
    return api


def _text_result(text, is_error=None, rpc_id=7):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error is not None:
        result["isError"] = is_error
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _call(body, name="get_recent_messages"):
    return asyncio.run(_api(body).call_tool(name, {}))


def test_plain_text_is_error_becomes_a_json_rpc_error():
    raw = _call(_text_result("channel: required but missing", is_error=True))
    assert "result" not in raw
    assert raw["id"] == 7
    assert raw["error"]["code"] == -32000
    assert raw["error"]["message"] == "channel: required but missing"
    assert FilamentAPI.result_error(raw) == "channel: required but missing"
    assert not FilamentAPI.is_retryable_error(FilamentAPI.result_error(raw))


def test_is_error_without_text_still_names_the_failure():
    body = {"jsonrpc": "2.0", "id": 3, "result": {"content": [], "isError": True}}
    raw = _call(body, name="react")
    assert FilamentAPI.result_error(raw) == "react failed"


def test_is_error_json_that_is_not_an_error_object_is_still_an_error():
    raw = _call(_text_result('["not", "an", "object"]', is_error=True))
    assert FilamentAPI.result_error(raw) == '["not", "an", "object"]'


def test_in_band_error_result_keeps_its_current_handling():
    text = json.dumps({"error": "message_id $x not found"})
    flagged = _call(_text_result(text, is_error=True))
    unflagged = _call(_text_result(text))
    for raw in (flagged, unflagged):
        assert FilamentAPI.result_error(raw) is None
        assert FilamentAPI.parse_tool_result(raw) == {
            "error": "message_id $x not found"
        }
    assert flagged["result"]["isError"] is True


def test_normal_results_are_unchanged():
    for body in (
        _text_result(json.dumps({"messages": []})),
        _text_result(json.dumps({"messages": []}), is_error=False),
        _text_result("plain text, not JSON"),
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}},
    ):
        assert _call(body) == body


def test_is_error_outside_tools_call_is_left_alone():
    body = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "tools": []}}
    api = _api(body)

    async def go():
        return await api._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert asyncio.run(go()) == body


def test_poll_work_is_error_reads_as_an_error():
    api = _api(_text_result("Unauthorized: token revoked", is_error=True))
    raw = asyncio.run(api.poll_work({"wait_seconds": 30}))
    error = FilamentAPI.result_error(raw)
    assert error == "Unauthorized: token revoked"


def test_is_error_logs_like_a_json_rpc_error(monkeypatch):
    seen = []

    class _Log:
        def debug(self, event, **kw):
            seen.append(("debug", event, kw))

        def warning(self, event, **kw):
            seen.append(("warning", event, kw))

    monkeypatch.setattr(fa, "slog", _Log())
    _call(_text_result("nope", is_error=True))
    _call(_text_result(json.dumps({"error": "nope"}), is_error=True))
    _call(_text_result(json.dumps({"ok": True})))
    done = [s for s in seen if s[1] != "filament_fcm.mcp.request.start"]
    assert [(level, event) for level, event, _ in done] == [
        ("warning", "filament_fcm.mcp.request.error"),
        ("warning", "filament_fcm.mcp.request.error"),
        ("debug", "filament_fcm.mcp.request.complete"),
    ]
    assert done[0][2]["jsonrpc_error_code"] == -32000
    assert done[0][2]["tool_is_error"] is True
    assert done[1][2]["tool_is_error"] is True
    assert "tool_is_error" not in done[2][2]
