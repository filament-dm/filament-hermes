"""The local ``download_media`` gateway tool (ENG-603).

The agents MCP surface deliberately never streams media bytes through
``tools/call`` (results are JSON): read tools surface an ``mxc_url``
reference in their ``media`` blocks, and the bytes come from a signed URL
(``get_media_download_url``) or the ``/mcp/agents/media`` side-channel. This
module wraps both (``FilamentAPI.fetch_media`` picks) as one local gateway
tool, so the agent can save an attachment to disk in one call — no shell,
which a gated data turn may not have — and work on it with its other tools.

Lives in its own module (not ``__init__``) so it can be unit-tested without
pulling in the Hermes CLI/gateway packages.
"""

import json
import logging
import os
import re
import stat
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
        "an '[attachment: ...]' note in a message. Pass the message_id of "
        "the message it came from, and the attachment's filename so the "
        "saved file keeps its extension."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mxc_url": {
                "type": "string",
                "description": "The attachment's mxc:// URL",
            },
            "message_id": {
                "type": "string",
                "description": "The message_id of the message the attachment came from",
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


def local_file_problem(path: str) -> str | None:
    """Why the local file at *path* can't be sent as an attachment, or None.

    Hermes has already checked a ``MEDIA:`` path against its own delivery
    policy (credential and system paths); this only makes sure it is a
    regular file this process can read, so the upload never streams a
    directory, a device or a FIFO.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"cannot read {os.path.basename(path)}: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return f"{os.path.basename(path)} is not a regular file"
    if not os.access(path, os.R_OK):
        return f"cannot read {os.path.basename(path)}"
    return None


# Inbound images handed to Hermes' vision on a control turn: the formats
# Hermes treats as images, under a cap so one message can't stall the turn
# on a huge download, and only the first few of a gallery.
INBOUND_IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
INBOUND_IMAGE_MAX_BYTES = 10 * 1024 * 1024
INBOUND_IMAGE_MAX_COUNT = 4


def inbound_images(media: Any) -> list[dict[str, Any]]:
    """The image descriptors in a get_thread ``media`` list worth fetching
    for vision: ``m.image`` in a format Hermes reads, not declared over the
    cap (an undeclared size is capped while streaming)."""
    picked: list[dict[str, Any]] = []
    for m in media if isinstance(media, list) else []:
        if len(picked) >= INBOUND_IMAGE_MAX_COUNT:
            break
        if not isinstance(m, dict) or m.get("msgtype") != "m.image":
            continue
        mxc = m.get("mxc_url")
        if not (isinstance(mxc, str) and mxc.startswith("mxc://")):
            continue
        if str(m.get("mimetype") or "").lower() not in INBOUND_IMAGE_TYPES:
            continue
        size = m.get("size")
        if isinstance(size, int) and size > INBOUND_IMAGE_MAX_BYTES:
            continue
        picked.append(m)
    return picked


# Leading bytes of PNG and JPEG files (GIF and WebP are spelled in ASCII).
_PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
_JPEG_MAGIC = bytes.fromhex("ffd8ff")


def _looks_like_image(path: Path) -> bool:
    """Whether *path* starts like one of the INBOUND_IMAGE_TYPES formats.
    The mimetype is the sender's word; vision gets only real images."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return (
        head.startswith(_PNG_MAGIC)
        or head.startswith(_JPEG_MAGIC)
        or head[:6] in (b"GIF87a", b"GIF89a")
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    )


async def fetch_inbound_images(
    api: FilamentAPI, media: Any, message_id: str, dest_dir: Path
) -> tuple[list[str], list[str]]:
    """Download *message_id*'s images (see inbound_images) into *dest_dir*.

    Returns ``(paths, mimetypes)`` for a MessageEvent's ``media_urls`` /
    ``media_types``. Never raises: an image that fails to download, or isn't
    one, is logged and left out, and the turn keeps its attachment note.
    """
    paths: list[str] = []
    types: list[str] = []
    picked = inbound_images(media)
    if not picked:
        return paths, types
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("filament-fcm: no directory for inbound images", exc_info=True)
        return paths, types
    for m in picked:
        mxc = m["mxc_url"]
        mimetype = str(m["mimetype"]).lower()
        media_id = _safe_filename(mxc.rstrip("/").rsplit("/", 1)[-1]) or "media"
        path = dest_dir / f"filament_{media_id}{INBOUND_IMAGE_TYPES[mimetype]}"
        try:
            await api.fetch_media(
                mxc, str(path), message_id=message_id, max_bytes=INBOUND_IMAGE_MAX_BYTES
            )
        except Exception as exc:
            logger.warning(
                "filament-fcm: could not fetch image %s for vision: %s", mxc, exc
            )
            continue
        if not _looks_like_image(path):
            logger.warning("filament-fcm: %s is not an image; not attaching it", mxc)
            path.unlink(missing_ok=True)
            continue
        paths.append(str(path))
        types.append(mimetype)
    return paths, types


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
        message_id = str(args.get("message_id") or "").strip() or None
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
            nbytes = await api.fetch_media(mxc_url, str(path), message_id=message_id)
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
