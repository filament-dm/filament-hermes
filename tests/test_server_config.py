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
        "channel_instructions_store": reactive.ChannelInstructionsStore(
            tmp_path / "channel_instructions.json"
        ),
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

    stores["capability_store"].write(
        {"default_capabilities": ["messaging", "escalate"]}
    )
    asyncio.run(sync.write_back("capability_policy"))  # server → revision 8, remembered
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


def test_apply_writes_channel_instructions_file(tmp_path):
    guidance = {"!room:x": "Answer in French.", "!team:x": "Be terse."}
    api = FakeAPI(
        get_results=[
            (200, {"config": {"channel_instructions": guidance}, "revision": 2})
        ]
    )
    sync, stores = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())
    # Lands exactly as the store's own writer serializes it, and resolves
    # per-channel exactly as if written locally.
    assert (tmp_path / "channel_instructions.json").read_text() == json.dumps(
        guidance, indent=2
    )
    assert stores["channel_instructions_store"].get("!room:x") == "Answer in French."
    assert stores["channel_instructions_store"].get("!other:x") == ""


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


def test_seed_includes_channel_instructions(tmp_path):
    guidance = {"!room:x": "Answer in French."}
    (tmp_path / "channel_instructions.json").write_text(json.dumps(guidance, indent=2))
    api = FakeAPI(
        get_results=[(200, {"config": None, "revision": 0})],
        put_results=[(200, {"agent_user_id": "@a:x", "revision": 1})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.sync())
    assert api.put_bodies[0]["config"] == {"channel_instructions": guidance}
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
        asyncio.run(sync.write_back("instructions"))
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


def test_write_back_rebases_one_section_on_the_server_document(tmp_path):
    # The server holds revision 8 with a newer wake_policy the local files
    # don't have; a local instructions edit must PUT only its own section on
    # top of the server document (compare-and-set on the fetched revision),
    # preserving the newer wake_policy — and applying it locally too.
    server_doc = {
        "wake_policy": {"reactive_wake": "all"},
        "instructions": "old text",
    }
    (tmp_path / "instructions.md").write_text("post-edit text")
    api = FakeAPI(
        get_results=[(200, {"config": server_doc, "revision": 8})],
        put_results=[(200, {"revision": 9})],
    )
    sync, stores = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("instructions"))

    assert len(api.put_bodies) == 1
    body = api.put_bodies[0]
    assert body["base_revision"] == 8
    assert body["config"] == {
        "wake_policy": {"reactive_wake": "all"},
        "instructions": "post-edit text",
    }
    assert sync.revision == 9
    # The server's newer wake_policy landed locally; the edited section kept
    # the local (newer) value.
    assert stores["wake_store"].read()["reactive_wake"] == "all"
    assert (tmp_path / "instructions.md").read_text() == "post-edit text"


