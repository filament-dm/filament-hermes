"""Tests for the version helpers (_version.py) and the update reminder
(update_check.py) plus the version plumbing in filament_api.py.

Modules are loaded standalone (same pattern as the other test files) so a
bare dev environment without Hermes works. httpx must be importable (it is
a declared dependency of the package): run with `uvx --with httpx pytest`.
"""

import asyncio
import importlib.util
import json
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


_version = _load("_version")
credentials = _load("credentials")
update_check = _load("update_check")
filament_api = _load("filament_api")


# ── _version: parsing and comparison ─────────────────────────────────


def test_plugin_version_is_string():
    # In a bare test env the distribution isn't installed → "unknown",
    # in an installed env → the real version. Either way: a str.
    assert isinstance(_version.plugin_version(), str)
    assert _version.plugin_version()


def test_version_headers_present():
    headers = _version.version_headers()
    assert headers["User-Agent"].startswith("hermes-filament-fcm/")
    assert headers["X-Filament-Plugin-Version"]


def test_version_from_pyproject():
    text = '[project]\nname = "x"\nversion = "1.2.3"\n'
    assert _version.version_from_pyproject(text) == "1.2.3"


def test_version_from_pyproject_this_repo():
    # The real pyproject.toml must parse — it's what the update check reads.
    text = (_PKG_DIR.parent / "pyproject.toml").read_text()
    got = _version.version_from_pyproject(text)
    assert got and got[0].isdigit()


def test_version_from_pyproject_missing():
    assert _version.version_from_pyproject("[project]\nname='x'\n") is None


def test_is_newer():
    assert _version.is_newer("0.2.0", "0.1.1")
    assert _version.is_newer("0.10.0", "0.9.9")
    assert _version.is_newer("1.0", "0.9.9")
    assert _version.is_newer("0.1.1.1", "0.1.1")
    assert not _version.is_newer("0.1.1", "0.1.1")
    assert not _version.is_newer("0.1.0", "0.1.1")
    assert not _version.is_newer("0.2", "0.2.0")  # equal after padding


def test_is_newer_fails_quiet_on_garbage():
    assert not _version.is_newer("unknown", "0.1.1")
    assert not _version.is_newer("0.2.0", "unknown")
    assert not _version.is_newer("", "")


# ── update_check: remind once per version ────────────────────────────


def _checker(tmp_path, current="0.1.0"):
    store = credentials.CredentialStore(base_dir=str(tmp_path))
    return update_check.UpdateChecker(store, current_version=current), store


def test_update_checker_reminds_then_stays_quiet(tmp_path, monkeypatch):
    checker, _ = _checker(tmp_path)

    async def fake_fetch(timeout=10.0):
        return "0.2.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)

    # Newer version, never notified → remind.
    assert asyncio.run(checker.check()) == "0.2.0"
    # Delivery failed (mark_notified not called) → remind again.
    assert asyncio.run(checker.check()) == "0.2.0"

    checker.mark_notified("0.2.0")
    # Same version already notified → quiet.
    assert asyncio.run(checker.check()) is None

    # An even newer version → remind again.
    async def fake_fetch2(timeout=10.0):
        return "0.3.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch2)
    assert asyncio.run(checker.check()) == "0.3.0"


def test_update_checker_quiet_when_current_or_unknown(tmp_path, monkeypatch):
    for latest in ("0.1.0", "0.0.9", None, "unknown"):
        checker, _ = _checker(tmp_path)

        async def fake_fetch(timeout=10.0, _latest=latest):
            return _latest

        monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)
        assert asyncio.run(checker.check()) is None


def test_notified_state_survives_restart(tmp_path, monkeypatch):
    checker, _ = _checker(tmp_path)

    async def fake_fetch(timeout=10.0):
        return "0.2.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)
    checker.mark_notified("0.2.0")

    # A fresh checker over the same store (= gateway restart) stays quiet.
    checker2, _ = _checker(tmp_path)
    assert asyncio.run(checker2.check()) is None


def test_corrupted_notice_file_still_reminds(tmp_path, monkeypatch):
    # A valid-JSON-but-non-dict update_notice.json must not kill the check
    # with AttributeError — it reads as "never notified" and gets rewritten
    # by the next mark_notified.
    (tmp_path / "update_notice.json").write_text('["not", "a", "dict"]')
    checker, _ = _checker(tmp_path)

    async def fake_fetch(timeout=10.0):
        return "0.2.0"

    monkeypatch.setattr(update_check, "fetch_latest_version", fake_fetch)
    assert asyncio.run(checker.check()) == "0.2.0"
    checker.mark_notified("0.2.0")
    assert asyncio.run(checker.check()) is None


def test_update_check_disabled_env(monkeypatch):
    monkeypatch.delenv("FILAMENT_DISABLE_UPDATE_CHECK", raising=False)
    assert not update_check.update_check_disabled()
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("FILAMENT_DISABLE_UPDATE_CHECK", val)
        assert update_check.update_check_disabled()
    monkeypatch.setenv("FILAMENT_DISABLE_UPDATE_CHECK", "false")
    assert not update_check.update_check_disabled()


