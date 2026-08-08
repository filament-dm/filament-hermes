"""Tests for the unattended self-update (self_update.py) and the adapter
wiring that drives it.

Modules are loaded standalone (same pattern as the other test files) so a
bare dev environment without Hermes works — adapter.py itself can't be
imported here, so its half is pinned by reading the source.
"""

import ast
import importlib.util
import json
import subprocess
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


credentials = _load("credentials")
self_update = _load("self_update")


# ── The two messages the principal sees ──────────────────────────────


def test_notices_are_the_agreed_wording():
    assert (
        self_update.build_start_notice("0.9.1")
        == "New version of the Filament Plugin detected (v0.9.1), upgrading..."
    )
    assert (
        self_update.build_complete_notice("0.9.1")
        == "Upgrade to Filament Plugin version v0.9.1 complete."
    )


def test_failure_notice_offers_the_reconnect_route_not_a_restart_command():
    note = self_update.build_failure_notice("0.9.1", "git pull failed")
    assert "0.9.1" in note and "git pull failed" in note
    assert "reconnect" in note
    # `hermes gateway restart` typed into a terminal restarts the gateway in
    # the foreground of that terminal — never hand it to the principal.
    assert "gateway restart" not in note


# ── Opt-out ──────────────────────────────────────────────────────────


def test_auto_update_disabled_env(monkeypatch):
    monkeypatch.delenv("FILAMENT_DISABLE_AUTO_UPDATE", raising=False)
    assert not self_update.auto_update_disabled()
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("FILAMENT_DISABLE_AUTO_UPDATE", val)
        assert self_update.auto_update_disabled()
    monkeypatch.setenv("FILAMENT_DISABLE_AUTO_UPDATE", "false")
    assert not self_update.auto_update_disabled()


# ── The plugin tree ──────────────────────────────────────────────────


def test_plugin_root_is_the_repo_and_is_a_checkout():
    root = self_update.plugin_root()
    assert (root / "pyproject.toml").exists()
    assert (root / "hermes_filament_fcm").is_dir()
    # This working tree is a git clone, which is what an installed directory
    # plugin looks like too.
    assert self_update.is_git_checkout(root)


def test_is_git_checkout_false_without_a_repo(tmp_path):
    assert not self_update.is_git_checkout(tmp_path)


