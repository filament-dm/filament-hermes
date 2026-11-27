"""The plugin speaks MCP 2026-07-28 to Filament (filament-dm/synapse#1347).

Stateless MCP: no initialize or session. Every request carries
``params._meta`` (protocol version, client capabilities, clientInfo) and the
``MCP-Protocol-Version`` / ``Mcp-Method`` / ``Mcp-Name`` headers; discovery is
``server/discover``. Filament's own error codes move out of the JSON-RPC
reserved range (-32002 -> -31002, -32001 -> -31001), and a known tool's
failure arrives as an ``isError`` result keeping its code in ``_meta``.
"""

import ast
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _load_filament_api():
    base = Path(__file__).parent.parent / "hermes_filament_fcm"
    pkg_name = "hfcm_mcp_2026"
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
META = "io.modelcontextprotocol/"


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Client:
    """Answers each POST with the next canned body, recording what was sent."""

    def __init__(self, *bodies, status_code=200):
        self.bodies = list(bodies)
        self.status_code = status_code
        self.sent = []

    async def post(self, url, content=None, headers=None):
        self.sent.append((json.loads(content), dict(headers or {})))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return _Resp(body, self.status_code)


def _api(client, connected=True):
    api = FilamentAPI("http://x/mcp/agents", "tok")
    api._initialized = connected
    api._client_for_loop = lambda: client
    api._poll_client_for_loop = lambda: client
    return api


def _ok(payload, rpc_id=1):
    text = json.dumps(payload)
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def _is_error(text, code=None, rpc_id=1):
    result = {"content": [{"type": "text", "text": text}], "isError": True}
    result["_meta"] = {META + "serverInfo": {"name": "filament", "version": "1"}}
    if code is not None:
        result["_meta"]["dm.filament/errorCode"] = code
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


# ── Request shape ───────────────────────────────────────────────────────


def test_tools_call_carries_meta_and_routing_headers():
    client = _Client(_ok({"mxid": "@a:x"}))
    asyncio.run(_api(client).call_tool("get_self", {}))
    body, headers = client.sent[0]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "get_self"
    assert body["params"]["arguments"] == {}
    meta = body["params"]["_meta"]
    assert meta[META + "protocolVersion"] == "2026-07-28"
    assert meta[META + "clientCapabilities"] == {}
    assert meta[META + "clientInfo"]["name"] == "hermes-filament-fcm"
    assert isinstance(meta[META + "clientInfo"]["version"], str)
    assert headers["MCP-Protocol-Version"] == "2026-07-28"
    assert headers["Mcp-Method"] == "tools/call"
    assert headers["Mcp-Name"] == "get_self"
    assert headers["Authorization"] == "Bearer tok"
    assert not any(k.lower() == "mcp-session-id" for k in headers)


def test_mcp_name_that_is_not_plain_ascii_is_base64_wrapped():
    assert fa._encode_header_value("get_self") == "get_self"
    encoded = fa._encode_header_value("réact")
    assert encoded.startswith("=?base64?") and encoded.endswith("?=")
    assert fa._encode_header_value(" pad") != " pad"


def test_caller_meta_is_kept_alongside_the_protocol_meta():
    body = fa.with_request_meta(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "x", "_meta": {"k": 1}},
        }
    )
    assert body["params"]["_meta"]["k"] == 1
    assert body["params"]["_meta"][META + "protocolVersion"] == "2026-07-28"


def test_non_tool_methods_send_no_mcp_name():
    headers = fa.mcp_headers({"method": "tools/list", "params": {}})
    assert headers == {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}


def test_poll_work_speaks_the_same_protocol():
    client = _Client(_ok({"items": []}))
    asyncio.run(_api(client).poll_work({"wait_seconds": 30}))
    body, headers = client.sent[0]
    assert headers["Mcp-Name"] == "poll_work"
    assert body["params"]["_meta"][META + "protocolVersion"] == "2026-07-28"


# ── Discovery replaces initialize ───────────────────────────────────────


def test_initialize_is_one_server_discover_and_no_notification():
    discovered = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "supportedVersions": ["2026-07-28"],
            "instructions": "About Filament",
        },
    }
    client = _Client(discovered)
    api = _api(client, connected=False)
    result = asyncio.run(api.initialize())
    assert [b["method"] for b, _ in client.sent] == ["server/discover"]
    assert client.sent[0][1]["Mcp-Method"] == "server/discover"
    assert result["result"]["instructions"] == "About Filament"
    assert api.is_connected


def test_a_failed_discover_does_not_latch_connected():
    client = _Client(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "x"}}
    )
    api = _api(client, connected=False)
    asyncio.run(api.initialize())
    assert not api.is_connected


def test_call_tool_discovers_first_when_not_connected():
    client = _Client(
        {"jsonrpc": "2.0", "id": 1, "result": {}}, _ok({"ok": True}, rpc_id=2)
    )
    asyncio.run(_api(client, connected=False).call_tool("get_self", {}))
    assert [b["method"] for b, _ in client.sent] == ["server/discover", "tools/call"]


def test_fetch_tools_is_a_single_stateless_tools_list(monkeypatch):
    sent = []

    class _SyncResp:
        status_code = 200
        headers: dict = {}  # noqa: RUF012

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"tools": [{"name": "get_self"}]},
            }

    class _SyncClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, content=None):
            sent.append((json.loads(content), headers))
            return _SyncResp()

    monkeypatch.setattr(fa.httpx, "Client", _SyncClient)
    assert FilamentAPI.fetch_tools("http://x/mcp/agents", "tok") == [
        {"name": "get_self"}
    ]
    assert len(sent) == 1
    body, headers = sent[0]
    assert body["method"] == "tools/list"
    assert body["params"]["_meta"][META + "protocolVersion"] == "2026-07-28"
    assert headers["Mcp-Method"] == "tools/list"
    assert headers["Authorization"] == "Bearer tok"


