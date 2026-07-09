"""The local ``download_media`` gateway tool (ENG-603).

The agents MCP surface deliberately never streams media bytes through
``tools/call`` (results are JSON): read tools surface an ``mxc_url``
reference in their ``media`` blocks, and the bytes come from the
``/mcp/agents/media`` side-channel. This module wraps that side-channel as a
local gateway tool so the agent can save an attachment to disk and work on
it with its other tools.

Lives in its own module (not ``__init__``) so it can be unit-tested without
pulling in the Hermes CLI/gateway packages.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .filament_api import FilamentAPI

logger = logging.getLogger("gateway.filament_fcm")

# Filenames are sender-controlled: reduce them to a safe basename component
# so a hostile name can't traverse directories or hide the file.
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LEN = 80

DOWNLOAD_MEDIA_SCHEMA: dict[str, Any] = {
    "name": "download_media",
    "description": (
        "Download a message attachment (image, file, video, audio) to local "
        "disk and return the saved file path. Get the mxc_url from the "
        "'media' block of get_thread / get_recent_messages results, or from "
        "an '[attachment: ...]' note in a message. Pass the attachment's "
        "filename too so the saved file keeps its extension."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mxc_url": {
                "type": "string",
                "description": "The attachment's mxc:// URL",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional original filename, used to name the saved file"
                ),
            },
        },
        "required": ["mxc_url"],
    },
}


def _safe_filename(name: str) -> str:
    """Flatten an untrusted filename into a safe basename component."""
    flat = _FILENAME_UNSAFE.sub("_", name or "").lstrip("._")
    # Keep the tail so the extension survives truncation.
    return flat[-_MAX_NAME_LEN:] if flat else ""


def media_dir() -> Path:
    """Directory downloaded attachments are saved to."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "filament_media"


def make_download_media_handler(api: FilamentAPI):
    """Create the async ``download_media`` tool handler bound to *api*."""

    async def handler(args: dict, **kwargs: Any) -> str:
        mxc_url = str(args.get("mxc_url") or "").strip()
        if not mxc_url.startswith("mxc://"):
            return json.dumps(
                {
                    "error": "mxc_url must be an mxc:// URL — find it in the "
                    "'media' block of get_thread / get_recent_messages "
                    "results"
                }
            )
        # Name the file by media id (unguessable, collision-free) plus the
        # sanitized original filename so the extension survives.
        media_id = _safe_filename(mxc_url.rstrip("/").rsplit("/", 1)[-1]) or "media"
        original = _safe_filename(str(args.get("filename") or ""))
        name = f"{media_id}-{original}" if original else media_id
        dest = media_dir()
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / name
        try:
            # Streamed straight to disk — media can be large (video/document).
            nbytes = await api.download_media(mxc_url, str(path))
        except Exception as exc:
            logger.exception("filament-fcm: download_media failed for %s", mxc_url)
            return json.dumps({"error": f"download failed: {exc}"})
        logger.info(
            "filament-fcm: downloaded %s (%d bytes) → %s", mxc_url, nbytes, path
        )
        return json.dumps({"ok": True, "path": str(path), "bytes": nbytes})

    handler.__name__ = "filament_download_media"
    handler.__qualname__ = "filament_download_media"
    return handler