def test_write_back_retries_once_on_revision_conflict(tmp_path):
    # A concurrent writer bumps the revision between our GET and PUT: the 409
    # refetches and rebases on the winner's document, composing both edits.
    (tmp_path / "instructions.md").write_text("mine")
    api = FakeAPI(
        get_results=[
            (200, {"config": {"instructions": "old"}, "revision": 3}),
            (200, {"config": {"wake_policy": {"reactive_wake": "all"}}, "revision": 4}),
        ],
        put_results=[(409, {"current_revision": 4}), (200, {"revision": 5})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("instructions"))

    assert len(api.put_bodies) == 2
    assert api.put_bodies[0]["base_revision"] == 3
    retry = api.put_bodies[1]
    assert retry["base_revision"] == 4
    # Rebased on the winner: their wake_policy survives alongside our edit.
    assert retry["config"] == {
        "wake_policy": {"reactive_wake": "all"},
        "instructions": "mine",
    }
    assert sync.revision == 5


def test_write_back_channel_instructions_section(tmp_path):
    # No set_* tool writes this section, but the write-back path is generic by
    # section name and must handle it like any other JSON section.
    guidance = {"!room:x": "Answer in French."}
    (tmp_path / "channel_instructions.json").write_text(json.dumps(guidance, indent=2))
    api = FakeAPI(
        get_results=[(200, {"config": {"instructions": "old"}, "revision": 3})],
        put_results=[(200, {"revision": 4})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("channel_instructions"))

    assert len(api.put_bodies) == 1
    body = api.put_bodies[0]
    assert body["base_revision"] == 3
    assert body["config"] == {
        "instructions": "old",
        "channel_instructions": guidance,
    }
    assert sync.revision == 4


def test_failed_write_back_retries_before_the_next_fetch_applies(tmp_path):
    # A write-back that fails must be retried by the next sync BEFORE the
    # fetched document is applied — otherwise the fetch would overwrite the
    # local edit that never made it to the server.
    (tmp_path / "instructions.md").write_text("my edit")
    api = FakeAPI(
        get_results=[(200, {"config": {"instructions": "server old"}, "revision": 3})],
        put_results=[OSError("blip"), (200, {"revision": 4})],
    )
    sync, _ = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("instructions"))  # fails; queued for retry

    asyncio.run(sync.sync())
    # The retry PUT happened, and the local edit survived the sync.
    assert len(api.put_bodies) == 2
    assert api.put_bodies[1]["config"]["instructions"] == "my edit"
    assert (tmp_path / "instructions.md").read_text() == "my edit"


def test_write_back_forgets_revision_when_other_sections_fail_to_apply(tmp_path):
    # PUT succeeds but bringing the other sections' local files up to the
    # rebased state fails: the revision must stay unremembered so the next
    # sync re-applies instead of skipping on an already-known revision.
    (tmp_path / "instructions.md").write_text("mine")
    api = FakeAPI(
        get_results=[
            (200, {"config": {"wake_policy": {"reactive_wake": "all"}}, "revision": 3})
        ],
        put_results=[(200, {"revision": 4})],
    )
    sync, stores = _make_sync(tmp_path, api)
    stores["wake_store"].write = lambda policy: (_ for _ in ()).throw(OSError("disk"))
    asyncio.run(sync.write_back("instructions"))
    assert sync.revision is None  # re-apply on the next sync, don't skip


def test_write_back_failure_keeps_local_change(tmp_path, caplog):
    (tmp_path / "instructions.md").write_text("the local edit")
    api = FakeAPI(
        get_results=[(200, {"config": None, "revision": 0})],
        put_results=[OSError("boom")],
    )
    sync, _ = _make_sync(tmp_path, api)
    with caplog.at_level(logging.DEBUG, logger="gateway.filament_fcm"):
        asyncio.run(sync.write_back("instructions"))  # must not raise
    assert (tmp_path / "instructions.md").read_text() == "the local edit"
    assert [r for r in caplog.records if r.levelno == logging.WARNING]
    assert sync.revision is None


def test_apply_filesystem_failure_never_raises_and_retries_next_sync(tmp_path):
    # A store writer blowing up (read-only disk, ENOSPC) must not raise out of
    # sync() into the event turn, and must leave the revision unremembered so
    # the next sync retries the apply.
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 5})])
    sync, stores = _make_sync(tmp_path, api)
    original_write = stores["wake_store"].write
    stores["wake_store"].write = lambda policy: (_ for _ in ()).throw(OSError("disk"))
    asyncio.run(sync.sync())  # must not raise
    assert sync.revision is None
    stores["wake_store"].write = original_write
    asyncio.run(sync.sync(force=True))
    assert sync.revision == 5


# ── Escape hatch ─────────────────────────────────────────────────────


def test_env_off_short_circuits_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("FILAMENT_SERVER_CONFIG", "off")
    api = FakeAPI(get_results=[(200, {"config": _CONFIG, "revision": 1})])
    sync, _ = _make_sync(tmp_path, api, inventory_provider=lambda: [{"name": "x"}])
    asyncio.run(sync.sync(force=True))
    asyncio.run(sync.write_back("instructions"))
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


# ── Tool health derivation ───────────────────────────────────────────


def test_health_filament_toolset_follows_session_state():
    assert (
        server_config.derive_tool_health("filament", {}, True)
        == server_config.TOOL_HEALTH_OK
    )
    assert (
        server_config.derive_tool_health("filament", {}, False)
        == server_config.TOOL_HEALTH_DISCONNECTED
    )
    # No API handle → no signal, not "healthy".
    assert server_config.derive_tool_health("filament", {}, None) is None


