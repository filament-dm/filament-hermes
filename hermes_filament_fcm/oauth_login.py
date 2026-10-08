"""Sign this agent in to Filament with OAuth instead of a pasted token.

Filament's agents MCP is a standard MCP OAuth server: protected-resource
metadata, authorization-server metadata, dynamic client registration, and
authorization code + PKCE. The owner signs in to Filament in a browser and the
grant goes to the agent they just pressed Connect on in the app, so there is
no agent to pick and no token to copy. The access token it returns is the same
``fmcp_`` bearer a pasted connect token is, so everything after login is
unchanged.

When the server offers the device grant (RFC 8628), that is used instead: the
owner opens a short link on any device, which suits a headless host.

Stdlib-only so it is unit-testable without Hermes.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import select
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

SCOPE = "filament:agent:control"
CLIENT_NAME = "Hermes (Filament plugin)"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_HTTP_TIMEOUT_S = 20


class LoginError(Exception):
    """A login step failed; the message is fit to show the owner."""


@dataclass(frozen=True)
class ServerMetadata:
    resource: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    device_authorization_endpoint: str | None


def _request(
    url: str, *, form: dict[str, str] | None = None, body: Any = None
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw.decode(errors="replace")[:200]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LoginError(f"Could not reach {url}: {exc}") from exc


def _well_known(base: str, name: str) -> list[str]:
    """Where RFC 8414 / 9728 metadata for *base* may live, most specific first."""
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    return [f"{origin}/.well-known/{name}{path}", f"{base.rstrip('/')}/.well-known/{name}"]


def discover(mcp_url: str) -> ServerMetadata:
    prm = None
    for url in _well_known(mcp_url, "oauth-protected-resource"):
        status, doc = _request(url)
        if status == 200 and isinstance(doc, dict):
            prm = doc
            break
    if not prm or not prm.get("authorization_servers"):
        raise LoginError(f"{mcp_url} does not offer OAuth sign-in.")
    issuer = prm["authorization_servers"][0]
    meta = None
    for url in _well_known(issuer, "oauth-authorization-server"):
        status, doc = _request(url)
        if status == 200 and isinstance(doc, dict):
            meta = doc
            break
    if not meta:
        raise LoginError(f"Could not read the sign-in settings of {issuer}.")
    try:
        return ServerMetadata(
            resource=prm.get("resource") or mcp_url,
            authorization_endpoint=meta["authorization_endpoint"],
            token_endpoint=meta["token_endpoint"],
            registration_endpoint=meta["registration_endpoint"],
            device_authorization_endpoint=meta.get("device_authorization_endpoint"),
        )
    except KeyError as exc:
        raise LoginError(f"Filament's sign-in settings lack {exc}.") from exc


def register_client(meta: ServerMetadata, redirect_uri: str | None) -> str:
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "token_endpoint_auth_method": "none",
        "redirect_uris": [redirect_uri] if redirect_uri else [],
        "grant_types": [DEVICE_GRANT] if redirect_uri is None else ["authorization_code"],
        "response_types": [] if redirect_uri is None else ["code"],
    }
    status, doc = _request(meta.registration_endpoint, body=body)
    if status not in (200, 201) or "client_id" not in doc:
        raise LoginError(f"Filament refused to register this agent ({status}).")
    return str(doc["client_id"])


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def parse_redirect(text: str) -> dict[str, str]:
    """The query of a pasted redirect address (or a bare ``code=...`` query)."""
    text = text.strip()
    query = urllib.parse.urlsplit(text).query if "?" in text else text
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def _exchange(meta: ServerMetadata, form: dict[str, str]) -> tuple[int, Any]:
    return _request(meta.token_endpoint, form={**form, "resource": meta.resource})


def _token_from(status: int, doc: Any) -> str:
    if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
        return str(doc["access_token"])
    detail = doc.get("error_description") or doc.get("error") if isinstance(doc, dict) else doc
    raise LoginError(f"Filament did not issue a token ({status}): {detail}")


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        type(self).result = parse_redirect(self.path)
        ok = "code" in type(self).result
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Signed in to Filament. You can close this tab."
            if ok
            else "Filament sign-in did not finish. Go back to the terminal."
        )
        self.wfile.write(f"<p>{message}</p>".encode())
        type(self).done.set()

    def log_message(self, *_args: Any) -> None:
        pass


def _wait_for_redirect(
    done: threading.Event, get_result: Callable[[], dict[str, str]], timeout_s: float
) -> dict[str, str]:
    """The browser's redirect, or one pasted at the terminal, whichever comes."""
    deadline = time.monotonic() + timeout_s
    interactive = sys.stdin.isatty()
    while time.monotonic() < deadline:
        if done.wait(0.2):
            return get_result()
        if interactive and select.select([sys.stdin], [], [], 0)[0]:
            pasted = parse_redirect(sys.stdin.readline())
            if "code" in pasted or "error" in pasted:
                return pasted
    raise LoginError("Sign-in timed out. Run the login again.")


