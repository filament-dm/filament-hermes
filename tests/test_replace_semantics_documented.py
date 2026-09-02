"""Tools whose write replaces the whole document say so.

``set_wake_policy`` and ``set_capabilities`` both take a complete object and
overwrite the stored one. Their stores merge a read over defaults, so a
partial save does not fail - it reverts every key the model left out, and
nothing reports it. "Also wake on the bug emoji" is the shape that hits it.

The only guard against that is the tool description telling the model to
read the current object first and save the whole edited result, so these
assert the instruction is there. The registrations are read out of the
source with ``ast``: importing the package pulls in Hermes, absent in a
bare test env, and only the description strings are under test.
"""

import ast
from pathlib import Path

_INIT = Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "__init__.py"


def _descriptions() -> dict[str, str]:
    """Every ``_reg(name, description, ...)`` in the plugin, by tool name."""
    out = {}
    for node in ast.walk(ast.parse(_INIT.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "_reg" or len(node.args) < 2:
            continue
        name, description = node.args[0], node.args[1]
        if isinstance(name.value, str) and isinstance(description.value, str):
            out[name.value] = description.value
    return out


def test_both_whole_document_setters_ask_for_a_read_first():
    descriptions = _descriptions()
    for setter, getter in (
        ("set_wake_policy", "get_wake_policy"),
        ("set_capabilities", "get_capabilities"),
    ):
        text = descriptions[setter]
        assert "CONVERSATIONALLY" in text, setter
        assert getter in text, setter


def test_wake_policy_names_the_cost_of_omitting_a_key():
    # Not just "send the whole object" - what happens if you don't.
    text = _descriptions()["set_wake_policy"]
    assert "REPLACES" in text
    assert "reverts to its default" in text


def test_getters_point_back_at_their_setter():
    descriptions = _descriptions()
    assert "set_wake_policy" in descriptions["get_wake_policy"]
    assert "set_capabilities" in descriptions["get_capabilities"]
