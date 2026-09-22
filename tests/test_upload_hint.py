"""The server's upload hint names ``$MCP_BASE`` and ``$TOKEN``; a Hermes agent
has neither. ``upload_hint`` rewrites registered descriptions so the snippet
the model reads actually runs on its host (ENG: "needed MCP_BASE and TOKEN
but we don't provide those")."""

import importlib.util
import json
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"

_spec = importlib.util.spec_from_file_location(
    "upload_hint", _PKG_DIR / "upload_hint.py"
)
upload_hint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upload_hint)

PROD = "https://api.filament.dm/mcp/agents"

# Verbatim from the live server (tools_write._ATTACHMENTS_DESCRIPTION).
LIVE_ATTACHMENTS = (
    "For LOCAL FILES, upload first with:\n"
    "  curl -X POST '$MCP_BASE/mcp/agents/upload?filename=photo.jpg' \\\n"
    "    -H 'Authorization: Bearer $TOKEN' \\\n"
    "    -H 'Content-Type: image/jpeg' \\\n"
    "    --data-binary @/path/to/photo.jpg\n"
    "which returns {mxc_url: 'mxc://...'} to pass here."
)


def test_upload_url_sits_next_to_the_mcp_url():
    assert upload_hint.upload_url(PROD) == "https://api.filament.dm/mcp/agents/upload"
    assert (
        upload_hint.upload_url(PROD + "/")
        == "https://api.filament.dm/mcp/agents/upload"
    )
    assert (
        upload_hint.upload_url("http://localhost:8008/mcp/agents")
        == "http://localhost:8008/mcp/agents/upload"
    )


def test_rewrites_live_curl_snippet_to_deployment_values():
    out = upload_hint.rewrite_text(LIVE_ATTACHMENTS, PROD)
    assert (
        "curl -X POST 'https://api.filament.dm/mcp/agents/upload?filename=photo.jpg'"
        in out
    )
    assert "-H 'Authorization: Bearer $FILAMENT_MCP_TOKEN'" in out
    assert "MCP_BASE" not in out
    assert "$TOKEN" not in out
    # Everything else is untouched.
    assert "--data-binary @/path/to/photo.jpg" in out
    assert "{mxc_url: 'mxc://...'}" in out


def test_rewrites_dev_cluster_url():
    out = upload_hint.rewrite_text(
        LIVE_ATTACHMENTS, "http://localhost:8008/mcp/agents/"
    )
    assert "POST 'http://localhost:8008/mcp/agents/upload?filename=photo.jpg'" in out


def test_rewrites_braced_and_manifest_spellings():
    assert (
        upload_hint.rewrite_text(
            "POST ${MCP_BASE}/mcp/agents/upload with ${TOKEN}", PROD
        )
        == "POST https://api.filament.dm/mcp/agents/upload with $FILAMENT_MCP_TOKEN"
    )
    # The bundled manifest's older prose form.
    assert (
        upload_hint.rewrite_text(
            "POST the raw file bytes to `<mcp-base>/mcp/agents/upload`", PROD
        )
        == "POST the raw file bytes to `https://api.filament.dm/mcp/agents/upload`"
    )


def test_bare_base_placeholder_becomes_the_origin():
    """The server's prose defines ``$MCP_BASE`` as the scheme and host of the
    MCP URL; after substitution that sentence has to still be true."""
    prose = (
        "($MCP_BASE is the scheme and host of the MCP URL you connect to; "
        "$TOKEN is the bearer token that authenticates your MCP requests.)"
    )
    assert upload_hint.rewrite_text(prose, PROD) == (
        "(https://api.filament.dm is the scheme and host of the MCP URL you "
        "connect to; $FILAMENT_MCP_TOKEN is the bearer token that "
        "authenticates your MCP requests.)"
    )
    assert upload_hint.mcp_origin("http://localhost:8008/mcp/agents/") == (
        "http://localhost:8008"
    )
    # The URL form is still derived from the full MCP URL, not the origin, so
    # a deployment behind a path prefix keeps its prefix.
    prefixed = "https://proxy.example/filament/mcp/agents"
    out = upload_hint.rewrite_text(
        "$MCP_BASE/mcp/agents/upload and $MCP_BASE", prefixed
    )
    assert out == (
        "https://proxy.example/filament/mcp/agents/upload and https://proxy.example"
    )
    # Word-bounded: ``$MCP_BASE_URL`` is not our placeholder.
    assert upload_hint.rewrite_text("$MCP_BASE_URL", PROD) == "$MCP_BASE_URL"


def test_token_rewrite_is_word_bounded():
    """``$TOKEN`` only — a description that already says ``$FILAMENT_MCP_TOKEN``
    or names some other ``$TOKEN_ID`` must survive a second pass unchanged."""
    already = "Bearer $FILAMENT_MCP_TOKEN and $TOKEN_ID"
    assert upload_hint.rewrite_text(already, PROD) == already


def test_rewrite_is_idempotent():
    once = upload_hint.rewrite_text(LIVE_ATTACHMENTS, PROD)
    assert upload_hint.rewrite_text(once, PROD) == once


def test_unrelated_text_is_returned_unchanged():
    for text in ("", "Post a message in a channel.", "costs $5", "TOKEN budget"):
        assert upload_hint.rewrite_text(text, PROD) == text


def test_rewrite_tool_reaches_nested_descriptions_and_defs_without_mutating():
    tool = {
        "name": "post_message",
        "description": "Upload bytes first with the /mcp/agents/upload side-channel.",
        "inputSchema": {
            "$defs": {
                "AttachmentArg": {
                    "properties": {
                        "mxc_url": {"description": "from $MCP_BASE/mcp/agents/upload"},
                    }
                }
            },
            "properties": {
                "attachments": {
                    "anyOf": [
                        {"items": {"$ref": "#/$defs/AttachmentArg"}},
                        {"type": "null"},
                    ],
                    "description": LIVE_ATTACHMENTS,
                },
                "final": {"default": False, "description": "no placeholders here"},
                "channel": {"enum": ["$TOKEN"], "description": "Channel id"},
            },
        },
    }
    before = json.dumps(tool, sort_keys=True)
    out = upload_hint.rewrite_tool(tool, PROD)
    assert json.dumps(tool, sort_keys=True) == before, "input must not be mutated"

    props = out["inputSchema"]["properties"]
    assert "$FILAMENT_MCP_TOKEN" in props["attachments"]["description"]
    assert (
        "https://api.filament.dm/mcp/agents/upload"
        in props["attachments"]["description"]
    )
    assert (
        out["inputSchema"]["$defs"]["AttachmentArg"]["properties"]["mxc_url"][
            "description"
        ]
        == "from https://api.filament.dm/mcp/agents/upload"
    )
    # Only description fields are rewritten — a value that happens to look
    # like the placeholder is data, not prose.
    assert props["channel"]["enum"] == ["$TOKEN"]
    assert props["final"]["default"] is False
    # A bare path mention with no placeholder is left as-is.
    assert out["description"] == tool["description"]


def test_bundled_manifest_has_no_placeholder_left_after_rewrite():
    manifest = json.loads((_PKG_DIR / "tool_manifest.json").read_text())
    tools = manifest["tools"] if isinstance(manifest, dict) else manifest
    rewritten = json.dumps([upload_hint.rewrite_tool(t, PROD) for t in tools])
    assert "MCP_BASE" not in rewritten
    assert "<mcp-base>" not in rewritten
    assert "$TOKEN" not in rewritten
    # And the manifest really did carry the hint, so this test is not vacuous.
    assert "<mcp-base>" in json.dumps(tools) or "MCP_BASE" in json.dumps(tools)
