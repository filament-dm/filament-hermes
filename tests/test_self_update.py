"""Tests for self_update.py — the auto-update mechanics.

Modules are loaded standalone (same pattern as the other test files) so a
bare dev environment without Hermes works. Every subprocess goes through
self_update._run, which these tests replace — no real git or hermes CLI is
ever invoked.
"""

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

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


self_update = _load("self_update")


# ── Fixture helpers ──────────────────────────────────────────────────


def _pyproject(version: str) -> str:
    return f'[project]\nname = "x"\nversion = "{version}"\n'


def _tree(tmp_path, version="0.1.0", plugin_id="filament", with_git=True):
    """A fake installed directory plugin under a fake HERMES_HOME."""
    home = tmp_path / "hermes"
    root = home / "plugins" / plugin_id
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(_pyproject(version))
    if with_git:
        (root / ".git").mkdir()
    return root, home


class FakeRun:
    """Stands in for self_update._run; scripted per-command results.

    ``on_update`` runs when `hermes plugins update` is invoked (e.g. to
    rewrite pyproject.toml like a real pull would) and returns
    (returncode, stdout).
    """

    def __init__(self, branch="main", status="", on_update=None):
        self.calls = []
        self.branch = branch
        self.status = status
        self.on_update = on_update or (lambda: (0, "✓ Plugin filament updated."))

    def __call__(self, args, cwd=None, timeout=60):
        self.calls.append(list(args))
        if args[0] == "git" and "rev-parse" in args:
            return SimpleNamespace(returncode=0, stdout=f"{self.branch}\n", stderr="")
        if args[0] == "git" and "status" in args:
            return SimpleNamespace(returncode=0, stdout=self.status, stderr="")
        if args[:3] == ["hermes", "plugins", "update"]:
            code, out = self.on_update()
            return SimpleNamespace(returncode=code, stdout=out, stderr="")
        raise AssertionError(f"unexpected command: {args}")

    @property
    def update_calls(self):
        return [c for c in self.calls if c[:3] == ["hermes", "plugins", "update"]]


def _attempt(root, home, latest="0.2.0", current="0.1.0"):
    return self_update.attempt_update(latest, current, root=root, home=home)


# ── Eligibility gates ────────────────────────────────────────────────


def test_not_a_git_clone_is_skipped(tmp_path, monkeypatch):
    root, home = _tree(tmp_path, with_git=False)
    monkeypatch.setattr(self_update, "_run", FakeRun())
    outcome = _attempt(root, home)
    assert outcome.status == self_update.SKIPPED
    assert "git clone" in outcome.reason


def test_tree_outside_plugins_dir_is_skipped(tmp_path, monkeypatch):
    # A dev checkout (or a PYTHONPATH install) must never be auto-updated,
    # and `hermes plugins update` would pull a *different* tree anyway.
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text(_pyproject("0.1.0"))
    (root / ".git").mkdir()
    monkeypatch.setattr(self_update, "_run", FakeRun())
    outcome = _attempt(root, tmp_path / "hermes")
    assert outcome.status == self_update.SKIPPED
    assert "directory plugin" in outcome.reason


def test_non_main_branch_is_skipped(tmp_path, monkeypatch):
    # A FILAMENT_FCM_REF install (test/dev pin) checks out another branch —
    # auto-update must not fight the pin.
    root, home = _tree(tmp_path)
    fake = FakeRun(branch="jon/experiment")
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.SKIPPED
    assert "jon/experiment" in outcome.reason
    assert not fake.update_calls


def test_dirty_tree_is_skipped(tmp_path, monkeypatch):
    # `hermes plugins update` autostashes and can reset --hard on conflict;
    # local edits mean a human should drive the update.
    root, home = _tree(tmp_path)
    fake = FakeRun(status=" M adapter.py\n")
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.SKIPPED
    assert "local changes" in outcome.reason
    assert not fake.update_calls


def test_missing_git_binary_is_skipped(tmp_path, monkeypatch):
    root, home = _tree(tmp_path)

    def no_git(args, cwd=None, timeout=60):
        raise FileNotFoundError("git")

    monkeypatch.setattr(self_update, "_run", no_git)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.SKIPPED
    assert "git" in outcome.reason


# ── attempt_update outcomes ──────────────────────────────────────────


def test_version_already_on_disk_skips_the_pull(tmp_path, monkeypatch):
    # A previous cycle pulled but the restart never took: nothing to pull,
    # the caller only needs to restart.
    root, home = _tree(tmp_path, version="0.2.0")
    fake = FakeRun()
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home, latest="0.2.0", current="0.1.0")
    assert outcome.status == self_update.UPDATED
    assert outcome.pulled is False
    assert outcome.disk_version == "0.2.0"
    assert not fake.update_calls