def test_health_mcp_toolset_maps_server_status():
    statuses = {
        "linear": {"name": "linear", "connected": True, "status": "connected"},
        "gh": {"name": "gh", "connected": False, "status": "failed", "error": "boom"},
    }
    assert (
        server_config.derive_tool_health("mcp-linear", statuses, None)
        == server_config.TOOL_HEALTH_OK
    )
    assert (
        server_config.derive_tool_health("mcp-gh", statuses, None)
        == server_config.TOOL_HEALTH_DISCONNECTED
    )


def test_health_mcp_auth_flavored_error_reads_as_needs_reauth():
    for error in (
        "HTTP 401 Unauthorized",
        "OAuth token expired",
        "server requires re-authentication",
        "invalid_grant",
    ):
        statuses = {
            "cal": {
                "name": "cal",
                "connected": False,
                "status": "failed",
                "error": error,
            }
        }
        assert (
            server_config.derive_tool_health("mcp-cal", statuses, None)
            == server_config.TOOL_HEALTH_NEEDS_REAUTH
        ), error


def test_health_mcp_disabled_and_connecting_read_as_disconnected():
    for status in ("disabled", "connecting", "configured"):
        statuses = {"s": {"name": "s", "connected": False, "status": status}}
        assert (
            server_config.derive_tool_health("mcp-s", statuses, None)
            == server_config.TOOL_HEALTH_DISCONNECTED
        ), status


def test_health_unknown_server_and_inprocess_toolsets_have_no_signal():
    # Server missing from the status map (older Hermes, race with registration).
    assert server_config.derive_tool_health("mcp-ghost", {}, None) is None
    # Malformed status row.
    assert server_config.derive_tool_health("mcp-bad", {"bad": "nope"}, None) is None
    # In-process toolsets (builtins, other plugins' local tools) carry no health.
    assert server_config.derive_tool_health("memory", {}, True) is None
    assert server_config.derive_tool_health("hermes", {}, False) is None


def test_write_back_batches_multiple_sections_in_one_put(tmp_path):
    # A mutation that edits two sections pushes them in ONE rebased PUT.
    # Pushed separately, the first push's rebase would overwrite the second
    # section's fresh local edit with the server's stale copy (the
    # slash-command grant + advanced_tool_controls enable pair).
    server_doc = dict(_CONFIG)
    server_doc["feature_flags"] = {"advanced_tool_controls": False}
    (tmp_path / "capability_policy.json").write_text(
        '{"per_channel": {"!x": ["post"]}}'
    )
    (tmp_path / "feature_flags.json").write_text(
        '{"advanced_tool_controls": true}'
    )
    api = FakeAPI(
        get_results=[(200, {"config": server_doc, "revision": 3})],
        put_results=[(200, {"revision": 4})],
    )
    sync, stores = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("capability_policy", "feature_flags"))

    assert len(api.put_bodies) == 1
    cfg = api.put_bodies[0]["config"]
    assert cfg["capability_policy"] == {"per_channel": {"!x": ["post"]}}
    assert cfg["feature_flags"] == {"advanced_tool_controls": True}
    # The local flag file was NOT reverted to the server's stale copy.
    assert json.loads((tmp_path / "feature_flags.json").read_text()) == {
        "advanced_tool_controls": True
    }


def test_pending_write_back_rides_along_with_the_next_push(tmp_path):
    # A failed section push retries as part of the next write-back's batch —
    # one PUT carries both, and neither local edit is clobbered.
    (tmp_path / "instructions.md").write_text("Local truth.")
    (tmp_path / "feature_flags.json").write_text(
        '{"advanced_tool_controls": true}'
    )
    api = FakeAPI(
        get_results=[
            Exception("network down"),
            (200, {"config": dict(_CONFIG), "revision": 3}),
        ],
        put_results=[(200, {"revision": 4})],
    )
    sync, _stores = _make_sync(tmp_path, api)
    asyncio.run(sync.write_back("instructions"))  # fetch fails → pending
    asyncio.run(sync.write_back("feature_flags"))  # batches the pending one

    assert len(api.put_bodies) == 1
    cfg = api.put_bodies[0]["config"]
    assert cfg["instructions"] == "Local truth."
    assert cfg["feature_flags"] == {"advanced_tool_controls": True}
    assert (tmp_path / "instructions.md").read_text() == "Local truth."
