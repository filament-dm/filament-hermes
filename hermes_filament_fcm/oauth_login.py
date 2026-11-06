"""Sign this agent in to Filament with MCP OAuth: the device grant (RFC 8628)
when the server offers it, else authorization code + PKCE with a loopback
callback. Stdlib-only, so it is unit-testable without Hermes.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import select
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar

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
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"null")
            except ValueError as exc:
                raise LoginError(f"{url} did not answer with JSON.") from exc
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
    return [
        f"{origin}/.well-known/{name}{path}",
        f"{base.rstrip('/')}/.well-known/{name}",
    ]


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


class ClientStore:
    """The OAuth clients this agent registered, kept across logins.

    Filament gives a client that signs in again the agent it signed in before,
    so a reconnect lands on this agent rather than a new one. Kept per
    registration endpoint and flow; a missing *path* keeps nothing.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path

    def _load(self) -> dict[str, Any]:
        if self._path is None:
            return {}
        try:
            doc = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def get(self, meta: ServerMetadata, flow: str) -> dict[str, str] | None:
        entry = self._load().get(meta.registration_endpoint)
        record = entry.get(flow) if isinstance(entry, dict) else None
        if isinstance(record, dict) and isinstance(record.get("client_id"), str):
            return record
        return None

    def put(self, meta: ServerMetadata, flow: str, record: dict[str, str]) -> None:
        if self._path is None:
            return
        doc = self._load()
        entry = doc.get(meta.registration_endpoint)
        entry = entry if isinstance(entry, dict) else {}
        entry[flow] = record
        doc[meta.registration_endpoint] = entry
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2))
            os.replace(tmp, self._path)
        except OSError:
            # Only costs the next login its agent: it falls back to the app's
            # reconnect, as before clients were kept.
            pass


def register_client(meta: ServerMetadata, redirect_uri: str | None) -> str:
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "token_endpoint_auth_method": "none",
    }
    if redirect_uri is None:
        body.update(grant_types=[DEVICE_GRANT], redirect_uris=[], response_types=[])
    else:
        body.update(
            grant_types=["authorization_code"],
            redirect_uris=[redirect_uri],
            response_types=["code"],
        )
    status, doc = _request(meta.registration_endpoint, body=body)
    if status not in (200, 201) or not isinstance(doc, dict) or "client_id" not in doc:
        raise LoginError(f"Filament refused to register this agent ({status}).")
    return str(doc["client_id"])


def _with_query(url: str, params: dict[str, str]) -> str:
    """*url* with *params* added to whatever query it already has."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query) + list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


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
    detail = (
        doc.get("error_description") or doc.get("error")
        if isinstance(doc, dict)
        else doc
    )
    raise LoginError(f"Filament did not issue a token ({status}): {detail}")


class _Callback(http.server.BaseHTTPRequestHandler):
    result: ClassVar[dict[str, str]] = {}
    done: ClassVar[threading.Event] = threading.Event()
    state: ClassVar[str] = ""

    def do_GET(self) -> None:
        # Only the redirect for this login counts, once: a browser's stray
        # request for / or /favicon.ico must not end or overwrite it.
        got = parse_redirect(self.path)
        cls = type(self)
        if (
            urllib.parse.urlsplit(self.path).path != "/callback"
            or got.get("state") != cls.state
            or cls.done.is_set()
        ):
            self.send_response(404)
            self.end_headers()
            return
        cls.result = got
        ok = "code" in got
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Signed in to Filament. You can close this tab."
            if ok
            else "Filament sign-in did not finish. Go back to the terminal."
        )
        self.wfile.write(f"<p>{message}</p>".encode())
        cls.done.set()

    def log_message(self, *_args: Any) -> None:
        pass


def _wait_for_redirect(
    done: threading.Event, get_result: Callable[[], dict[str, str]], timeout_s: float
) -> dict[str, str]:
    """The browser's redirect, or one pasted at the terminal, whichever comes."""
    deadline = time.monotonic() + timeout_s
    # Paste-back reads stdin through select(), which Windows lacks for files.
    interactive = sys.stdin.isatty() and sys.platform != "win32"
    while time.monotonic() < deadline:
        if done.wait(0.2):
            return get_result()
        if interactive and select.select([sys.stdin], [], [], 0)[0]:
            pasted = parse_redirect(sys.stdin.readline())
            if "code" in pasted or "error" in pasted:
                return pasted
    raise LoginError("Sign-in timed out. Run the login again.")