def _fetch_with_fake_httpx(monkeypatch):
    """Run fetch_latest_version against a stubbed httpx, return the URL hit."""
    seen = {}

    class FakeResponse:
        status_code = 200
        text = '[project]\nname = "x"\nversion = "9.9.9"\n'

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return FakeResponse()

    monkeypatch.setattr(update_check.httpx, "AsyncClient", FakeAsyncClient)
    got = asyncio.run(update_check.fetch_latest_version())
    assert got == "9.9.9"
    return seen["url"]


def test_fetch_latest_version_env_url_override(monkeypatch):
    # The env var is read per call, after module load — a harness that sets
    # it before starting the gateway redirects the check to its own server.
    monkeypatch.setenv(
        "FILAMENT_UPDATE_CHECK_URL", "http://127.0.0.1:9/pyproject.toml"
    )
    assert _fetch_with_fake_httpx(monkeypatch) == "http://127.0.0.1:9/pyproject.toml"


def test_fetch_latest_version_default_url(monkeypatch):
    # Without the override (or with it empty) the GitHub raw URL is used.
    monkeypatch.delenv("FILAMENT_UPDATE_CHECK_URL", raising=False)
    assert _fetch_with_fake_httpx(monkeypatch) == _version.LATEST_PYPROJECT_URL
    monkeypatch.setenv("FILAMENT_UPDATE_CHECK_URL", "")
    assert _fetch_with_fake_httpx(monkeypatch) == _version.LATEST_PYPROJECT_URL


def test_build_reminder_mentions_versions():
    note = update_check.build_reminder("0.2.0", "0.1.0")
    assert "0.2.0" in note and "0.1.0" in note
    # The plugin is a directory plugin now, so the update instruction is the
    # `hermes plugins update` command (not a pip/repo URL).
    assert "hermes plugins update" in note


# ── filament_api: client survives a disconnect/reconnect cycle ───────


def test_client_usable_again_after_close():
    """close() (adapter disconnect) must not poison the reconnect path.

    The gateway's reconnect watcher reuses the same FilamentAPI instance on
    the same loop; a cached closed client made every request after a
    disconnect fail with "client has been closed" forever.
    """
    api = filament_api.FilamentAPI("https://example.test/mcp/agents", "tok")

    async def scenario():
        first = api._client_for_loop()
        await api.close()
        second = api._client_for_loop()
        assert second is not first
        assert not second.is_closed
        await api.close()

    asyncio.run(scenario())


# ── filament_api: version rides on every request ─────────────────────


def test_async_client_carries_version_headers():
    api = filament_api.FilamentAPI("https://example.test/mcp/agents", "tok")

    async def _get_headers():
        return dict(api._client_for_loop().headers)

    headers = asyncio.run(_get_headers())
    assert headers.get("user-agent", "").startswith("hermes-filament-fcm/")
    assert "x-filament-plugin-version" in headers


def test_initialize_sends_client_info():
    api = filament_api.FilamentAPI("https://example.test/mcp/agents", "tok")
    posted = []

    async def fake_post(body):
        posted.append(body)
        return {"result": {}}

    api._post = fake_post
    asyncio.run(api.initialize())

    init = posted[0]
    assert init["method"] == "initialize"
    client_info = init["params"]["clientInfo"]
    assert client_info["name"] == "hermes-filament-fcm"
    assert client_info["version"] == _version.PLUGIN_VERSION


def test_fetch_tools_sends_version(monkeypatch):
    seen = {"headers": [], "bodies": []}

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            body = seen["bodies"][-1]
            if body.get("method") == "tools/list":
                return {"result": {"tools": []}}
            return {"result": {}}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, content=None):
            seen["headers"].append(headers or {})
            seen["bodies"].append(json.loads(content))
            return FakeResponse()

    monkeypatch.setattr(filament_api.httpx, "Client", FakeClient)
    tools = filament_api.FilamentAPI.fetch_tools("https://example.test", "tok")
    assert tools == []
    for headers in seen["headers"]:
        assert headers.get("User-Agent", "").startswith("hermes-filament-fcm/")
        assert "X-Filament-Plugin-Version" in headers
    assert seen["bodies"][0]["params"]["clientInfo"]["name"] == "hermes-filament-fcm"


def test_adapter_imports_what_the_reminder_delivery_uses():
    # The delivery block was once commented out and the import cleanup took
    # build_reminder with it; re-enabling then raised a NameError that the
    # update-check loop swallowed at debug level — the reminder silently
    # never sent. Pin the import to the usage.
    adapter_src = (_PKG_DIR / "adapter.py").read_text()
    if "build_reminder(" in adapter_src:
        import_line = next(
            line for line in adapter_src.splitlines()
            if line.startswith("from .update_check import")
        )
        assert "build_reminder" in import_line
