import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "reactive",
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "reactive.py",
)
reactive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reactive)
assessment_note = reactive.assessment_note


def test_no_assessment_yields_empty():
    assert assessment_note(None) == ""
    assert assessment_note({"event_id": "$e", "body": "hi", "is_from_principal": True}) == ""


def test_addressed_and_reply_expected():
    row = {"is_implicitly_mentioned": True, "reply_expected": True}
    assert assessment_note(row) == (
        "moderator: reads this message as addressed to you (no @-mention); "
        "a reply is expected."
    )


def test_not_addressed_no_reply():
    row = {"is_implicitly_mentioned": False, "reply_expected": False}
    assert assessment_note(row) == (
        "moderator: does not read this message as addressed to you; "
        "no reply is expected."
    )
