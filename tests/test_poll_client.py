"""poll_work runs on its own HTTP client, with no idle connections kept.

The shared client is replaced whenever a call comes from another event loop,
and closing it cuts whatever is in flight; a long poll almost always is. And
a pooled connection a proxy has closed for idling fails the next request
sent on it. A dedicated, keep-alive-free client avoids both.

Loaded standalone like ``test_result_error``.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import ClassVar

import httpx


def _load_filament_api():
    base = Path(__file__).parent.parent / "hermes_filament_fcm"
    pkg_name = "hfcm_poll_client"
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


class _Recording(httpx.AsyncClient):
    made: ClassVar[list] = []

    def __init__(self, **kwargs):
        _Recording.made.append(kwargs)
        super().__init__(**kwargs)


def test_poll_work_has_its_own_client_without_keepalive(monkeypatch):
    monkeypatch.setattr(fa.httpx, "AsyncClient", _Recording)
    _Recording.made.clear()
    api = fa.FilamentAPI("http://x/mcp/agents", "tok")

    async def go():
        shared = api._client_for_loop()
        poll = api._poll_client_for_loop()
        assert poll is not shared
        assert api._poll_client_for_loop() is poll  # reused on the same loop
        await api.close()
        assert poll.is_closed and shared.is_closed

    asyncio.run(go())
    poll_kwargs = _Recording.made[1]
    assert poll_kwargs["limits"].max_keepalive_connections == 0
    assert "limits" not in _Recording.made[0]


def test_poll_work_posts_on_the_poll_client(monkeypatch):
    api = fa.FilamentAPI("http://x/mcp/agents", "tok")
    api._initialized = True
    seen = {}

    async def fake_post(body, client=None):
        seen["body"], seen["client"] = body, client
        return {"result": {}}

    monkeypatch.setattr(api, "_post", fake_post)

    async def go():
        await api.poll_work({"wait_seconds": 30})
        return api._poll_client

    poll_client = asyncio.run(go())
    assert seen["client"] is poll_client
    assert seen["body"]["params"] == {
        "name": "poll_work",
        "arguments": {"wait_seconds": 30},
    }
