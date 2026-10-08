"""Rewrite the server's "upload a local file" hint for this deployment.

The Filament server's tool descriptions (``post_message`` /
``reply_in_thread`` ``attachments``, ``set_profile`` ``image``) explain the
two-step attachment flow with a ``curl`` snippet against placeholders:

    curl -X POST '$MCP_BASE/mcp/agents/upload?filename=photo.jpg' \\
      -H 'Authorization: Bearer $TOKEN' ...

Those placeholders are the server's, not ours. Nothing in a Hermes install
defines ``MCP_BASE`` or ``TOKEN``: the plugin is configured by
``FILAMENT_MCP_URL`` (which already ends in ``/mcp/agents``) and
``FILAMENT_MCP_TOKEN``. Left alone, the agent reads the hint, tries the
snippet, finds both variables unset and gives up ("we don't provide
MCP_BASE and TOKEN"). So at registration the plugin rewrites every
description string it registers:

- ``$MCP_BASE/mcp/agents/upload`` (and the older ``<mcp-base>/…`` spelling
  in the bundled manifest) becomes the concrete upload URL derived from the
  configured MCP URL. It is baked in rather than referenced as
  ``$FILAMENT_MCP_URL`` because that variable is usually unset — the
  production default lives in code.
- A bare ``$MCP_BASE`` (the server's prose defines the placeholder as the
  scheme and host of the MCP URL) becomes that origin, so the sentence still
  reads true after substitution.
- ``$TOKEN`` becomes ``$FILAMENT_MCP_TOKEN``, the variable that IS in the
  gateway process's environment (Hermes loads its configured settings into
  it at startup) and is inherited by the terminal tool's subprocesses. The
  token value itself is never written into a description: descriptions are
  sent to the model provider.

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


def rewrite_text(text: str, mcp_url: str) -> str:
    """Substitute this deployment's values into one description string."""
    if "MCP_BASE" not in text and "<mcp-base>" not in text and "$" not in text:
        return text
    text = _UPLOAD_URL_PLACEHOLDER.sub(upload_url(mcp_url), text)
    text = _BASE_PLACEHOLDER.sub(mcp_origin(mcp_url), text)
    return _TOKEN_PLACEHOLDER.sub("$" + TOKEN_ENV_VAR, text)


def rewrite_tool(tool: dict[str, Any], mcp_url: str) -> dict[str, Any]:
    """Return a copy of an MCP tool definition with every ``description``
    (the tool's own and each nested parameter's, ``$defs`` included)
    rewritten. The input is not mutated — the manifest fallback is a
    module-level constant shared across registrations."""
    return _rewrite_node(tool, mcp_url)


def _rewrite_node(node: Any, mcp_url: str) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                out[key] = rewrite_text(value, mcp_url)
            else:
                out[key] = _rewrite_node(value, mcp_url)
        return out
    if isinstance(node, list):
        return [_rewrite_node(item, mcp_url) for item in node]
    return node
