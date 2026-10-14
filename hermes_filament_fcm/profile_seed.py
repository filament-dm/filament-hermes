"""Seed the Filament profile from the Hermes agent's own identity.

A new Filament agent arrives named "<Owner>'s agent" (numbered from the
second), or "My agent" when the owner has no display name, with the app's stock
picture. On connect, while the profile still carries that server default, the
plugin gives it the Hermes agent's name and avatar. A name the owner chose in
the app is never touched, and neither is its picture: a name that is still the
default is how we know the picture is still the stock one too.

Hermes has no name field of its own. What it does have, in order:

- ``$HERMES_HOME/profile.yaml`` ``display_name`` (or the desktop app's
  ``ui_meta.hermes-bots.title``), set when the owner names a profile;
- the persona in ``$HERMES_HOME/SOUL.md``, whose stock text opens "You are
  Hermes Agent, built by Nous Research." — an owner who rewrote it to "You are
  Ada, …" has named the agent Ada;
- the profile's own id (``~/.hermes/profiles/<id>``) for a named profile.

Failing all of those the agent is plainly Hermes, so it becomes "Hermes" —
numbered "Hermes 2" … when the owner's other agents hold the plain name. Skins
are left out on purpose: a built-in skin brands the banner "Ares Agent" or
"Poseidon Agent" as a colour theme, not as a choice to rename the agent.

The avatar is the profile's ``assets/avatar.{png,jpg,webp}`` (what the Hermes
desktop app saves), else the Hermes app icon from the Hermes install.

Stdlib-only (YAML through an injected loader) and free of Hermes imports, so it
is unit-tested standalone in ``tests/test_profile_seed.py``.
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("gateway.filament_fcm")

FALLBACK_NAME = "Hermes"

# How many names to try: the plain one, then "<name> 2" … "<name> 5".
NAME_ATTEMPTS = 5

# Profile ids that name no profile in particular (hermes_cli.profiles).
_ANONYMOUS_PROFILES = frozenset({"", "default", "custom"})

# Names that only repeat what every Hermes is.
_STOCK_NAMES = frozenset({"hermes", "hermes agent"})

# The desktop app's avatar slot, in its own lookup order.
_AVATAR_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

# The Hermes app icon, relative to the hermes-agent checkout. Shipped in every
# git install (the installer clones the whole repository).
_HERMES_ICON = Path("apps") / "desktop" / "assets" / "icon.png"

# Larger avatars go through the server as a data URL; past this it is a photo,
# not an avatar, and the request would be the wrong size for one.
_MAX_AVATAR_BYTES = 2 * 1024 * 1024

_WHITESPACE = re.compile(r"\s+")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# "You are Ada, …", "You are Ada Lovelace.", "- **You are Ada** —". One to three
# capitalised words, so "You are a pirate" and "You are an expert" are no name.
_SOUL_NAME = re.compile(
    r"^[\s>*_#-]*You are (?:called |named )?"
    r"([A-Z][\w'\u2019.-]*(?: [A-Z][\w'\u2019.-]*){0,2}?)"
    r"(?=\*|_|[,.;:!\u2014\u2013]|\s+[-\u2014\u2013(]"
    r"|\s+(?:a|an|the|and|who|from|of|by|built)\b|\s*$)",
    re.M,
)


def normalize_name(name: str) -> str:
    """The key the server compares agent names on (agent_names.py)."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", name)).strip().casefold()


def is_default_agent_name(display_name: Any, owner_name: Any) -> bool:
    """Whether ``display_name`` is still the server's default.

    "<Owner>'s agent", "<Owner>'s agent 2", …, or "My agent …" for an owner
    with no display name. A missing name counts as default: nothing the owner
    chose would be lost."""
    if not isinstance(display_name, str) or not display_name.strip():
        return True
    name = normalize_name(display_name)
    bases = ["my agent"]
    if isinstance(owner_name, str) and owner_name.strip():
        bases.append(f"{normalize_name(owner_name)}'s agent")
    return any(
        name == base or re.fullmatch(re.escape(base) + r" \d+", name) for base in bases
    )


def soul_name(text: str | None) -> str | None:
    """The name a SOUL.md persona gives the agent, if it gives one.

    A guess by construction — the persona is free prose — so it only accepts
    the one shape a name reliably takes there, and never Hermes's own."""
    if not text:
        return None
    match = _SOUL_NAME.search(_HTML_COMMENT.sub("", text))
    if not match:
        return None
    name = match.group(1).rstrip(".'\u2019-")
    if not name or normalize_name(name) in _STOCK_NAMES:
        return None
    return name


