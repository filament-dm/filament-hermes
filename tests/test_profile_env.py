"""Tests for profile_env: each Hermes profile's copy of the plugin reads that
profile's settings, never another profile's.

A multiplexed gateway serves every profile from one process whose
``os.environ`` holds the launch profile's .env; a served profile's own values
come from the secret scope Hermes installs while the plugin registers. The
Hermes modules are stubbed here, and profile_env is loaded standalone.
"""

import contextvars
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "profile_env.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("profile_env_undertest", _PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module's string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """Stub Hermes: a launch home, a home override and a secret scope."""
    launch = tmp_path / ".hermes"
    state = {"home": launch, "scope": None}
    override = contextvars.ContextVar("override", default=None)

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(override.get() or state["home"])
    constants.get_routing_process_hermes_home = lambda: launch
    constants.hermes_home_key = lambda p: str(Path(p).resolve())
    constants.set_hermes_home_override = override.set
    constants.reset_hermes_home_override = override.reset
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.current_secret_scope = lambda: state["scope"]
    secret_scope.current_secret_scope_home = lambda: state.get("scope_home")
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setenv("FILAMENT_MCP_TOKEN", "fmcp_launch")
    state["override"] = override
    return state


def test_unpinned_reads_the_environment(monkeypatch):
    profile_env = _load()
    monkeypatch.setenv("FILAMENT_MCP_TOKEN", "fmcp_env")
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_env"
    assert profile_env.setting("FILAMENT_UNSET", "fallback") == "fallback"


def test_served_profile_never_reads_the_launch_profiles_environment(hermes):
    profile_env = _load()
    served = hermes["home"].parent / ".hermes" / "profiles" / "scout"
    hermes["home"] = served
    hermes["scope"] = {"FILAMENT_MCP_URL": "https://example.test/mcp"}
    profile_env.pin()
    hermes["home"] = served.parent.parent
    hermes["scope"] = None

    assert profile_env.setting("FILAMENT_MCP_TOKEN") is None
    assert profile_env.setting("FILAMENT_MCP_URL") == "https://example.test/mcp"
    assert profile_env.hermes_home() == served
    assert profile_env.profile_name() == "scout"


def test_served_profile_reads_its_own_token(hermes):
    profile_env = _load()
    hermes["home"] = hermes["home"] / "profiles" / "scout"
    hermes["scope"] = {"FILAMENT_MCP_TOKEN": "fmcp_scout"}
    profile_env.pin()
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_scout"


def test_launch_profile_reads_the_environment_live(hermes, monkeypatch):
    profile_env = _load()
    profile_env.pin()
    monkeypatch.setenv("FILAMENT_MCP_TOKEN", "fmcp_rotated")
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_rotated"
    assert profile_env.profile_name() == "default"


def test_set_setting_stays_out_of_a_shared_environment(hermes, monkeypatch):
    monkeypatch.delenv("FILAMENT_HOME_ROOM", raising=False)
    profile_env = _load()
    hermes["home"] = hermes["home"] / "profiles" / "scout"
    hermes["scope"] = {}
    profile_env.pin()
    profile_env.set_setting("FILAMENT_HOME_ROOM", "!room")
    assert profile_env.setting("FILAMENT_HOME_ROOM") == "!room"
    assert "FILAMENT_HOME_ROOM" not in os.environ


def test_home_scope_points_hermes_at_the_profile(hermes):
    profile_env = _load()
    served = hermes["home"] / "profiles" / "scout"
    hermes["home"] = served
    profile_env.pin()
    hermes["home"] = served.parent.parent
    constants = sys.modules["hermes_constants"]
    with profile_env.home_scope():
        assert constants.get_hermes_home() == served
    assert constants.get_hermes_home() == served.parent.parent


def test_without_profile_scopes_reads_the_environment(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setenv("FILAMENT_MCP_TOKEN", "fmcp_env")
    profile_env = _load()
    profile_env.pin()
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_env"


def test_a_later_setup_reaches_the_profile_through_its_scope(hermes):
    profile_env = _load()
    served = hermes["home"] / "profiles" / "scout"
    hermes["home"] = served
    hermes["scope"] = {}
    profile_env.pin()
    assert profile_env.setting("FILAMENT_MCP_TOKEN") is None

    # Another profile's scope teaches it nothing.
    hermes["scope"] = {"FILAMENT_MCP_TOKEN": "fmcp_other"}
    hermes["scope_home"] = str(served.parent / "other")
    assert profile_env.setting("FILAMENT_MCP_TOKEN") is None

    # Its own scope, as the gateway installs to start its adapter after setup.
    hermes["scope"] = {"FILAMENT_MCP_TOKEN": "fmcp_scout"}
    hermes["scope_home"] = str(served)
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_scout"
    hermes["scope"], hermes["scope_home"] = None, None
    assert profile_env.setting("FILAMENT_MCP_TOKEN") == "fmcp_scout"