def test_successful_update(tmp_path, monkeypatch):
    root, home = _tree(tmp_path, version="0.1.0")

    def pull():
        (root / "pyproject.toml").write_text(_pyproject("0.2.0"))
        return (0, "✓ Plugin filament updated.")

    fake = FakeRun(on_update=pull)
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.UPDATED
    assert outcome.pulled is True
    assert outcome.disk_version == "0.2.0"
    # The plugin id passed to hermes is the directory name, not a constant —
    # legacy installs live under a different id.
    assert fake.update_calls == [["hermes", "plugins", "update", "filament"]]


def test_update_uses_the_directory_name_as_plugin_id(tmp_path, monkeypatch):
    root, home = _tree(tmp_path, plugin_id="filament-fcm")

    def pull():
        (root / "pyproject.toml").write_text(_pyproject("0.2.0"))
        return (0, "updated")

    fake = FakeRun(on_update=pull)
    monkeypatch.setattr(self_update, "_run", fake)
    assert _attempt(root, home).status == self_update.UPDATED
    assert fake.update_calls == [["hermes", "plugins", "update", "filament-fcm"]]


def test_update_command_failure(tmp_path, monkeypatch):
    root, home = _tree(tmp_path)
    fake = FakeRun(on_update=lambda: (1, "error: git pull failed"))
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.FAILED
    assert "git pull failed" in outcome.reason


def test_scan_disabled_verdict(tmp_path, monkeypatch):
    # Exit code 0 but the plugin left the enabled set: the one outcome the
    # caller must NOT restart into.
    root, home = _tree(tmp_path)

    def pull():
        (root / "pyproject.toml").write_text(_pyproject("0.2.0"))
        return (
            0,
            "Plugin 'filament' has been disabled. Review the findings, then "
            "re-enable with `hermes plugins enable filament` if you trust them.",
        )

    monkeypatch.setattr(self_update, "_run", FakeRun(on_update=pull))
    outcome = _attempt(root, home)
    assert outcome.status == self_update.DISABLED
    assert outcome.disk_version == "0.2.0"


def test_pull_that_does_not_reach_latest_fails(tmp_path, monkeypatch):
    # e.g. origin is a fork whose main doesn't hold the announced version:
    # exit 0, "already up to date", version unchanged → reminder fallback.
    root, home = _tree(tmp_path, version="0.1.0")
    fake = FakeRun(on_update=lambda: (0, "Plugin filament is already up to date."))
    monkeypatch.setattr(self_update, "_run", fake)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.FAILED
    assert "0.1.0" in outcome.reason and "0.2.0" in outcome.reason


def test_missing_hermes_cli_fails(tmp_path, monkeypatch):
    root, home = _tree(tmp_path)
    calls = FakeRun()

    def run(args, cwd=None, timeout=60):
        if args[0] == "hermes":
            raise FileNotFoundError("hermes")
        return calls(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(self_update, "_run", run)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.FAILED
    assert "hermes CLI" in outcome.reason


def test_attempt_update_never_raises(tmp_path, monkeypatch):
    root, home = _tree(tmp_path)

    def boom(args, cwd=None, timeout=60):
        if args[0] == "hermes":
            raise subprocess.TimeoutExpired(args, timeout)
        return FakeRun()(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(self_update, "_run", boom)
    outcome = _attempt(root, home)
    assert outcome.status == self_update.FAILED
    assert "TimeoutExpired" in outcome.reason


# ── env kill switch ──────────────────────────────────────────────────


def test_auto_update_disabled_env(monkeypatch):
    monkeypatch.delenv("FILAMENT_DISABLE_AUTO_UPDATE", raising=False)
    assert not self_update.auto_update_disabled()
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("FILAMENT_DISABLE_AUTO_UPDATE", val)
        assert self_update.auto_update_disabled()
    monkeypatch.setenv("FILAMENT_DISABLE_AUTO_UPDATE", "false")
    assert not self_update.auto_update_disabled()


# ── spawn_gateway_restart ────────────────────────────────────────────


def test_restart_spawn_scrubs_the_gateway_guard_env(monkeypatch):
    # `hermes gateway restart` refuses to run as a child of a supervised
    # gateway, keyed on _HERMES_GATEWAY — the spawn must scrub exactly that
    # variable (and nothing else, or restart routing breaks) and detach.
    seen = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_HOME", "/tmp/somewhere")

    assert self_update.spawn_gateway_restart() is True
    assert seen["args"] == ["hermes", "gateway", "restart"]
    assert seen["kwargs"]["start_new_session"] is True
    env = seen["kwargs"]["env"]
    assert "_HERMES_GATEWAY" not in env
    assert env["HERMES_HOME"] == "/tmp/somewhere"


def test_restart_spawn_failure_returns_false(monkeypatch):
    def fake_popen(args, **kwargs):
        raise FileNotFoundError("hermes")

    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)
    assert self_update.spawn_gateway_restart() is False


# ── disk_version ─────────────────────────────────────────────────────


def test_disk_version_reads_pyproject(tmp_path):
    root, _ = _tree(tmp_path, version="1.2.3")
    assert self_update.disk_version(root) == "1.2.3"
    assert self_update.disk_version(tmp_path / "nowhere") is None