def seed_name(
    *,
    profile_meta: dict | None,
    soul_text: str | None,
    profile_name: str | None,
) -> str:
    """The name to give the Filament agent (see the module docstring)."""
    meta = profile_meta if isinstance(profile_meta, dict) else {}
    ui_meta = meta.get("ui_meta")
    bots = ui_meta.get("hermes-bots") if isinstance(ui_meta, dict) else None
    candidates = [
        meta.get("display_name"),
        bots.get("title") if isinstance(bots, dict) else None,
        soul_name(soul_text),
        None if (profile_name or "") in _ANONYMOUS_PROFILES else profile_name,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            name = _WHITESPACE.sub(" ", candidate).strip()
            if normalize_name(name) not in _STOCK_NAMES:
                return name
            break
    return FALLBACK_NAME


def find_avatar(hermes_home: Path, hermes_root: Path | None) -> Path | None:
    """The profile's own avatar, else the Hermes app icon, else None."""
    for ext in _AVATAR_TYPES:
        path = hermes_home / "assets" / f"avatar{ext}"
        if path.is_file():
            return path
    if hermes_root is not None:
        icon = hermes_root / _HERMES_ICON
        if icon.is_file():
            return icon
    return None


def avatar_data_url(path: Path) -> str | None:
    """``path`` as a ``data:`` URL set_profile accepts, or None if unusable."""
    content_type = _AVATAR_TYPES.get(path.suffix.lower())
    if content_type is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > _MAX_AVATAR_BYTES:
        return None
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


# One tool call: (parsed result, error). The error is the JSON-RPC rejection or
# transport failure; a refusal the server words inside an ok result comes back
# as the parsed result and set_profile_error reads it.
ToolCall = Callable[[str, dict], Awaitable[tuple[Any, str | None]]]


def set_profile_error(data: Any, error: str | None) -> str | None:
    """Why set_profile did not apply, if it did not.

    A taken name or an unusable image comes back as a successful call whose
    result is ``{"error": "…"}``, so a call without a JSON-RPC error is not
    yet success."""
    if error:
        return error
    if isinstance(data, dict):
        inner = data.get("error")
        if isinstance(inner, str) and inner:
            return inner
        result = data.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return "the server refused it"
    return None


def _name_taken(reason: str) -> bool:
    return bool(re.search(r"already uses that name|name.*taken", reason, re.I))


@dataclass
class SeedResult:
    name: str | None = None  # the name the profile now has, when this set it
    image: bool = False


async def _set_name(call: ToolCall, wanted: str) -> str | None:
    """Set ``wanted``, or "<wanted> 2" … when the owner's agents hold it."""
    for n in range(1, NAME_ATTEMPTS + 1):
        candidate = wanted if n == 1 else f"{wanted} {n}"
        data, error = await call("set_profile", {"name": candidate})
        refused = set_profile_error(data, error)
        if refused is None:
            # The owner can lock the name in the app; the server then keeps the
            # old one and still answers with the profile, so check it took.
            applied = data.get("display_name") if isinstance(data, dict) else None
            if isinstance(applied, str) and normalize_name(applied) != normalize_name(
                candidate
            ):
                logger.info(
                    "filament-fcm: profile name stayed %r (locked by the owner)",
                    applied,
                )
                return None
            return candidate
        if not _name_taken(refused):
            logger.info(
                "filament-fcm: the profile name %r was refused: %s", candidate, refused
            )
            return None
    logger.info(
        "filament-fcm: %r and its numbered forms are all taken; name left as is",
        wanted,
    )
    return None


async def seed_profile(
    call: ToolCall,
    *,
    display_name: str | None,
    owner_name: str | None,
    wanted_name: str | None,
    avatar: Path | None,
) -> SeedResult | None:
    """Give a default-named profile the Hermes agent's name and avatar.

    Returns what was set, or None when nothing was (an owner-chosen name, or
    every attempt refused). Never raises: a profile it cannot seed stays as
    the server made it."""
    try:
        if not is_default_agent_name(display_name, owner_name):
            return None
        result = SeedResult()
        # Name and picture go in separate calls: the server refuses the whole
        # call over either one, and a taken name must not cost the picture.
        if wanted_name and normalize_name(wanted_name) != normalize_name(
            display_name or ""
        ):
            result.name = await _set_name(call, wanted_name)
        image = avatar_data_url(avatar) if avatar is not None else None
        if avatar is not None and image is None:
            logger.info(
                "filament-fcm: avatar %s is not an image Filament takes", avatar
            )
        if image is not None:
            data, error = await call("set_profile", {"image": image})
            refused = set_profile_error(data, error)
            if refused:
                logger.info("filament-fcm: the avatar was refused: %s", refused)
            else:
                result.image = True
        if result.name is None and not result.image:
            return None
        logger.info(
            "filament-fcm: profile seeded from the Hermes agent (name=%r, avatar=%s)",
            result.name,
            avatar if result.image else None,
        )
        return result
    except Exception:
        logger.warning("filament-fcm: profile seeding failed", exc_info=True)
        return None
