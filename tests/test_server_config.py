"""Tests for the server-side agent config sync (server_config.py).

Modules are loaded standalone (same synthetic-package pattern as
``test_version_and_update.py``) so a bare dev environment without Hermes
works; all HTTP is stubbed at the injected-client seam (``ServerConfigSync``
takes any object with async ``get_config`` / ``put_config`` / ``post_tools``)
plus one httpx-free test of ``FilamentAPI``'s side-channel request shape.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _load(name: str):
    mod_name = f"hermes_filament_fcm.{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if "hermes_filament_fcm" not in sys.modules:
        pkg = types.ModuleType("hermes_filament_fcm")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["hermes_filament_fcm"] = pkg
    spec = importlib.util.spec_from_file_location(mod_name, _PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


reactive = _load("reactive")
server_config = _load("server_config")
filament_api = _load("filament_api")


# ── Test doubles ─────────────────────────────────────────────────────


class FakeAPI:
    """Recording stub for the three side-channel endpoints.

    ``get_results`` / ``put_results`` are consumed in order (the last one
    repeats); an Exception instance is raised instead of returned.
    """

    def __init__(self, get_results=None, put_results=None, post_tools_result=(200, {})):
        self.get_results = list(get_results or [])
        self.put_results = list(put_results or [])
        self.post_tools_result = post_tools_result
        self.get_calls = 0
        self.put_bodies = []
        self.tools_posted = []

    def _next(self, results):
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def get_config(self):
        self.get_calls += 1
        return self._next(self.get_results)

    async def put_config(self, body):
        self.put_bodies.append(body)
        return self._next(self.put_results)

    async def post_tools(self, tools):
        self.tools_posted.append(tools)
        result = self.post_tools_result
        if isinstance(result, Exception):
            raise result
        return result


def _make_sync(tmp_path, api, **kwargs):
    """A ServerConfigSync over stores rooted in tmp_path, TTL off by default."""
    stores = {
        "capability_store": reactive.CapabilityPolicyStore(
            tmp_path / "capability_policy.json"
        ),
        "wake_store": reactive.WakePolicyStore(tmp_path / "wake_policy.json"),
        "instructions_store": reactive.InstructionsStore(tmp_path / "instructions.md"),
        "feature_store": reactive.FeatureFlagStore(tmp_path / "feature_flags.json"),
    }
    kwargs.setdefault("ttl_seconds", 0.0)
    return server_config.ServerConfigSync(api, **stores, **kwargs), stores


_CONFIG = {
    "capability_policy": {
        "default_capabilities": ["messaging"],
        "bundles": {"calendar": ["list_events"]},
        "per_channel": {"!room": ["calendar"]},
        "per_user": {},
    },
    "wake_policy": {
        "trigger_emojis": ["🐞"],
        "reactive_wake": "all",
        "reply_style": "channel",
        "per_channel": {},
    },
    "instructions": "Reply politely.\nEscalate anything odd.\n",
    "feature_flags": {"advanced_tool_controls": True},
}


# ── Hydrate: apply a server document to the local store files ────────


def test_apply_writes_store_files_byte_identically(tmp_path):
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 7})])
    sync, stores = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())

    # Each JSON section lands exactly as the local set_* writers serialize it,
    # so every read-fresh consumer sees the same bytes as a local edit.
    assert (tmp_path / "capability_policy.json").read_text() == json.dumps(
        _CONFIG["capability_policy"], indent=2
    )
    assert (tmp_path / "wake_policy.json").read_text() == json.dumps(
        _CONFIG["wake_policy"], indent=2
    )
    assert (tmp_path / "feature_flags.json").read_text() == json.dumps(
        _CONFIG["feature_flags"], indent=2
    )
    # The instructions section is the file contents, verbatim.
    assert (tmp_path / "instructions.md").read_text() == _CONFIG["instructions"]
    assert sync.revision == 7

    # And the stores resolve them exactly as if written locally.
    assert stores["wake_store"].read()["reactive_wake"] == "all"
    assert stores["feature_store"].is_enabled("advanced_tool_controls") is True
    assert stores["capability_store"].read()["bundles"] == {"calendar": ["list_events"]}


def test_stale_fetch_never_overwrites_a_newer_local_write(tmp_path):
    # A write-back bumps the server to revision 8 while a fetch carrying
    # revision 7 is still in flight; when that stale response lands it must
    # NOT be applied over the files the write-back just mirrored.
    api = FakeAPI(
        get_results=[(200, {"config": _CONFIG, "revision": 7})],
        put_results=[(200, {"revision": 8})],
    )
    sync, stores = _make_sync(tmp_path, api)

    stores["capability_store"].write({"default_capabilities": ["messaging", "escalate"]})
    asyncio.run(sync.write_back())  # server now at revision 8, remembered
    assert sync.revision == 8
    fresh = (tmp_path / "capability_policy.json").read_text()

    asyncio.run(sync.sync())  # the in-flight fetch arrives carrying revision 7
    assert (tmp_path / "capability_policy.json").read_text() == fresh
    assert sync.revision == 8


def test_apply_only_present_sections(tmp_path):
    (tmp_path / "instructions.md").write_text("keep me")
    api = FakeAPI(
        get_results=[
            (200, {"config": {"wake_policy": {"reactive_wake": "off"}}, "revision": 2})
        ]
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())
    # The present section applied; the absent ones left local files untouched.
    assert json.loads((tmp_path / "wake_policy.json").read_text()) == {
        "reactive_wake": "off"
    }
    assert (tmp_path / "instructions.md").read_text() == "keep me"
    assert not (tmp_path / "capability_policy.json").exists()


def test_same_revision_is_not_rewritten(tmp_path):
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 7})])
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())
    mtime = (tmp_path / "wake_policy.json").stat().st_mtime_ns
    (tmp_path / "wake_policy.json").write_text("local scribble")
    asyncio.run(sync.sync(force=True))
    # Unchanged revision → no re-apply (files not rewritten per wake).
    assert (tmp_path / "wake_policy.json").read_text() == "local scribble"
    assert api.get_calls == 2
    del mtime


# ── Seed: create the server document from the local files ────────────


def test_seed_puts_local_files_with_base_revision_zero(tmp_path):
    local_policy = {"default_capabilities": ["messaging", "escalate"], "bundles": {}}
    (tmp_path / "capability_policy.json").write_text(
        json.dumps(local_policy, indent=2)
    )
    (tmp_path / "instructions.md").write_text("be nice")
    api = FakeAPI(
        get_results=[(200, {"config": None, "revision": 0})],
        put_results=[(200, {"agent_user_id": "@a:x", "revision": 1})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())

    assert len(api.put_bodies) == 1
    body = api.put_bodies[0]
    # Create-once semantics: base_revision=0, document mirrors the files
    # verbatim, and sections with no local file are omitted (never invented).
    assert body["base_revision"] == 0
    assert body["config"] == {
        "capability_policy": local_policy,
        "instructions": "be nice",
    }
    assert sync.revision == 1


def test_seed_409_race_refetches_and_applies(tmp_path):
    (tmp_path / "instructions.md").write_text("my local text")
    api = FakeAPI(
        get_results=[
            (200, {"config": None, "revision": 0}),
            (200, {"config": _CONFIG, "revision": 3}),
        ],
        put_results=[(409, {"error": "revision_conflict", "current_revision": 3})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())
    # The concurrent seeder won: their document is fetched and applied.
    assert api.get_calls == 2
    assert (tmp_path / "instructions.md").read_text() == _CONFIG["instructions"]
    assert sync.revision == 3


# ── Fallback: offline and endpoint-less servers change nothing ───────


def test_network_error_warns_once_and_keeps_local_files(tmp_path, caplog):
    (tmp_path / "wake_policy.json").write_text('{"reactive_wake": "all"}')
    api = FakeAPI(get_results=[OSError("connect refused")])
    sync, stores = _make_sync(tmp_path, api)
    with caplog.at_level(logging.DEBUG, logger="gateway.filament_fcm"):
        asyncio.run(sync.sync())
        asyncio.run(sync.sync(force=True))
        asyncio.run(sync.sync(force=True))
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "server-config" in r.getMessage()
    ]
    assert len(warnings) == 1  # once, not per event
    # Local behavior untouched: the store still resolves from the local file.
    assert stores["wake_store"].read()["reactive_wake"] == "all"
    assert (tmp_path / "wake_policy.json").read_text() == '{"reactive_wake": "all"}'


def test_404_disables_sync_for_process_lifetime(tmp_path, caplog):
    api = FakeAPI(get_results=[(404, {})])
    sync, _ = _make_sync(tmp_path, api)
    with caplog.at_level(logging.DEBUG, logger="gateway.filament_fcm"):
        asyncio.run(sync.sync())
        asyncio.run(sync.sync(force=True))
        asyncio.run(sync.write_back())
    # One GET, then quiet: no retries, no write-backs, no warnings (an older
    # server is a normal condition, not an error).
    assert api.get_calls == 1
    assert api.put_bodies == []
    assert not sync.active
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ttl_caches_bursts(tmp_path):
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 1})])
    sync, _ = _make_sync(tmp_path, api, ttl_seconds=60.0)
    for _ in range(5):
        asyncio.run(sync.sync())
    assert api.get_calls == 1


# ── Write-back: a local set_* edit mirrors to the server ─────────────


def test_write_back_puts_full_document_without_base_revision(tmp_path):
    (tmp_path / "wake_policy.json").write_text(json.dumps({"reactive_wake": "all"}))
    (tmp_path / "feature_flags.json").write_text(
        json.dumps({"advanced_tool_controls": True})
    )
    (tmp_path / "instructions.md").write_text("post-edit text")
    api = FakeAPI(put_results=[(200, {"revision": 9})])
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back())

    assert len(api.put_bodies) == 1
    body = api.put_bodies[0]
    # Whole-value setter: the full document, no compare-and-set.
    assert "base_revision" not in body
    assert body["config"] == {
        "wake_policy": {"reactive_wake": "all"},
        "feature_flags": {"advanced_tool_controls": True},
        "instructions": "post-edit text",
    }
    assert sync.revision == 9


def test_write_back_failure_keeps_local_change(tmp_path, caplog):
    (tmp_path / "instructions.md").write_text("the local edit")
    api = FakeAPI(put_results=[OSError("boom")])
    sync, _ = _make_sync(tmp_path, api)
    with caplog.at_level(logging.DEBUG, logger="gateway.filament_fcm"):
        asyncio.run(sync.write_back())  # must not raise
    assert (tmp_path / "instructions.md").read_text() == "the local edit"
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
    assert sync.revision is None


# ── Escape hatch ─────────────────────────────────────────────────────


def test_env_off_short_circuits_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("FILAMENT_SERVER_CONFIG", "off")
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 1})])
    sync, _ = _make_sync(tmp_path, api, inventory_provider=lambda: [{"name": "x"}])
    asyncio.run(sync.sync(force=True))
    asyncio.run(sync.write_back())
    asyncio.run(sync.maybe_report_tools(force=True))
    assert api.get_calls == 0
    assert api.put_bodies == []
    assert api.tools_posted == []
    assert not sync.active
    assert not list(tmp_path.iterdir())  # no files touched


def test_env_default_is_enabled(monkeypatch):
    monkeypatch.delenv("FILAMENT_SERVER_CONFIG", raising=False)
    assert not server_config.server_config_disabled()
    monkeypatch.setenv("FILAMENT_SERVER_CONFIG", "on")
    assert not server_config.server_config_disabled()
    for val in ("off", "OFF", "false", "0"):
        monkeypatch.setenv("FILAMENT_SERVER_CONFIG", val)
        assert server_config.server_config_disabled()


# ── Tool-inventory reporting ─────────────────────────────────────────


def test_report_tools_posts_inventory(tmp_path):
    inventory = [
        {"name": "post_message", "description": "Send a message", "origin": "filament"},
        {"name": "get_thread", "origin": "filament"},
    ]
    api = FakeAPI()
    sync, _ = _make_sync(tmp_path, api, inventory_provider=lambda: list(inventory))
    asyncio.run(sync.maybe_report_tools())
    assert api.tools_posted == [inventory]


def test_report_tools_rate_limited_and_404_silenced(tmp_path, caplog):
    api = FakeAPI()
    sync, _ = _make_sync(
        tmp_path,
        api,
        inventory_provider=lambda: [{"name": "x", "origin": "filament"}],
        tools_interval_seconds=3600.0,
    )
    asyncio.run(sync.maybe_report_tools())
    asyncio.run(sync.maybe_report_tools())  # within the hour → no second POST
    assert len(api.tools_posted) == 1

    api404 = FakeAPI(post_tools_result=(404, {}))
    sync404, _ = _make_sync(
        tmp_path,
        api404,
        inventory_provider=lambda: [{"name": "x", "origin": "filament"}],
    )
    with caplog.at_level(logging.DEBUG, logger="gateway.filament_fcm"):
        asyncio.run(sync404.maybe_report_tools(force=True))
        asyncio.run(sync404.maybe_report_tools(force=True))
    # One POST discovers the endpoint is absent; then silence, no warnings.
    assert len(api404.tools_posted) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── FilamentAPI side-channel request shape (httpx stubbed) ───────────


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_filament_api_config_and_tools_requests():
    api = filament_api.FilamentAPI("https://example.test/mcp/agents/", "tok")
    seen = []

    class _FakeClient:
        async def request(self, method, url, content=None, headers=None):
            seen.append(
                {
                    "method": method,
                    "url": url,
                    "body": json.loads(content) if content else None,
                    "headers": headers,
                }
            )
            return _FakeResponse(200, {"revision": 4})

    fake_client = _FakeClient()
    api._client_for_loop = lambda: fake_client

    async def scenario():
        assert await api.get_config() == (200, {"revision": 4})
        assert await api.put_config({"config": {}, "base_revision": 0}) == (
            200,
            {"revision": 4},
        )
        assert await api.post_tools([{"name": "x"}]) == (200, {"revision": 4})

    asyncio.run(scenario())

    assert [s["method"] for s in seen] == ["GET", "PUT", "POST"]
    assert seen[0]["url"] == "https://example.test/mcp/agents/config"
    assert seen[1]["url"] == "https://example.test/mcp/agents/config"
    assert seen[2]["url"] == "https://example.test/mcp/agents/tools"
    # Same bearer auth as the heartbeat/pong side-channels.
    for s in seen:
        assert s["headers"]["Authorization"] == "Bearer tok"
    assert seen[0]["body"] is None
    assert seen[1]["body"] == {"config": {}, "base_revision": 0}
    assert seen[2]["body"] == {"tools": [{"name": "x"}]}
