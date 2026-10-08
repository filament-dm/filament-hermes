"""Tests for the chat display defaults setup seeds into config.yaml.

Hermes's own defaults suit a terminal: mid-run input interrupts and is
acknowledged, and mid-turn assistant text is relayed. Setup writes quieter
values for chat, but only where the user hasn't chosen. Loaded standalone via
AST: setup_cli's imports need Hermes.
"""

import ast
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

_SETUP_CLI = Path(__file__).resolve().parent.parent / "filament" / "setup_cli.py"

_WANTED = (
    "_BUSY_DEFAULTS",
    "_PLATFORM_DISPLAY_DEFAULTS",
    "_find_hermes_home",
    "_subdict",
    "seed_display_defaults",
)


def _load():
    tree = ast.parse(_SETUP_CLI.read_text())
    ns: dict = {
        "os": os,
        "Path": Path,
        "yaml": yaml,
        "print_info": lambda *a, **k: None,
    }
    for node in tree.body:
        keep = (isinstance(node, ast.FunctionDef) and node.name in _WANTED) or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in _WANTED for t in node.targets)
        )
        if keep:
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns


_spec = importlib.util.spec_from_file_location(
    "filament_naming", _SETUP_CLI.parent / "naming.py"
)
naming = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(naming)
_ns = _load()
_ns["platform_name"] = naming.platform_name
seed_display_defaults = _ns["seed_display_defaults"]
PLATFORM_NAME = naming.PLATFORM_NAME


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path / "config.yaml"


def _read(path):
    return yaml.safe_load(path.read_text())


def test_missing_config_is_created_with_all_defaults(config_path):
    seed_display_defaults()
    display = _read(config_path)["display"]
    assert display["busy_input_mode"] == "queue"
    assert display["busy_ack_enabled"] is False
    assert display["platforms"][PLATFORM_NAME]["interim_assistant_messages"] is False
    assert display["platforms"][PLATFORM_NAME]["show_reasoning"] is False


def test_global_show_reasoning_is_overridden_for_filament_only(config_path):
    # Some hosted images (Nous cloud) turn reasoning on globally. The global
    # value stays — other platforms keep it — and ours gets a per-platform off,
    # which Hermes resolves ahead of the global one.
    config_path.write_text("display:\n  show_reasoning: true\n")
    seed_display_defaults()
    display = _read(config_path)["display"]
    assert display["show_reasoning"] is True
    assert display["platforms"][PLATFORM_NAME]["show_reasoning"] is False


def test_explicit_filament_reasoning_choice_survives(config_path):
    # Hermes's /reasoning command writes exactly this key.
    config_path.write_text(
        f"display:\n  platforms:\n    {PLATFORM_NAME}:\n      show_reasoning: true\n"
    )
    seed_display_defaults()
    assert (
        _read(config_path)["display"]["platforms"][PLATFORM_NAME]["show_reasoning"]
        is True
    )


def test_returns_the_keys_it_wrote(config_path):
    ours = f"display.platforms.{PLATFORM_NAME}"
    config_path.write_text(
        "display:\n  busy_input_mode: interrupt\n  busy_ack_enabled: false\n"
        f"  platforms:\n    {PLATFORM_NAME}:\n      interim_assistant_messages: false\n"
    )
    assert seed_display_defaults(announce=False) == [f"{ours}.show_reasoning"]
    assert seed_display_defaults(announce=False) == []


def test_other_sections_are_kept(config_path):
    config_path.write_text("plugins:\n  enabled:\n  - filament\nmodel: x\n")
    seed_display_defaults()
    config = _read(config_path)
    assert config["plugins"] == {"enabled": ["filament"]}
    assert config["model"] == "x"
    assert "display" in config


def test_explicit_choices_survive(config_path):
    config_path.write_text(
        "display:\n"
        "  busy_input_mode: interrupt\n"
        "  compact: true\n"
        "  platforms:\n"
        f"    {PLATFORM_NAME}:\n"
        "      interim_assistant_messages: true\n"
        "    telegram:\n"
        "      tool_progress: minimal\n"
    )
    seed_display_defaults()
    display = _read(config_path)["display"]
    assert display["busy_input_mode"] == "interrupt"
    assert display["busy_ack_enabled"] is False
    assert display["compact"] is True
    assert display["platforms"][PLATFORM_NAME]["interim_assistant_messages"] is True
    assert display["platforms"]["telegram"] == {"tool_progress": "minimal"}


def test_nothing_missing_leaves_the_file_untouched(config_path):
    seed_display_defaults()
    before = config_path.read_text()
    os.utime(config_path, (0, 0))
    seed_display_defaults()
    assert config_path.read_text() == before
    assert config_path.stat().st_mtime == 0


def test_empty_display_section_is_filled(config_path):
    config_path.write_text("display:\n")
    seed_display_defaults()
    assert _read(config_path)["display"]["busy_input_mode"] == "queue"


@pytest.mark.parametrize("section", ["platforms", "display.platforms"])
def test_legacy_platform_keeps_sessions_and_explicit_choices(config_path, section):
    legacy = naming.LEGACY_PLATFORM_NAME
    config = {"platforms": {legacy: {"enabled": True, "extra": {"custom": True}}}}
    if section == "display.platforms":
        config = {"display": {"platforms": {legacy: {"show_reasoning": True}}}}
    config_path.write_text(yaml.safe_dump(config))
    assert naming.platform_name() == legacy
    seed_display_defaults()
    saved = _read(config_path)
    assert PLATFORM_NAME not in saved["display"]["platforms"]
    if section == "platforms":
        assert saved["platforms"] == config["platforms"]
    else:
        assert saved["display"]["platforms"][legacy]["show_reasoning"] is True


def test_named_profile_does_not_inherit_root_platform(config_path, monkeypatch):
    root = config_path.parent
    monkeypatch.setenv("HOME", str(root))
    (root / ".hermes" / naming.LEGACY_PLATFORM_NAME).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / ".hermes/profiles/new-agent"))
    assert naming.platform_name() == PLATFORM_NAME


def test_legacy_state_selects_legacy_platform(config_path):
    (config_path.parent / naming.LEGACY_PLATFORM_NAME).mkdir()
    assert naming.platform_name() == naming.LEGACY_PLATFORM_NAME