def authorization_code_login(
    meta: ServerMetadata,
    *,
    out: Callable[[str], None] = print,
    open_browser: bool = True,
    timeout_s: float = 600,
) -> str:
    handler = type("Callback", (_Callback,), {"result": {}, "done": threading.Event()})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client_id = register_client(meta, redirect_uri)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        url = meta.authorization_endpoint + "?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": meta.resource,
            }
        )
        out("Sign in to Filament to connect this agent:")
        out(f"  {url}")
        out(
            "If the browser is on another machine, sign in there, then paste "
            "the address it ends up on (it starts with http://127.0.0.1) here."
        )
        if open_browser:
            try:
                import webbrowser  # noqa: PLC0415 — only when a browser may exist

                webbrowser.open(url)
            except Exception:  # noqa: BLE001 — opening a browser is best-effort
                pass
        got = _wait_for_redirect(handler.done, lambda: handler.result, timeout_s)
    finally:
        server.shutdown()
    if got.get("error"):
        raise LoginError(f"Sign-in was refused: {got.get('error_description') or got['error']}")
    if got.get("state") != state:
        raise LoginError("Sign-in answered a different request. Run the login again.")
    return _token_from(
        *_exchange(
            meta,
            {
                "grant_type": "authorization_code",
                "code": got["code"],
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
    )


def device_login(
    meta: ServerMetadata,
    *,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    if not meta.device_authorization_endpoint:
        raise LoginError("Filament does not offer device sign-in.")
    client_id = register_client(meta, None)
    status, doc = _request(
        meta.device_authorization_endpoint,
        form={"client_id": client_id, "scope": SCOPE, "resource": meta.resource},
    )
    if status != 200 or "device_code" not in doc:
        raise LoginError(f"Filament refused device sign-in ({status}).")
    link = doc.get("verification_uri_complete") or doc["verification_uri"]
    out("To connect this agent, open this on any device signed in to Filament:")
    out(f"  {link}")
    out(f"If it asks for a code, enter {doc['user_code']}.")
    interval = float(doc.get("interval", 5))
    deadline = time.monotonic() + float(doc.get("expires_in", 600))
    while time.monotonic() < deadline:
        sleep(interval)
        status, token_doc = _exchange(
            meta,
            {"grant_type": DEVICE_GRANT, "device_code": doc["device_code"], "client_id": client_id},
        )
        error = token_doc.get("error") if isinstance(token_doc, dict) else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        return _token_from(status, token_doc)
    raise LoginError("The sign-in link expired. Run the login again.")


def login(
    mcp_url: str,
    *,
    flow: str = "auto",
    out: Callable[[str], None] = print,
    open_browser: bool = True,
) -> str:
    """Run the sign-in and return the agent's ``fmcp_`` token.

    *flow* is ``browser``, ``device``, or ``auto``: the device grant when the
    server offers it, otherwise the browser.
    """
    meta = discover(mcp_url)
    if flow == "device" or (flow == "auto" and meta.device_authorization_endpoint):
        return device_login(meta, out=out)
    return authorization_code_login(meta, out=out, open_browser=open_browser)
