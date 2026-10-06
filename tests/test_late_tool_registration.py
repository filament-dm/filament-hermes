"""A gateway that started on the static manifest registers the live tools it
lacked once the adapter connects.

The tool list is registered at plugin load. When the server is unreachable
then, the bundled manifest stands in, and without a second fetch the gateway
would run on it until the next restart, missing every tool added since the
manifest was generated.
"""

import asyncio

import pytest
from test_capability_policy_validation import _load_plugin_init

plugin = _load_plugin_init()


class _Ctx:
    def __init__(self):
        self.tools: list[str] = []
        self.adapter_factory = None

    def register_platform(self, **kwargs):
        self.adapter_factory = kwargs["adapter_factory"]

    def register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])

    def register_hook(self, *args, **kwargs):
        pass


class _FakeAdapter:
    def __init__(self, cfg, **kwargs):
        self.on_connected = kwargs.get("on_connected")


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("FILAMENT_MCP_TOKEN", "fmcp_test")
    monkeypatch.setattr(plugin, "FCMFilamentAdapter", _FakeAdapter)
    return _Ctx()


def _manifest_names():
    return {t["name"] for t in plugin._STATIC_TOOLS}


def _live_tools():
    return [*plugin._STATIC_TOOLS, {"name": "brand_new_tool", "description": "x"}]


def test_the_static_fallback_registers_missing_live_tools_on_connect(ctx, monkeypatch):
    def unreachable(*args, **kwargs):
        raise ConnectionRefusedError("server down")

    async def list_tools(self):
        return _live_tools()

    monkeypatch.setattr(plugin.FilamentAPI, "fetch_tools", unreachable)
    monkeypatch.setattr(plugin.FilamentAPI, "list_tools", list_tools)
    plugin.register(ctx)
    assert "brand_new_tool" not in ctx.tools

    adapter = ctx.adapter_factory(None)
    asyncio.run(adapter.on_connected())

    assert "brand_new_tool" in ctx.tools
    # Tools the manifest already registered are not registered twice.
    assert len(ctx.tools) == len(set(ctx.tools))
    # Only once: a reconnect does not fetch again.
    before = list(ctx.tools)
    asyncio.run(adapter.on_connected())
    assert ctx.tools == before


def test_a_live_startup_fetch_needs_nothing_after_connect(ctx, monkeypatch):
    calls = []

    async def list_tools(self):
        calls.append(1)
        return _live_tools()

    monkeypatch.setattr(
        plugin.FilamentAPI, "fetch_tools", lambda *a, **k: list(plugin._STATIC_TOOLS)
    )
    monkeypatch.setattr(plugin.FilamentAPI, "list_tools", list_tools)
    plugin.register(ctx)

    asyncio.run(ctx.adapter_factory(None).on_connected())

    assert calls == []


def test_a_failed_late_fetch_keeps_the_manifest_tools(ctx, monkeypatch):
    def unreachable(*args, **kwargs):
        raise ConnectionRefusedError("server down")

    async def still_down(self):
        raise ConnectionRefusedError("still down")

    monkeypatch.setattr(plugin.FilamentAPI, "fetch_tools", unreachable)
    monkeypatch.setattr(plugin.FilamentAPI, "list_tools", still_down)
    plugin.register(ctx)

    asyncio.run(ctx.adapter_factory(None).on_connected())

    assert _manifest_names() - plugin.BLOCKED_TOOLS.keys() <= set(ctx.tools)


def test_the_bundled_manifest_carries_the_settings_tools():
    assert {"declare_settings", "set_setting", "get_settings"} <= _manifest_names()
