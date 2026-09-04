"""Tests for the connect-token exchange at setup time (ENG-893).

The pasted connect token has been through shell history and the process
environment, so right after validation setup swaps it for a fresh bearer via
the server's token-exchange grant and persists the replacement. The swap is
best-effort: any failure keeps the pasted token, so setup still completes
against a server that predates the exchange.

Loaded standalone, AST-style like test_token_source: setup_cli's module-level
imports need Hermes, but the function under test only touches httpx and the
print helpers, which we stub.
"""

import ast
import types
from pathlib import Path

_SETUP_CLI = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "setup_cli.py"
)

_WANTED_ASSIGNS = {"_TOKEN_EXCHANGE_GRANT", "_ACCESS_TOKEN_TYPE"}


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeHttpx(types.SimpleNamespace):
    """Records the one POST the exchange makes and plays back a response."""

    def __init__(self, response=None, error=None):
        super().__init__()
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _load_exchange(fake_httpx):
    tree = ast.parse(_SETUP_CLI.read_text())
    nodes = []
    for node in tree.body:
        is_exchange_def = (
            isinstance(node, ast.FunctionDef) and node.name == "_exchange_connect_token"
        )
        is_wanted_assign = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _WANTED_ASSIGNS for t in node.targets
        )
        if is_exchange_def or is_wanted_assign:
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
    fake = _FakeHttpx(response=_Response(200, {"access_token": "fmcp_fresh"}))
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
    nothing — setup must proceed with the token it validated."""
    fake = _FakeHttpx(response=_Response(401, {"error": "invalid_grant"}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"


def test_network_failure_keeps_the_pasted_token():
    fake = _FakeHttpx(error=OSError("connection refused"))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"


def test_garbage_success_body_keeps_the_pasted_token():
    """A 200 whose body lacks a plausible token must not blank the credential."""
    fake = _FakeHttpx(response=_Response(200, {"access_token": None}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"

    fake = _FakeHttpx(response=_Response(200, {"access_token": "not-an-fmcp-token"}))
    exchange = _load_exchange(fake)
    assert exchange("fmcp_pasted", URL) == "fmcp_pasted"
