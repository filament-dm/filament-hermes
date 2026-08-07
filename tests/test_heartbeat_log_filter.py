"""Tests for the firebase-messaging heartbeat log filter.

The plugin turns that library's logging up to DEBUG, because everything it says
about connection state — connecting, reconnecting, resetting — is logged there.
The heartbeat sits at the same level and repeats every 20 seconds forever, so on
a healthy client it buries the lines worth reading and lands on the console of
anyone running the gateway in the foreground.

Filtering it is only safe if it drops the heartbeat and nothing else, hence these
tests. `fcm_client` needs Hermes to import, so the filter is lifted out of the
source the way the other tests here do it.
"""

import ast
import logging
import os
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "fcm_client.py"

_WANTED = ("_DropHeartbeats", "_HEARTBEAT_MARKERS")


def _load():
    ns: dict = {"logging": logging, "os": os}
    for node in ast.parse(_SRC.read_text()).body:
        keep = (isinstance(node, ast.ClassDef) and node.name in _WANTED) or (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") in _WANTED for t in node.targets)
        )
        if keep:
            exec(compile(ast.Module([node], []), str(_SRC), "exec"), ns)
    return ns


_ns = _load()
_filter = _ns["_DropHeartbeats"]()


def _record(msg: str) -> logging.LogRecord:
    # msg is the format string, which is what the filter inspects.
    return logging.LogRecord("firebase_messaging", logging.DEBUG, "f", 1, msg, (), None)


# ── the noise goes ───────────────────────────────────────────────────

# Verbatim format strings from vendor/firebase_messaging/fcmpushclient.py.
HEARTBEATS = (
    "Sent heartbeat ping",
    "Received heartbeat ack: %s",
    "Received heartbeat ping, sending ack: Stream ID: %s, Last: %s, Status: %s",
)


@pytest.mark.parametrize("msg", HEARTBEATS)
def test_heartbeat_records_are_dropped(msg):
    assert _filter.filter(_record(msg)) is False


# ── everything else stays ────────────────────────────────────────────

# The reason the level is DEBUG at all. Losing these would defeat the point.
CONNECTION_STATE = (
    "Connected to MCS endpoint (%s,%s)",
    "Re-connected to ssl socket",
    "Reestablishing connection",
    "Resetting connection",
    "%ss since last reset attempt.",
)


@pytest.mark.parametrize("msg", CONNECTION_STATE)
def test_connection_state_survives(msg):
    assert _filter.filter(_record(msg)) is True


def test_an_error_is_never_dropped():
    assert _filter.filter(_record("Unknown error: %s, shutting down FcmPushClient."))


def test_matches_the_format_string_not_the_arguments():
    # A payload that happens to contain the word must not disappear.
    rec = logging.LogRecord(
        "firebase_messaging",
        logging.DEBUG,
        "f",
        1,
        "Received message: %s",
        ("heartbeat ping",),
        None,
    )
    assert _filter.filter(rec) is True