def test_version_on_disk_reads_the_tree_not_the_import(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "4.5.6"\n')
    assert self_update.version_on_disk(tmp_path) == "4.5.6"
    # Missing / unreadable pyproject is a None, never an exception: the
    # caller treats it as "the pull landed nothing" and says so.
    assert self_update.version_on_disk(tmp_path / "nope") is None


def test_git_pull_fails_cleanly_outside_a_repo(tmp_path):
    ok, reason = self_update.git_pull(tmp_path)
    assert not ok
    assert reason  # a human-readable line, not an empty string


def test_git_pull_is_ff_only(monkeypatch, tmp_path):
    """--ff-only, like `hermes plugins update`: a tree with local commits
    fails loudly instead of being merged under a running agent."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Already up to date.", stderr=""
        )

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)
    ok, out = self_update.git_pull(tmp_path)
    assert ok and out == "Already up to date."
    assert seen["cmd"][1:] == ["pull", "--ff-only"]
    assert seen["cwd"] == str(tmp_path)


# ── The marker that survives the restart ─────────────────────────────


def _store(tmp_path):
    return self_update.PendingUpgradeStore(
        credentials.CredentialStore(base_dir=str(tmp_path))
    )


def test_pending_upgrade_roundtrip_and_clear(tmp_path):
    store = _store(tmp_path)
    assert store.load() is None

    store.save("0.9.1", "!room:server")
    # A fresh store over the same dir = the process that came back after the
    # restart. It must find what the dying process wrote.
    reloaded = _store(tmp_path).load()
    assert reloaded["target_version"] == "0.9.1"
    assert reloaded["room_id"] == "!room:server"
    assert isinstance(reloaded["started_ms"], int)

    store.clear()
    assert _store(tmp_path).load() is None
    # Clearing an absent marker is success, not an error — the announce path
    # clears unconditionally.
    store.clear()


def test_pending_upgrade_survives_a_missing_room(tmp_path):
    # No backchannel known when the upgrade started: the marker is still
    # valid, the announce falls back to whatever room it learns on connect.
    _store(tmp_path).save("0.9.1", None)
    assert _store(tmp_path).load()["room_id"] is None


def test_corrupted_marker_reads_as_nothing_pending(tmp_path):
    (tmp_path / "pending_upgrade.json").write_text('["not", "a", "dict"]')
    assert _store(tmp_path).load() is None
    (tmp_path / "pending_upgrade.json").write_text("{not json")
    assert _store(tmp_path).load() is None


# ── Restart ──────────────────────────────────────────────────────────


def test_under_service_manager_detection(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(self_update.os.path, "exists", lambda p: False)
    assert not self_update._under_service_manager()

    monkeypatch.setenv("INVOCATION_ID", "abc")  # systemd
    assert self_update._under_service_manager()
    monkeypatch.delenv("INVOCATION_ID")

    # Interactive macOS shells inherit XPC_SERVICE_NAME=0 — not a service.
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert not self_update._under_service_manager()
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.hermes.gateway")
    assert self_update._under_service_manager()


def test_request_restart_false_without_hermes(monkeypatch):
    # No hermes in this env, so the lazy import fails — the update path must
    # degrade to "tell the principal" rather than raise.
    monkeypatch.setitem(sys.modules, "gateway.run", None)
    assert self_update.request_gateway_restart() is False


def test_request_restart_uses_the_runner_and_matches_slash_restart(monkeypatch):
    calls = []

    class FakeRunner:
        def request_restart(self, *, detached, via_service):
            calls.append((detached, via_service))
            return True

    runner = FakeRunner()
    fake_mod = types.ModuleType("gateway.run")
    fake_mod._gateway_runner_ref = lambda: runner
    pkg = types.ModuleType("gateway")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.run", fake_mod)

    # Under a supervisor: exit and let it restart us (via_service).
    monkeypatch.setattr(self_update, "_under_service_manager", lambda: True)
    assert self_update.request_gateway_restart() is True
    assert calls[-1] == (False, True)

    # Bare process: the gateway must spawn its own detached relaunch.
    monkeypatch.setattr(self_update, "_under_service_manager", lambda: False)
    assert self_update.request_gateway_restart() is True
    assert calls[-1] == (True, False)

    # A dead weakref (gateway already shutting down) is not an exception.
    fake_mod._gateway_runner_ref = lambda: None
    assert self_update.request_gateway_restart() is False


# ── Adapter wiring (source-level: adapter.py needs Hermes to import) ──


def _adapter_src() -> str:
    return (_PKG_DIR / "adapter.py").read_text()


def _adapter_method(name: str) -> str:
    """The source of one method of the adapter, via AST (no Hermes import)."""
    src = _adapter_src()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"adapter has no method {name}")


def test_adapter_imports_what_the_upgrade_path_uses():
    # Same failure mode as the reminder's import once had: a NameError in
    # here is swallowed by the update loop's debug-level except, so the
    # upgrade silently never happens. Pin imports to usage.
    src = _adapter_src()
    import_block = src[src.index("from .self_update import") :]
    import_block = import_block[: import_block.index(")")]
    body = src.replace(import_block, "")
    for used in (
        "build_start_notice",
        "build_complete_notice",
        "build_failure_notice",
        "git_pull",
        "is_git_checkout",
        "request_gateway_restart",
        "version_on_disk",
    ):
        # git_pull is handed to asyncio.to_thread, so match the bare name.
        assert used in body, f"{used} imported but unused"
        assert used in import_block, f"{used} used but not imported"


def test_marker_is_written_before_the_restart_is_requested():
    # Order is load-bearing: after request_gateway_restart the process is on
    # its way out, and a marker written later may never hit disk — leaving
    # the upgrade silent even though it worked.
    body = _adapter_method("_auto_upgrade")
    assert body.index("_pending_upgrade.save(") < body.index("request_gateway_restart(")


def test_failed_upgrade_cannot_restart_loop():
    # mark_notified before any fallible step: the daily check must not start
    # a second attempt at the same version, or a broken upgrade becomes an
    # agent that restarts every day.
    body = _adapter_method("_auto_upgrade")
    assert body.index("mark_notified(") < body.index("git_pull")
    assert body.index("mark_notified(") < body.index("request_gateway_restart(")


def test_announce_clears_the_marker_on_every_path():
    # One marker describes exactly one restart. If it can survive the
    # announce, the agent re-announces the same upgrade on every start.
    body = _adapter_method("_announce_completed_upgrade")
    assert "_pending_upgrade.clear()" in body
    # Cleared before the outcome branches, so neither branch can skip it.
    assert body.index("_pending_upgrade.clear()") < body.index("is_newer(")


def test_completion_is_announced_before_the_next_update_check_starts():
    body = _adapter_method("_connect_attempt")
    assert body.index("_announce_completed_upgrade(") < body.index(
        "_start_update_check()"
    )


def test_credential_store_marker_file_is_its_own():
    # Not folded into update_notice.json: that file's "already reminded"
    # state must survive an upgrade, while the pending marker is deleted by
    # design on the next connect.
    src = (_PKG_DIR / "credentials.py").read_text()
    assert "pending_upgrade.json" in src
    assert json  # imported for the corrupted-file test above
