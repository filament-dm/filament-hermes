"""Rewrite the server's "upload a local file" hint for this deployment.

The Filament server's tool descriptions (``post_message`` /
``reply_in_thread`` ``attachments``, ``set_profile`` ``image``) explain how
to attach a local file. A current server leads with its signed-URL tool —
call ``create_media_upload``, ``curl -T`` the file to the ``upload_url`` it
returns, no token needed — and then offers harnesses that hold the agent's
bearer token a ``curl`` against placeholders:

    curl -X POST '$MCP_BASE/mcp/agents/upload?filename=photo.jpg' \\
      -H 'Authorization: Bearer $TOKEN' ...

An older server has only that bearer snippet.

The token is the trouble. The plugin holds it, but the model's shell never
does: Hermes strips ``FILAMENT_MCP_TOKEN`` (``secret: true`` in plugin.yaml)
from the environment of terminal and execute_code subprocesses. So at
registration the plugin rewrites every description string it registers:

- With ``create_media_upload`` on the server (``signed_upload``), the bearer
  alternative is cut — the "if your harness holds the agent bearer token"
  snippet with the placeholder note explaining it, and the "harnesses that
  hold the agent bearer token can instead ..." sentences of the media tools —
  so the model is pointed at the signed URL only. Nothing in the text names
  ``$TOKEN`` any more, and nothing is substituted for it.
- Without it, the old rewrite: ``$MCP_BASE/mcp/agents/upload`` (and the older
  ``<mcp-base>/…`` spelling in the bundled manifest) becomes the concrete
  upload URL derived from the configured MCP URL (baked in, because
  ``FILAMENT_MCP_URL`` is usually unset — the production default lives in
  code); a bare ``$MCP_BASE`` (the server's prose defines it as the scheme
  and host of the MCP URL) becomes that origin; and ``$TOKEN`` becomes
  ``$FILAMENT_MCP_TOKEN``. That last only works where a shell inherits the
  variable, which current Hermes doesn't allow; on such a server the agent
  sends a local file natively instead (``MEDIA:/path`` in its reply, which
  this process uploads). The token value itself is never written into a
  description: descriptions are sent to the model provider.

Only ``description`` fields are rewritten, recursively, so a parameter's
enum values or defaults can never be touched. Stdlib-only and side-effect
free so ``tests/test_upload_hint.py`` loads it standalone.
"""

import re
from typing import Any
from urllib.parse import urlsplit

TOKEN_ENV_VAR = "FILAMENT_MCP_TOKEN"

# The server's placeholder spellings for the upload endpoint. Both forms end in
# the fixed ``/mcp/agents/upload`` path, so the replacement is the whole URL.
_UPLOAD_URL_PLACEHOLDER = re.compile(
    r"(?:\$\{?MCP_BASE\}?|<mcp-base>)/mcp/agents/upload"
)
# A bare ``$MCP_BASE`` outside the URL form (the server's explanatory prose).
_BASE_PLACEHOLDER = re.compile(r"\$\{?MCP_BASE\}?(?![A-Za-z0-9_])")
# ``$TOKEN`` / ``${TOKEN}`` only — never ``$TOKEN_X`` or ``$FILAMENT_MCP_TOKEN``.
_TOKEN_PLACEHOLDER = re.compile(r"\$\{?TOKEN\}?(?![A-Za-z0-9_])")

# The bearer alternative in the server's text (synapse tools_write /
# tools_config / tools_media), cut when the signed-URL tool is offered. The
# curl snippet runs from "If your harness holds the agent bearer token" to
# "which returns {mxc_url: ...} to pass here.", the placeholder note that
# follows it explains $MCP_BASE and $TOKEN, and the media tools end on one
# "Harnesses that hold the agent bearer token can instead ..." sentence.
_BEARER_SNIPPET = re.compile(
    r"If your harness holds the agent bearer token.*?"
    r"which returns \{mxc_url: '[^']*'\} to pass here\.[ \t]*",
    re.DOTALL,
)
_PLACEHOLDER_NOTE = re.compile(
    r"[ \t]*\(\$MCP_BASE is the scheme and host of the MCP URL.*?\)", re.DOTALL
)
_BEARER_SENTENCE = re.compile(
    r"[ \t]*Harnesses that hold the agent bearer token can instead [^.]*?"
    r" with it\."
)


def upload_url(mcp_url: str) -> str:
    """The upload side-channel next to *mcp_url*, e.g.
    ``https://api.filament.dm/mcp/agents`` → ``…/mcp/agents/upload``.

    Same derivation ``FilamentAPI.download_media`` uses for ``/media``.
    """
    return mcp_url.rstrip("/") + "/upload"


def mcp_origin(mcp_url: str) -> str:
    """The scheme and host of *mcp_url* — what the server's prose calls
    ``$MCP_BASE`` (``https://api.filament.dm/mcp/agents`` →
    ``https://api.filament.dm``). Falls back to the URL itself if it has no
    scheme/host to speak of, rather than emitting an empty string."""
    parts = urlsplit(mcp_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return mcp_url.rstrip("/")


def rewrite_text(text: str, mcp_url: str, signed_upload: bool = False) -> str:
    """Substitute this deployment's values into one description string,
    first cutting the bearer alternative when *signed_upload* (the server
    offers ``create_media_upload``)."""
    # Every bearer alternative names $MCP_BASE, so the fast path holds.
    if "MCP_BASE" not in text and "<mcp-base>" not in text and "$" not in text:
        return text
    if signed_upload:
        text = _BEARER_SNIPPET.sub("", text)
        text = _PLACEHOLDER_NOTE.sub("", text)
        text = _BEARER_SENTENCE.sub("", text)
    text = _UPLOAD_URL_PLACEHOLDER.sub(upload_url(mcp_url), text)
    text = _BASE_PLACEHOLDER.sub(mcp_origin(mcp_url), text)
    if signed_upload:
        return text
    return _TOKEN_PLACEHOLDER.sub("$" + TOKEN_ENV_VAR, text)


def rewrite_tool(
    tool: dict[str, Any], mcp_url: str, signed_upload: bool = False
) -> dict[str, Any]:
    """Return a copy of an MCP tool definition with every ``description``
    (the tool's own and each nested parameter's, ``$defs`` included)
    rewritten. The input is not mutated — the manifest fallback is a
    module-level constant shared across registrations."""
    return _rewrite_node(tool, mcp_url, signed_upload)


def _rewrite_node(node: Any, mcp_url: str, signed_upload: bool) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                out[key] = rewrite_text(value, mcp_url, signed_upload)
            else:
                out[key] = _rewrite_node(value, mcp_url, signed_upload)
        return out
    if isinstance(node, list):
        return [_rewrite_node(item, mcp_url, signed_upload) for item in node]
    return node