def _saved_port(record: dict[str, str] | None) -> int:
    """The callback port a saved client registered, else 0 (any free one)."""
    if record is None:
        return 0
    try:
        return urllib.parse.urlsplit(record.get("redirect_uri", "")).port or 0
    except ValueError:
        return 0


def _listen(handler: type[_Callback], port: int) -> http.server.ThreadingHTTPServer:
    # Threading, so a browser's idle pre-opened connection can't block shutdown.
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        if port == 0:
            raise
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)


def authorization_code_login(
    meta: ServerMetadata,
    *,
    out: Callable[[str], None] = print,
    open_browser: bool = True,
    timeout_s: float = 600,
    clients: ClientStore | None = None,
) -> str:
    handler = type("Callback", (_Callback,), {"result": {}, "done": threading.Event()})
    saved = clients.get(meta, "browser") if clients else None
    # The saved client's own port, so its registered redirect still matches.
    server = _listen(handler, _saved_port(saved))
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if saved is not None and saved.get("redirect_uri") == redirect_uri:
            client_id = saved["client_id"]
        else:
            client_id = register_client(meta, redirect_uri)
            if clients is not None:
                clients.put(
                    meta,
                    "browser",
                    {"client_id": client_id, "redirect_uri": redirect_uri},
                )
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        handler.state = state
        url = _with_query(
            meta.authorization_endpoint,
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": meta.resource,
            },
        )
        out("Sign in to Filament to connect this agent:")
        out(f"  {url}")
        out(
            "If the browser is on another machine, sign in there, then paste "
            "the address it ends up on (it starts with http://127.0.0.1) here."
        )
        if open_browser:
            import webbrowser  # noqa: PLC0415 — only when a browser may exist

            with contextlib.suppress(Exception):
                webbrowser.open(url)
        got = _wait_for_redirect(handler.done, lambda: handler.result, timeout_s)
    finally:
        server.shutdown()
        server.server_close()
    if got.get("error"):
        raise LoginError(
            f"Sign-in was refused: {got.get('error_description') or got['error']}"
        )
    if got.get("state") != state:
        raise LoginError("Sign-in answered a different request. Run the login again.")
    if not got.get("code"):
        raise LoginError("Sign-in returned no authorization code. Run the login again.")
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


def _device_client(meta: ServerMetadata, clients: ClientStore | None) -> str:
    client_id = register_client(meta, None)
    if clients is not None:
        clients.put(meta, "device", {"client_id": client_id})
    return client_id


def device_login(
    meta: ServerMetadata,
    *,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clients: ClientStore | None = None,
) -> str:
    endpoint = meta.device_authorization_endpoint
    if not endpoint:
        raise LoginError("Filament does not offer device sign-in.")
    saved = clients.get(meta, "device") if clients else None
    client_id = saved["client_id"] if saved else _device_client(meta, clients)

    def start(client_id: str) -> tuple[int, Any]:
        form = {"client_id": client_id, "scope": SCOPE, "resource": meta.resource}
        return _request(endpoint, form=form)

    status, doc = start(client_id)
    # A saved client the server no longer knows: register afresh, once.
    if saved and isinstance(doc, dict) and doc.get("error") == "invalid_client":
        client_id = _device_client(meta, clients)
        status, doc = start(client_id)
    if (
        status != 200
        or not isinstance(doc, dict)
        or not {"device_code", "user_code", "verification_uri"} <= doc.keys()
    ):
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
            {
                "grant_type": DEVICE_GRANT,
                "device_code": doc["device_code"],
                "client_id": client_id,
            },
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
    client_file: Path | None = None,
) -> str:
    """Run the sign-in and return the agent's ``fmcp_`` token.

    *flow* is ``browser``, ``device``, or ``auto``: the device grant when the
    server offers it, otherwise the browser. *client_file* keeps the
    registered client, so signing in again reconnects the same agent.
    """
    meta = discover(mcp_url)
    clients = ClientStore(client_file)
    if flow == "device" or (flow == "auto" and meta.device_authorization_endpoint):
        return device_login(meta, out=out, clients=clients)
    return authorization_code_login(
        meta, out=out, open_browser=open_browser, clients=clients
    )
