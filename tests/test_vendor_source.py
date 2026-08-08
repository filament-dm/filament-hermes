"""Tests that vendor/ really holds our firebase-messaging fork.

The fork keeps upstream's version number, so nothing about the version tells a
patched tree from a stock one. Three places name the commit — vendor-deps.sh,
pyproject.toml and the marker the script writes — and a rebuild that reaches
PyPI instead would look completely normal while silently undoing the fix.

The fix matters more than most: upstream sliced the Crypto-Key and Encryption
headers by label length rather than parsing them, and once CPython gh-145264
stopped the base64 decoder discarding data after the first padded quad, every
push raised "Incorrect padding". The client treats an error from a payload as
fatal, so a single bad push took the receiver down and no notification arrived
again. A silent revert to PyPI is an agent that connects and never wakes.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "vendor-deps.sh"
_PYPROJECT = _ROOT / "pyproject.toml"
_MARKER = _ROOT / "vendor" / "FIREBASE_MESSAGING_SOURCE"
_CLIENT = _ROOT / "vendor" / "firebase_messaging" / "fcmpushclient.py"

_FORK = "github.com/filament-dm/firebase-messaging"


def _script_commit() -> str:
    m = re.search(
        r"^FIREBASE_MESSAGING_COMMIT=([0-9a-f]{40})$", _SCRIPT.read_text(), re.M
    )
    assert m, "vendor-deps.sh must pin FIREBASE_MESSAGING_COMMIT to a full sha"
    return m.group(1)


def _pyproject_commit() -> str:
    m = re.search(rf"{re.escape(_FORK)}\.git@([0-9a-f]{{40}})", _PYPROJECT.read_text())
    assert m, "pyproject.toml must depend on the fork at a full sha"
    return m.group(1)


# ── the three places agree ───────────────────────────────────────────


def test_script_and_pyproject_pin_the_same_commit():
    # Otherwise the tests run one build of the library and the plugin ships
    # another, which is how this class of bug stays invisible.
    assert _script_commit() == _pyproject_commit()


def test_marker_records_what_was_vendored():
    text = _MARKER.read_text()
    assert _FORK in text
    assert _script_commit() in text


def test_marker_names_the_branch_to_track():
    assert "filament/integration" in _MARKER.read_text()


# ── the vendored tree is actually the fork ───────────────────────────


def test_vendored_client_parses_headers_instead_of_slicing():
    src = _CLIENT.read_text()
    assert "_header_param(" in src, "vendor/ is not the fork — rebuild it"


def test_vendored_client_has_no_length_slicing_left():
    src = _CLIENT.read_text()
    # The exact upstream expressions the fix removed.
    assert '"crypto-key")[3:]' not in src
    assert '"encryption")[5:]' not in src


def test_vendored_client_restores_base64_padding():
    # The specs omit "=" padding; without restoring it the decode raises on a
    # value that is perfectly valid.
    assert "_urlsafe_b64decode_padded(" in _CLIENT.read_text()


def test_one_bad_payload_does_not_kill_the_receiver():
    # The isolation half of the fix. Without it a single undecryptable push
    # stops every later notification, which is indistinguishable from a dead
    # agent.
    src = _CLIENT.read_text()
    assert "_handle_data_message" in src
    body = src.split("def _handle_data_message", 1)[1].split("\n    def ", 1)[0]
    assert "except ValueError" in body, (
        "_handle_data_message must swallow a bad payload rather than let it "
        "reach _listen() and shut the client down"
    )
