"""Tests for the control-plane user set (`merge_control_users`).

FILAMENT_CONTROL_USERS is the platform's allowed_users_env: everyone listed can
command the agent. So both failure directions are real — dropping a teammate
silently revokes their access, and keeping a stale entry silently leaves a former
owner in command. `hermes filament connect` is the reconnect path and runs with
no human to review the list, so the rule has to be right without supervision.

Loaded standalone: the function is pure, and setup_cli's imports need Hermes.
"""

import ast
from pathlib import Path

import pytest

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)

OWNER = "@alice:filament.dm"
OTHER_OWNER = "@bob:filament.dm"
MATE = "@carol:filament.dm"
MATE2 = "@dave:filament.dm"


def _load():
    tree = ast.parse(_SETUP_CLI.read_text())
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "merge_control_users":
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns["merge_control_users"]


merge_control_users = _load()


# ── the principal is always present, and always first ────────────────


def test_no_prior_list():
    assert merge_control_users("", OWNER) == [OWNER]


def test_principal_comes_first():
    """The order is load-bearing: the first entry is how the next run recognises
    which owner the list belongs to."""
    assert merge_control_users(f"{OWNER},{MATE}", OWNER)[0] == OWNER


# ── same owner: teammates are kept ───────────────────────────────────


def test_extras_survive_a_reconnect():
    """The reported bug: a reconnect used to write the principal alone, revoking
    every teammate the owner had granted."""
    assert merge_control_users(f"{OWNER},{MATE},{MATE2}", OWNER) == [
        OWNER,
        MATE,
        MATE2,
    ]


def test_principal_only_list_is_unchanged():
    assert merge_control_users(OWNER, OWNER) == [OWNER]


def test_whitespace_and_empty_segments_are_tolerated():
    assert merge_control_users(f" {OWNER} , {MATE} ,, ", OWNER) == [OWNER, MATE]


def test_duplicates_collapse():
    assert merge_control_users(f"{OWNER},{MATE},{MATE}", OWNER) == [OWNER, MATE]


# ── owner changed: the old list is not this owner's ───────────────────


def test_a_different_owner_resets_the_set():
    """Reconnecting with another owner's token. Keeping the list would leave the
    previous owner — first entry here — able to command the agent."""
    assert merge_control_users(f"{OTHER_OWNER},{MATE}", OWNER) == [OWNER]


def test_the_previous_owner_is_never_carried_over():
    result = merge_control_users(f"{OTHER_OWNER},{MATE}", OWNER)
    assert OTHER_OWNER not in result


def test_principal_present_but_not_first_is_treated_as_changed():
    """A list this code did not write (hand-edited, or from an older version).
    Its first entry is not a principal we can trust, so fail closed."""
    assert merge_control_users(f"{MATE},{OWNER}", OWNER) == [OWNER]


@pytest.mark.parametrize("prior", [",", "  ", ",,,"])
def test_junk_prior_yields_just_the_principal(prior):
    assert merge_control_users(prior, OWNER) == [OWNER]


def test_similar_ids_are_not_confused():
    """Exact match only — a prefix rule would treat a different account on the
    same homeserver as the same owner."""
    lookalike = OWNER.replace("@alice", "@alice2")
    assert merge_control_users(f"{lookalike},{MATE}", OWNER) == [OWNER]


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