# ── Errors ──────────────────────────────────────────────────────────────


def test_is_error_keeps_the_servers_code_from_meta():
    text = (
        "This agent isn't finalized yet — its principal is still setting it up. "
        "Tools become available once setup finishes; wait and retry."
    )
    raw = asyncio.run(_api(_Client(_is_error(text, code=-31002))).get_self())
    assert raw["error"]["code"] == -31002
    assert raw["error"]["message"] == text
    assert raw["error"]["data"] == {"isError": True}
    assert FilamentAPI.result_error(raw) == text


def test_argument_error_reads_with_its_code_and_signature():
    text = (
        "Invalid arguments: channel: required but missing. "
        "Expected get_recent_messages(channel, limit?)."
    )
    raw = asyncio.run(
        _api(_Client(_is_error(text, code=-32602))).call_tool("get_recent_messages", {})
    )
    assert raw["error"]["code"] == -32602
    assert "Expected get_recent_messages(" in FilamentAPI.result_error(raw)


@pytest.mark.parametrize("code", [True, "x", None])
def test_is_error_without_a_usable_code_falls_back(code):
    raw = asyncio.run(_api(_Client(_is_error("nope", code=code))).get_self())
    assert raw["error"]["code"] == -32000


def test_in_band_rejection_with_is_error_still_reads_from_the_result():
    text = json.dumps({"error": "message_id $nope not found"})
    raw = asyncio.run(_api(_Client(_is_error(text))).get_thread("$nope"))
    assert FilamentAPI.result_error(raw) is None
    assert FilamentAPI.parse_tool_result(raw) == {"error": "message_id $nope not found"}


def test_unknown_tool_stays_a_json_rpc_error():
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "Unknown tool: nope"},
    }
    raw = asyncio.run(_api(_Client(body)).call_tool("nope", {}))
    assert raw == body


@pytest.mark.parametrize("status", [401, 429])
def test_transport_refusals_stay_http_shaped(status):
    body = {
        "jsonrpc": "2.0",
        "error": {"code": -31000 - (1 if status == 401 else 7), "message": "x"},
    }
    raw = asyncio.run(_api(_Client(body, status_code=status)).get_self())
    assert raw == {"error": f"HTTP {status}"}
    assert FilamentAPI.is_retryable_error(FilamentAPI.result_error(raw)) is (
        status == 429
    )


def test_code_sets_cover_both_eras():
    assert {-32002, -31002} <= fa.NOT_SET_UP_CODES
    assert {-32001, -31001} <= fa.AUTH_REQUIRED_CODES
    assert -31003 not in fa.NOT_SET_UP_CODES


# ── Callers keyed on the not-set-up / auth codes ────────────────────────


def _adapter():
    spec = importlib.util.spec_from_file_location(
        "_media_send_2026", Path(__file__).with_name("test_media_send.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.adapter


@pytest.mark.parametrize(
    ("code", "expected"),
    [(-32002, True), (-31002, True), (-31003, False), (-32602, False)],
)
def test_adapter_reads_not_finalized_in_both_eras(code, expected):
    result = {"error": {"code": code, "message": "m"}}
    assert _adapter()._is_not_finalized(result) is expected


def test_adapter_reads_not_finalized_from_an_is_error_result():
    raw = asyncio.run(
        _api(_Client(_is_error("wait and retry", code=-31002))).get_self()
    )
    assert _adapter()._is_not_finalized(raw)


_SETUP_CLI = Path(__file__).parent.parent / "hermes_filament_fcm" / "setup_cli.py"


def _wait_for_finalization(responses):
    """setup_cli._wait_for_finalization against canned get_self answers.

    setup_cli imports Hermes, so the function is lifted out of its source."""
    tree = ast.parse(_SETUP_CLI.read_text())
    printed = []

    class _FakeApi:
        def __init__(self, url, token):
            pass

        async def get_self(self):
            return responses.pop(0)

        parse_tool_result = staticmethod(FilamentAPI.parse_tool_result)

        async def close(self):
            pass

    async def _no_sleep(_s):
        return None

    ns = {
        "asyncio": types.SimpleNamespace(sleep=_no_sleep, run=asyncio.run),
        "time": __import__("time"),
        "FilamentAPI": _FakeApi,
        "NOT_SET_UP_CODES": fa.NOT_SET_UP_CODES,
        "AUTH_REQUIRED_CODES": fa.AUTH_REQUIRED_CODES,
        "UNREACHABLE_BUDGET_S": 60.0,
        "print_info": lambda m: printed.append(("info", m)),
        "print_warning": lambda m: printed.append(("warning", m)),
        "print_success": lambda m: printed.append(("success", m)),
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_wait_for_finalization":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns["_wait_for_finalization"]("tok", "http://x"), printed


def test_setup_waits_through_minus_31002_then_connects():
    owner = {"owner": {"user_id": "@u_test1:x"}}
    ready, printed = _wait_for_finalization(
        [
            {"error": {"code": -31002, "message": "wait"}},
            {"error": {"code": -32002, "message": "wait"}},
            _ok(owner),
        ]
    )
    assert ready == (True, "@u_test1:x")
    # One nudge across both codes, then the success line.
    assert [kind for kind, _ in printed] == ["info", "success"]


def test_setup_stops_on_minus_31001():
    ready, printed = _wait_for_finalization(
        [{"error": {"code": -31001, "message": "auth"}}]
    )
    assert ready == (False, None)
    assert printed[0][0] == "warning"
