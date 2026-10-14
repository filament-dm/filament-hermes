"""How the plugin process moves media bytes (filament-dm/synapse#1157).

A server offering the signed-URL tools gets ``get_media_download_url`` /
``create_media_upload`` (the tool authorizes, a bearer-free URL carries the
bytes); one without them gets the bearer side-channels ``/media`` and
``/upload``. Either way the bytes go between this process and the server,
never through the model.

Loaded standalone like ``test_poll_client``; HTTP runs against an
``httpx.MockTransport``.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest


def _load_filament_api():
    base = Path(__file__).parent.parent / "hermes_filament_fcm"
    pkg_name = "hfcm_media_routes"
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

MCP = "https://hs.example/mcp/agents"
SIGNED_GET = "https://hs.example/mcp/agents/media/signed/tok123"
SIGNED_PUT = "https://hs.example/mcp/agents/upload/signed/tok456"


def _tool_result(obj):
    return {"result": {"content": [{"type": "text", "text": json.dumps(obj)}]}}


def _api(server_tools, tool_results, handler):
    """A FilamentAPI whose tool calls return *tool_results* by name and whose
    HTTP goes to *handler*. Returns (api, tool_calls, requests)."""
    api = fa.FilamentAPI(MCP, "fmcp_secret")
    api.server_tools = frozenset(server_tools)
    calls, requests = [], []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return tool_results[name]

    def record(request):
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    api.call_tool = call_tool
    api._client_for_loop = lambda: client
    return api, calls, requests


# ── fetch_media ──────────────────────────────────────────────────────


def test_fetch_uses_the_signed_url_without_a_bearer(tmp_path):
    api, calls, requests = _api(
        {"get_media_download_url"},
        {
            "get_media_download_url": _tool_result(
                {
                    "download_url": SIGNED_GET,
                    "bytes": 6,
                    "expires_at": "2026-10-14T00:05:00Z",
                }
            )
        },
        lambda r: httpx.Response(200, content=b"pixels"),
    )
    dest = tmp_path / "a.png"
    n = asyncio.run(api.fetch_media("mxc://hs/abc", str(dest), message_id="$evt"))
    assert n == 6 and dest.read_bytes() == b"pixels"
    assert calls == [
        ("get_media_download_url", {"mxc_url": "mxc://hs/abc", "message_id": "$evt"})
    ]
    (req,) = requests
    assert str(req.url) == SIGNED_GET
    assert "authorization" not in req.headers


def test_fetch_falls_back_to_the_bearer_side_channel(tmp_path):
    # An older server (no tool), and a current one asked without a message id
    # (the tool would refuse anything but the agent's own uploads), both use
    # the bearer GET.
    for tools, message_id in ((set(), "$evt"), ({"get_media_download_url"}, None)):
        api, calls, requests = _api(
            tools, {}, lambda r: httpx.Response(200, content=b"img")
        )
        dest = tmp_path / "b.png"
        assert (
            asyncio.run(
                api.fetch_media("mxc://hs/abc", str(dest), message_id=message_id)
            )
            == 3
        )
        assert calls == []
        (req,) = requests
        assert req.url.path == "/mcp/agents/media"
        assert req.url.params["mxc_url"] == "mxc://hs/abc"
        assert req.headers["authorization"] == "Bearer fmcp_secret"


def test_fetch_raises_a_tool_refusal_without_trying_the_bearer_route(tmp_path):
    api, _, requests = _api(
        {"get_media_download_url"},
        {"get_media_download_url": _tool_result({"error": "not readable"})},
        lambda r: httpx.Response(200, content=b"img"),
    )
    with pytest.raises(RuntimeError, match="not readable"):
        asyncio.run(api.fetch_media("mxc://hs/abc", str(tmp_path / "c"), "$evt"))
    assert requests == []


def test_fetch_refuses_media_over_the_cap(tmp_path):
    # Declared size over the cap: refused before any transfer.
    api, _, requests = _api(
        {"get_media_download_url"},
        {
            "get_media_download_url": _tool_result(
                {"download_url": SIGNED_GET, "bytes": 100}
            )
        },
        lambda r: httpx.Response(200, content=b"x" * 100),
    )
    dest = tmp_path / "d"
    with pytest.raises(RuntimeError, match="cap"):
        asyncio.run(api.fetch_media("mxc://hs/abc", str(dest), "$evt", max_bytes=10))
    assert requests == []
    # Undeclared, the stream stops at the cap and leaves no file behind.
    api, _, _ = _api(set(), {}, lambda r: httpx.Response(200, content=b"x" * 100))
    with pytest.raises(RuntimeError, match="cap"):
        asyncio.run(api.fetch_media("mxc://hs/abc", str(dest), "$evt", max_bytes=10))
    assert not dest.exists() and not Path(f"{dest}.part").exists()


# ── upload_media ─────────────────────────────────────────────────────


def test_upload_sends_the_file_to_the_signed_url(tmp_path):
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG-bytes")
    api, calls, requests = _api(
        {"create_media_upload"},
        {
            "create_media_upload": _tool_result(
                {
                    "upload_url": SIGNED_PUT,
                    "method": "PUT",
                    "upload_headers": {"Content-Type": "image/png"},
                    "mxc_url": "mxc://hs/reserved",
                    "max_bytes": 1000,
                }
            )
        },
        lambda r: httpx.Response(200, json={"mxc_url": "mxc://hs/reserved"}),
    )
    assert asyncio.run(api.upload_media(str(f))) == "mxc://hs/reserved"
    assert calls == [
        (
            "create_media_upload",
            {"filename": "chart.png", "bytes": 10, "content_type": "image/png"},
        )
    ]
    (req,) = requests
    assert req.method == "PUT" and str(req.url) == SIGNED_PUT
    assert "authorization" not in req.headers
    assert req.headers["content-type"] == "image/png"
    assert req.headers["content-length"] == "10"
    assert req.read() == b"\x89PNG-bytes"


def test_upload_refuses_a_file_over_the_servers_limit(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 50)
    api, _, requests = _api(
        {"create_media_upload"},
        {
            "create_media_upload": _tool_result(
                {"upload_url": SIGNED_PUT, "mxc_url": "mxc://hs/r", "max_bytes": 10}
            )
        },
        lambda r: httpx.Response(200, json={}),
    )
    with pytest.raises(RuntimeError, match="limit"):
        asyncio.run(api.upload_media(str(f)))
    assert requests == []


def test_upload_falls_back_to_the_bearer_side_channel(tmp_path):
    f = tmp_path / "notes.pdf"
    f.write_bytes(b"%PDF-1.4")
    api, calls, requests = _api(
        set(), {}, lambda r: httpx.Response(200, json={"mxc_url": "mxc://hs/up"})
    )
    assert asyncio.run(api.upload_media(str(f), filename="Notes.pdf")) == "mxc://hs/up"
    assert calls == []
    (req,) = requests
    assert req.method == "POST" and req.url.path == "/mcp/agents/upload"
    assert req.url.params["filename"] == "Notes.pdf"
    assert req.headers["authorization"] == "Bearer fmcp_secret"
    assert req.headers["content-type"] == "application/pdf"
    assert req.read() == b"%PDF-1.4"


def test_upload_failure_raises(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"x")
    api, _, _ = _api(set(), {}, lambda r: httpx.Response(413, json={"error": "big"}))
    with pytest.raises(RuntimeError, match="413"):
        asyncio.run(api.upload_media(str(f)))


# ── attachments on the send tools ────────────────────────────────────


def test_send_tools_carry_attachments_only_when_given():
    api, calls, _ = _api(
        set(),
        {"post_message": _tool_result({}), "reply_in_thread": _tool_result({})},
        lambda r: httpx.Response(200),
    )
    att = [{"mxc_url": "mxc://hs/a", "filename": "a.png"}]

    async def go():
        await api.post_message("!room", "hi")
        await api.post_message("!room", "", attachments=att)
        await api.reply_in_thread("$root", "cap", attachments=att)

    asyncio.run(go())
    assert calls == [
        ("post_message", {"channel": "!room", "markdown_body": "hi"}),
        (
            "post_message",
            {"channel": "!room", "markdown_body": "", "attachments": att},
        ),
        (
            "reply_in_thread",
            {"message_id": "$root", "markdown_body": "cap", "attachments": att},
        ),
    ]
