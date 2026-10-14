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


# ── A server with create_media_upload: the bearer route is cut ───────

# Verbatim from filament-dm/synapse#1157 (tools_write._ATTACHMENTS_DESCRIPTION,
# tools_config's set_profile image, tools_media's two tool descriptions).
_NOTE = (
    "($MCP_BASE is the scheme and host of the MCP URL you connect to, for "
    "production https://api.filament.dm, and $TOKEN is the agent bearer token "
    "that authenticates your MCP requests — substitute your own values. "
    "Hermes agents: FILAMENT_MCP_URL is $MCP_BASE/mcp/agents and the token is "
    "FILAMENT_MCP_TOKEN.)"
)
SIGNED_ATTACHMENTS = (
    "Optional media attachments (images/files) to send with the message. "
    "Each references already-uploaded media by mxc:// URL. For a LOCAL FILE, "
    "call create_media_upload, then from your shell run "
    "`curl -fsS -T /path/to/photo.jpg '<upload_url>'` (no token needed); the "
    "response's mxc_url goes here. For a file at a public URL, "
    "upload_media_from_url returns the mxc_url directly. If your harness "
    "holds the agent bearer token you can instead upload with:\n"
    "  curl -X POST '$MCP_BASE/mcp/agents/upload?filename=photo.jpg' \\\n"
    "    -H 'Authorization: Bearer $TOKEN' \\\n"
    "    -H 'Content-Type: image/jpeg' \\\n"
    "    --data-binary @/path/to/photo.jpg\n"
    "which returns {mxc_url: 'mxc://...'} to pass here. "
    + _NOTE
    + " A single attachment "
    "is sent as an image/video/audio/file message with markdown_body as its "
    "caption; multiple attachments are bundled into one gallery message. "
    "markdown_body may be empty when attachments are provided."
)
SIGNED_PROFILE_IMAGE = (
    "Avatar image. Three accepted forms (preferred order):\n"
    "  1. `mxc://...` — an already-uploaded media URL. "
    "Cheapest. For LOCAL FILES, call create_media_upload and run "
    "`curl -fsS -T /path/to/file '<upload_url>'` from your shell (no "
    "token needed); the response's mxc_url goes here. If your "
    "harness holds the agent bearer token, you can instead upload "
    "with:\n"
    "    curl -X POST $MCP_BASE/mcp/agents/upload \\\n"
    "      -H 'Authorization: Bearer $TOKEN' \\\n"
    "      -H 'Content-Type: image/jpeg' \\\n"
    "      --data-binary @/path/to/file\n"
    "  which returns {mxc_url: 'mxc://...'} to pass here. " + _NOTE + "\n"
    "  2. `https://...` or `http://...` — server downloads and "
    "stores it for you.\n"
)
CREATE_MEDIA_UPLOAD = (
    "Get a short-lived URL to upload one local file from your shell, "
    "to attach to a message (or use as set_profile's image): "
    "curl -fsS -T /path/to/file '<upload_url>' (plus upload_headers) "
    "— no Authorization header needed. The response is "
    "{mxc_url: 'mxc://...'}; pass that mxc_url in `attachments`. "
    "Each URL accepts one upload and expires at expires_at. For a "
    "file at a public URL, use upload_media_from_url instead. "
    "Harnesses that hold the agent bearer token can instead POST to "
    "$MCP_BASE/mcp/agents/upload with it."
)
GET_MEDIA_DOWNLOAD_URL = (
    "Get a short-lived download URL for an attachment (image, file, "
    "video, audio) — pass its mxc_url from a message's `media` "
    "descriptors and that message's message_id. Download it to a local "
    "file from your shell: curl -fsSL -o <file> '<download_url>'. To look "
    "at an image or read a document, open that local file with your own "
    "tools. The URL needs no Authorization header and expires at "
    "expires_at; call get_media_download_url again for a fresh one. "
    "Harnesses that hold the agent bearer token can instead GET "
    "$MCP_BASE/mcp/agents/media?mxc_url=<mxc_url> with it."
)


def _signed(text):
    return upload_hint.rewrite_text(text, PROD, signed_upload=True)


def test_signed_server_attachments_point_at_create_media_upload_only():
    assert _signed(SIGNED_ATTACHMENTS) == (
        "Optional media attachments (images/files) to send with the message. "
        "Each references already-uploaded media by mxc:// URL. For a LOCAL "
        "FILE, call create_media_upload, then from your shell run "
        "`curl -fsS -T /path/to/photo.jpg '<upload_url>'` (no token needed); "
        "the response's mxc_url goes here. For a file at a public URL, "
        "upload_media_from_url returns the mxc_url directly. A single "
        "attachment is sent as an image/video/audio/file message with "
        "markdown_body as its caption; multiple attachments are bundled into "
        "one gallery message. markdown_body may be empty when attachments are "
        "provided."
    )


def test_signed_server_profile_image_keeps_its_numbered_forms():
    assert _signed(SIGNED_PROFILE_IMAGE) == (
        "Avatar image. Three accepted forms (preferred order):\n"
        "  1. `mxc://...` — an already-uploaded media URL. Cheapest. For LOCAL "
        "FILES, call create_media_upload and run "
        "`curl -fsS -T /path/to/file '<upload_url>'` from your shell (no token "
        "needed); the response's mxc_url goes here.\n"
        "  2. `https://...` or `http://...` — server downloads and stores it "
        "for you.\n"
    )


def test_signed_server_media_tools_lose_their_bearer_sentence():
    assert _signed(CREATE_MEDIA_UPLOAD).endswith(
        "For a file at a public URL, use upload_media_from_url instead."
    )
    assert _signed(GET_MEDIA_DOWNLOAD_URL).endswith(
        "call get_media_download_url again for a fresh one."
    )


def test_signed_rewrite_never_names_the_token():
    for text in (
        SIGNED_ATTACHMENTS,
        SIGNED_PROFILE_IMAGE,
        CREATE_MEDIA_UPLOAD,
        GET_MEDIA_DOWNLOAD_URL,
    ):
        out = _signed(text)
        assert "TOKEN" not in out and "Bearer" not in out and "MCP_BASE" not in out
        assert _signed(out) == out
    # A stray placeholder the cut didn't reach is left as the server wrote
    # it, never pointed at a variable the shell doesn't have.
    assert _signed("Bearer $TOKEN") == "Bearer $TOKEN"


def test_without_the_signed_tool_the_old_rewrite_stands():
    out = upload_hint.rewrite_text(SIGNED_ATTACHMENTS, PROD)
    assert "Bearer $FILAMENT_MCP_TOKEN" in out
    assert "https://api.filament.dm/mcp/agents/upload?filename=photo.jpg" in out


def test_rewrite_tool_passes_the_signed_flag_down():
    tool = {
        "name": "post_message",
        "inputSchema": {
            "properties": {"attachments": {"description": SIGNED_ATTACHMENTS}}
        },
    }
    out = upload_hint.rewrite_tool(tool, PROD, signed_upload=True)
    desc = out["inputSchema"]["properties"]["attachments"]["description"]
    assert desc == _signed(SIGNED_ATTACHMENTS)
