"""Tests for the per-profile state directory resolution and migration.

``default_state_dir`` keys the plugin state off HERMES_HOME so every Hermes
profile — each its own HERMES_HOME — gets its own FCM identity and stores.
These guard the resolution precedence, the one-time migration of the legacy
``~/.hermes/filament-fcm`` directory (root profile only — a named profile
must never adopt the root profile's identity), and that ``reactive``'s
mirrored resolution agrees with ``credentials``'s.

``credentials.py`` and ``reactive.py`` are pure-stdlib, so we load them
standalone — importing the package triggers ``__init__`` → the Hermes
``gateway`` package, which isn't present in a bare test environment.
"""

import importlib.util
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _BASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


credentials = _load("credentials")
reactive = _load("reactive")


def test_override_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path / "override"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert credentials.default_state_dir() == tmp_path / "override"


def test_hermes_home_unset_falls_back_to_legacy_path(tmp_path, monkeypatch):
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert credentials.default_state_dir() == tmp_path / ".hermes" / "filament-fcm"


def test_hermes_home_set_uses_per_home_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    assert credentials.default_state_dir() == tmp_path / "data" / "filament-fcm"


def test_root_profile_migrates_legacy_dir(tmp_path, monkeypatch):
    """An upgraded root-profile install keeps its FCM identity: the legacy
    ``~/.hermes/filament-fcm`` is renamed into ``$HERMES_HOME/filament-fcm``."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "fcm_credentials.json").write_text("{}")

    resolved = credentials.default_state_dir()

    assert resolved == tmp_path / "data" / "filament-fcm"
    assert (resolved / "fcm_credentials.json").exists()
    assert not legacy.exists()


def test_migration_skipped_when_target_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "fcm_credentials.json").write_text('{"who": "legacy"}')
    target = tmp_path / "data" / "filament-fcm"
    target.mkdir(parents=True)
    (target / "fcm_credentials.json").write_text('{"who": "current"}')

    resolved = credentials.default_state_dir()

    assert resolved == target
    assert (target / "fcm_credentials.json").read_text() == '{"who": "current"}'
    assert legacy.exists()


def test_named_profile_never_adopts_legacy_identity(tmp_path, monkeypatch):
    """A HERMES_HOME under profiles/ is a different agent: it must register
    fresh, not steal the root profile's FCM registration and instructions."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    profile_home = tmp_path / ".hermes" / "profiles" / "scout"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "fcm_credentials.json").write_text("{}")

    resolved = credentials.default_state_dir()

    assert resolved == profile_home / "filament-fcm"
    assert legacy.exists()
    assert not resolved.exists()


def test_reactive_resolution_matches_credentials(tmp_path, monkeypatch):
    """reactive._default_dir mirrors the resolution rules (sans migration)."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    for home in [None, str(tmp_path / "data"), str(tmp_path / ".hermes/profiles/x")]:
        if home is None:
            monkeypatch.delenv("HERMES_HOME", raising=False)
        else:
            monkeypatch.setenv("HERMES_HOME", home)
        assert reactive._default_dir() == credentials.default_state_dir()
    monkeypatch.setenv("FILAMENT_FCM_CREDENTIALS_DIR", str(tmp_path / "o"))
    assert reactive._default_dir() == credentials.default_state_dir()


def test_credential_store_uses_resolved_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    store = credentials.CredentialStore()
    store.save_fcm_credentials({"gcm": {"token": "t"}})
    expected = tmp_path / "data" / "filament-fcm" / "fcm_credentials.json"
    assert expected.exists()


def test_failed_migration_stays_on_legacy_and_reactive_converges(tmp_path, monkeypatch):
    """A cross-device rename (docker bind mounts) must not split the state:
    credentials stays on the legacy path, and reactive's stores resolve to
    the same place — not to an empty $HERMES_HOME dir that silently feeds
    the agent default instructions."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "instructions.md").write_text("act on requests")

    def _cross_device(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(credentials.os, "replace", _cross_device)

    resolved = credentials.default_state_dir()

    assert resolved == legacy
    assert (resolved / "instructions.md").exists()
    assert reactive._default_dir() == legacy


def test_successful_migration_leaves_a_marker(tmp_path, monkeypatch):
    """The move is a one-way door: a breadcrumb next to the old path records
    where the state went, and later resolutions are not confused by it."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "fcm_credentials.json").write_text("{}")

    resolved = credentials.default_state_dir()

    marker = tmp_path / ".hermes" / "filament-fcm.moved"
    assert marker.exists()
    assert str(resolved) in marker.read_text()
    assert credentials.default_state_dir() == resolved
    assert reactive._default_dir() == resolved


def test_store_constructed_before_migration_follows_the_move(tmp_path, monkeypatch):
    """The adapter builds its reactive stores before CredentialStore runs the
    one-time migration. Store paths resolve per access, so instructions
    written pre-upgrade are still found after the directory moves."""
    monkeypatch.delenv("FILAMENT_FCM_CREDENTIALS_DIR", raising=False)
    monkeypatch.delenv("FILAMENT_INSTRUCTIONS_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    legacy = tmp_path / ".hermes" / "filament-fcm"
    legacy.mkdir(parents=True)
    (legacy / "instructions.md").write_text("answer in haiku")

    store = reactive.InstructionsStore()
    assert store.path == legacy / "instructions.md"

    migrated = credentials.default_state_dir()

    assert migrated == tmp_path / "data" / "filament-fcm"
    assert store.path == migrated / "instructions.md"
    assert store.path.read_text() == "answer in haiku"
