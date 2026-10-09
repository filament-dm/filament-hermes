"""Tests for `hermes filament login`'s OAuth client (oauth_login.py).

Loaded standalone: the module is stdlib-only. HTTP is stubbed at `_request`.
"""

import base64
import hashlib
import importlib.util
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "oauth_login.py"
)
_spec = importlib.util.spec_from_file_location("filament_oauth_login", _PATH)
ol = importlib.util.module_from_spec(_spec)
sys.modules["filament_oauth_login"] = ol
_spec.loader.exec_module(ol)

MCP = "https://api.example/mcp/agents"
ISSUER = "https://api.example/mcp/agents/oauth"


def _server(device: bool = True) -> dict:
    meta = {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
    }
    if device:
        meta["device_authorization_endpoint"] = f"{ISSUER}/device_authorization"
    return {
        "https://api.example/.well-known/oauth-protected-resource/mcp/agents": (
            200,
            {"resource": MCP, "authorization_servers": [ISSUER]},
        ),
        f"{ISSUER}/.well-known/oauth-authorization-server": (200, meta),
    }


class FakeHttp:
    """Answers `_request` from a table; /token answers come from a queue."""

    def __init__(self, routes: dict, token_answers: list | None = None):
        self.routes = routes
        self.token_answers = list(token_answers or [])
        self.calls: list[tuple[str, dict | None, object]] = []

    def __call__(self, url, *, form=None, body=None):
        self.calls.append((url, form, body))
        if url.endswith("/register"):
            return 201, {"client_id": "client-1"}
        if url.endswith("/device_authorization"):
            return 200, {
                "device_code": "dev_1",
                "user_code": "BCDF-GHJK",
                "verification_uri": f"{ISSUER}/device",
                "verification_uri_complete": f"{ISSUER}/device?user_code=BCDF-GHJK",
                "expires_in": 600,
                "interval": 5,
            }
        if url.endswith("/token"):
            return self.token_answers.pop(0)
        return self.routes.get(url, (404, {}))


@pytest.fixture
def http(monkeypatch):
    def install(routes, token_answers=None):
        fake = FakeHttp(routes, token_answers)
        monkeypatch.setattr(ol, "_request", fake)
        return fake

    return install


def test_discovery_reads_the_resource_and_both_grants(http):
    http(_server())
    meta = ol.discover(MCP)
    assert meta.resource == MCP
    assert meta.token_endpoint == f"{ISSUER}/token"
    assert meta.device_authorization_endpoint == f"{ISSUER}/device_authorization"


def test_a_server_without_oauth_is_refused(http):
    http({})
    with pytest.raises(ol.LoginError, match="does not offer OAuth"):
        ol.discover(MCP)


def test_device_login_waits_out_pending_and_slow_down(http):
    fake = http(
        _server(),
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": "fmcp_ok"}),
        ],
    )
    slept: list[float] = []
    lines: list[str] = []
    token = ol.device_login(ol.discover(MCP), out=lines.append, sleep=slept.append)
    assert token == "fmcp_ok"
    assert slept == [5, 5, 10]
    assert any("user_code=BCDF-GHJK" in line for line in lines)
    token_form = [form for url, form, _ in fake.calls if url.endswith("/token")][-1]
    assert token_form["grant_type"] == ol.DEVICE_GRANT
    assert token_form["resource"] == MCP


def test_device_registration_asks_for_no_redirect(http):
    fake = http(_server(), [(200, {"access_token": "fmcp_ok"})])
    ol.device_login(ol.discover(MCP), out=lambda _: None, sleep=lambda _: None)
    register = next(body for url, _, body in fake.calls if url.endswith("/register"))
    assert register["redirect_uris"] == []
    assert register["grant_types"] == [ol.DEVICE_GRANT]


def test_a_refused_device_grant_is_reported(http):
    http(_server(), [(400, {"error": "access_denied"})])
    with pytest.raises(ol.LoginError, match="access_denied"):
        ol.device_login(ol.discover(MCP), out=lambda _: None, sleep=lambda _: None)


def test_auto_prefers_the_device_grant_when_offered(http, monkeypatch):
    http(_server(device=True))
    monkeypatch.setattr(ol, "device_login", lambda meta, out: "fmcp_device")
    monkeypatch.setattr(
        ol, "authorization_code_login", lambda meta, out, open_browser: "fmcp_browser"
    )
    assert ol.login(MCP) == "fmcp_device"
    http(_server(device=False))
    assert ol.login(MCP) == "fmcp_browser"
    assert ol.login(MCP, flow="browser") == "fmcp_browser"


def test_browser_login_takes_the_loopback_redirect(http, monkeypatch):
    fake = http(_server(), [(200, {"access_token": "fmcp_browser"})])
    monkeypatch.setattr(ol.sys.stdin, "isatty", lambda: False)
    lines: list[str] = []
    result: dict = {}

    def run():
        result["token"] = ol.authorization_code_login(
            ol.discover(MCP), out=lines.append, open_browser=False, timeout_s=10
        )

    thread = threading.Thread(target=run)
    thread.start()
    while not any(line.strip().startswith("https://") for line in lines):
        pass
    url = next(line.strip() for line in lines if line.strip().startswith("https://"))
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    # A browser's stray request first: it must neither end nor spoil the login.
    origin = query["redirect_uri"].rsplit("/", 1)[0]
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{origin}/favicon.ico").read()
    urllib.request.urlopen(
        f"{query['redirect_uri']}?code=c1&state={query['state']}"
    ).read()
    thread.join(10)

    assert result["token"] == "fmcp_browser"
    assert query["resource"] == MCP
    assert query["code_challenge_method"] == "S256"
    token_form = [form for u, form, _ in fake.calls if u.endswith("/token")][-1]
    digest = hashlib.sha256(token_form["code_verifier"].encode()).digest()
    assert (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        == query["code_challenge"]
    )
    assert token_form["code"] == "c1"


def test_a_pasted_redirect_is_read_like_the_callback():
    assert ol.parse_redirect("http://127.0.0.1:5/callback?code=a&state=b\n") == {
        "code": "a",
        "state": "b",
    }
    assert ol.parse_redirect("code=a&state=b") == {"code": "a", "state": "b"}


def test_a_registration_that_is_not_an_object_is_refused(http, monkeypatch):
    fake = http(_server())

    def null_register(url, *, form=None, body=None):
        if url.endswith("/register"):
            return 201, None
        return fake(url, form=form, body=body)

    monkeypatch.setattr(ol, "_request", null_register)
    with pytest.raises(ol.LoginError, match="refused to register"):
        ol.device_login(ol.discover(MCP), out=lambda _: None, sleep=lambda _: None)


def test_a_reply_that_is_not_json_is_a_login_error(monkeypatch):
    class Resp:
        status = 200

        def read(self):
            return b"<html>proxy</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ol.urllib.request, "urlopen", lambda *a, **k: Resp())
    with pytest.raises(ol.LoginError, match="did not answer with JSON"):
        ol._request("https://api.example/x")
