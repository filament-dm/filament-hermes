"""Tests for where `hermes filament connect` reads the token from.

Loaded standalone: the function is pure, and setup_cli's imports need Hermes.
"""

import ast
from pathlib import Path

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)


# ── token_source: where the credential is read from ──────────────────
#
# argv is convenient and the default; -p is the opt-out for a shared machine,
# because /proc/<pid>/cmdline is world-readable on Linux and shells keep history.


def _load_token_source():
    tree = ast.parse(_SETUP_CLI.read_text())
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "token_source":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns["token_source"]


token_source = _load_token_source()


def test_a_token_argument_is_used_as_given():
    assert token_source("fmcp_abc", False) == "argv"


def test_dash_p_reads_stdin():
    assert token_source("", True) == "stdin"


def test_a_missing_token_falls_back_to_stdin():
    """So a script that forgets the argument prompts rather than failing
    obscurely."""
    assert token_source("", False) == "stdin"


def test_both_is_a_conflict_not_a_silent_preference():
    """Honouring either one would leave the argv copy already in shell history,
    so say so instead of picking."""
    assert token_source("fmcp_abc", True) == "conflict"
