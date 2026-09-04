"""The bundled MCP fallback teaches agents the Block Kit transport syntax."""

import json
from pathlib import Path


def test_message_tools_advertise_block_kit_without_html() -> None:
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "hermes_filament_fcm"
        / "tool_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())

    by_name = {tool["name"]: tool for tool in manifest}
    for name in ("post_message", "reply_in_thread", "message_principal"):
        properties = by_name[name]["inputSchema"]["properties"]
        assert "block-kit" in properties["markdown_body"]["description"]
        assert "html_body" not in properties
