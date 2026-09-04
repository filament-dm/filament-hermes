"""Tests for the connect-token exchange at setup time (ENG-893).

The pasted connect token has been through shell history and the process
environment, so right after validation setup swaps it for a fresh bearer via
the server's token-exchange grant and persists the replacement. A definitive
refusal keeps the pasted token, so setup still completes against a server that
predates the exchange. An ambiguous outcome — the exchange may have consumed
the paste without the reply landing — probes the paste and returns ``None``
when neither credential survived, so the caller aborts instead of persisting a
dead token.

Loaded standalone, AST-style like test_token_source: setup_cli's module-level
imports need Hermes, but the functions under test only touch httpx and the
print helpers, which we stub.
"""

import ast
import types
from pathlib import Path

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)

_WANTED_FUNCS = {"_exchange_connect_token", "_pasted_token_if_alive"}
_WANTED_ASSIGNS = {"_TOKEN_EXCHANGE_GRANT", "_ACCESS_TOKEN_TYPE"}


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeHttpx(types.SimpleNamespace):
    """Plays back one scripted result per POST, recording each call.

    A result that is an Exception instance is raised; anything else is
    returned. The nested ConnectError mirrors httpx's, which the code under
    test catches by name to recognize a request that never left the machine.
    """

    class ConnectError(Exception):
        pass

    def __init__(self, *results):
        super().__init__()
        self._results = list(results)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _load_exchange(fake_httpx):
    tree = ast.parse(_SETUP_CLI.read_text())
    nodes = []
    for node in tree.body:
        is_wanted_def = isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS
        is_wanted_assign = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _WANTED_ASSIGNS for t in node.targets
        )
        if is_wanted_def or is_wanted_assign:
            nodes.append(node)
    ns = {
        "httpx": fake_httpx,
        "print_info": lambda *a, **k: None,
        "print_warning": lambda *a, **k: None,
        "version_headers": lambda: {},
    }
    exec(compile(ast.Module(nodes, []), str(_SETUP_CLI), "exec"), ns)
    return ns["_exchange_connect_token"]


URL = "https://api.filament.dm/mcp/agents"


def test_success_returns_the_fresh_token():
    fake = _FakeHttpx(_Response(200, {"access_token": "fmcp_fresh"}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_fresh"

    # One POST, to the token endpoint, with the RFC 8693 form fields and the
    # pasted token as the subject.
    ((url, kwargs),) = fake.calls
    assert url == f"{URL}/oauth/token"
    data = kwargs["data"]
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert data["subject_token"] == "fmcp_pasted"
    assert data["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"


def test_old_server_rejection_keeps_the_pasted_token():
    """A server without the exchange 401s the fmcp_ subject and consumes
    nothing — setup must proceed with the token it validated, no probe."""
    fake = _FakeHttpx(_Response(401, {"error": "invalid_grant"}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"
    assert len(fake.calls) == 1


def test_connection_never_made_keeps_the_pasted_token():
    """A ConnectError means the request never reached a server, so nothing
    was consumed and no probe is needed."""
    fake = _FakeHttpx(_FakeHttpx.ConnectError("connection refused"))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"
    assert len(fake.calls) == 1


def test_lost_reply_with_live_paste_keeps_it():
    """The exchange request timed out but the probe shows the paste still
    authenticates: the exchange never went through, keep the paste."""
    fake = _FakeHttpx(OSError("read timeout"), _Response(200, {"result": {}}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"

    # The probe hits the MCP endpoint itself, authenticated with the paste.
    (_, (probe_url, probe_kwargs)) = fake.calls
    assert probe_url == URL
    assert probe_kwargs["headers"]["Authorization"] == "Bearer fmcp_pasted"


def test_lost_reply_with_dead_paste_returns_none():
    """The exchange consumed the paste and the replacement died with the lost
    reply: nothing usable remains, and persisting the paste would strand the
    gateway — the caller must abort."""
    fake = _FakeHttpx(OSError("read timeout"), _Response(401, {}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) is None


def test_lost_reply_with_failed_probe_returns_none():
    """When even the probe can't reach the server there is no trustworthy
    credential to save."""
    fake = _FakeHttpx(OSError("read timeout"), OSError("read timeout"))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) is None


def test_garbage_success_body_probes_the_paste():
    """A 200 consumed the subject, so an unusable replacement body is the
    same ambiguity as a lost reply: trust the probe, not the paste."""
    fake = _FakeHttpx(
        _Response(200, {"access_token": None}), _Response(200, {"result": {}})
    )
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"

    fake = _FakeHttpx(
        _Response(200, {"access_token": "not-an-fmcp-token"}), _Response(401, {})
    )
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) is None

    fake = _FakeHttpx(_Response(200, ValueError("not json")), _Response(401, {}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) is None
