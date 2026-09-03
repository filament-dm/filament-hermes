"""Tests for the chat display defaults setup seeds into config.yaml.

Hermes's own defaults suit a terminal: mid-run input interrupts and is
acknowledged, and mid-turn assistant text is relayed. Setup writes quieter
values for chat, but only where the user hasn't chosen. Loaded standalone via
AST: setup_cli's imports need Hermes.
"""

import ast
import os
from pathlib import Path

import pytest
import yaml

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)

_WANTED = (
    "PLATFORM_NAME",
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


_ns = _load()
seed_display_defaults = _ns["seed_display_defaults"]
PLATFORM_NAME = _ns["PLATFORM_NAME"]


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
