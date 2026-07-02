"""Firebase Cloud Messaging client wrapper.

Wraps the `firebase-messaging` library to handle:
  - FCM registration (Checkin API + GCM/FCM token)
  - Persistent MCS connection for receiving push notifications
  - Credential persistence across restarts
  - Parsing of Filament's DirectPusher payload format

The Filament server's DirectPusher sends FCM data messages
with this structure:
    {
        "body": "<JSON-serialized PushPayload>",
        "badge_count": "1",
        "room_name": "general",
        "message_text": "Hello from Alice",
        "from_directpusher": "true",
        "badge_only": "false",
    }

The "body" field contains the full PushPayload from the Filament server.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

from firebase_messaging import FcmPushClient, FcmRegisterConfig

from .credentials import CredentialStore

logger = logging.getLogger("gateway.filament_fcm")

# Enable firebase-messaging library logging so connection state is visible
# in the gateway logs.
_fb_logger = logging.getLogger("firebase_messaging")
_fb_logger.setLevel(logging.DEBUG)
if not _fb_logger.handlers:
    _fb_logger.addHandler(logging.StreamHandler())
    _fb_logger.propagate = True


# Filament Firebase project defaults — shared across all environments.
# These are public configuration values (same as what's baked into the
# Electron app's fcm-push-receiver.ts and the mobile app's
# google-services.json). Override via env vars if needed.
#
# We use the web app ID (same as the Electron desktop client) since the
# Hermes plugin is a non-mobile FCM client.
_DEFAULT_FIREBASE_PROJECT_ID = "filament-8ce44"
_DEFAULT_FIREBASE_API_KEY = "AIzaSyBtYzzP3IRpmIZ57dp1PMS4Y8RPjTB0snk"
_DEFAULT_FIREBASE_APP_ID = "1:143821144946:web:90e517a7f36aa42a6093eb"
_DEFAULT_FIREBASE_SENDER_ID = "143821144946"


@dataclass
class FCMConfig:
    """Firebase project configuration values."""

    project_id: str
    app_id: str
    api_key: str
    sender_id: str

    @classmethod
    def from_env(cls) -> "FCMConfig":
        """Read config from environment variables, falling back to defaults."""
        return cls(
            project_id=os.environ.get("FILAMENT_FIREBASE_PROJECT_ID")
            or _DEFAULT_FIREBASE_PROJECT_ID,
            app_id=os.environ.get("FILAMENT_FIREBASE_APP_ID")
            or _DEFAULT_FIREBASE_APP_ID,
            api_key=os.environ.get("FILAMENT_FIREBASE_API_KEY")
            or _DEFAULT_FIREBASE_API_KEY,
            sender_id=os.environ.get("FILAMENT_FIREBASE_SENDER_ID")
            or _DEFAULT_FIREBASE_SENDER_ID,
        )


@dataclass
class PushMessage:
    """A parsed push notification from Filament's DirectPusher."""

    event_id: str
    room_id: str
    room_name: str
    sender: str
    sender_display_name: str
    body: str
    is_direct: bool
    branch_type: str  # "direct_message", "channel_message", etc.
    thread_id: str | None  # thread root event ID, or None for main timeline
    is_mention: bool  # server flagged this as an @-mention of the agent
    is_everyone_mention: bool  # @everyone / @here
    raw: dict  # the full PushPayload dict


@dataclass
class InviteMessage:
    """A parsed invite notification from Filament's DirectPusher."""

    room_id: str
    branch_type: str  # "add_to_channel" or "add_to_space"
    inviter: str  # display name of the inviter
    inviter_id: str  # mxid of the inviter
    room_name: str  # channel name or space name
    raw: dict


@dataclass
class ReactionMessage:
    """A parsed emoji-reaction notification from Filament's DirectPusher.

    Reactions are wake-up signals: a reactor adds an emoji to a target message.
    """

    event_id: str  # the reaction event's own id (used for dedup)
    room_id: str
    room_name: str
    sender: str  # the reactor's id
    sender_display_name: str
    key: str  # the emoji
    target_event_id: str  # the message that was reacted to
    removed: bool  # True for un-reacts (ignored upstream)
    is_direct: bool
    thread_id: str | None
    raw: dict


# ── Envelope parsing ────────────────────────────────────────────────
#
# Every FCM data message from DirectPusher carries a JSON-serialized
# PushPayload in the ``body`` field.  The payload has a top-level
# ``type`` (for system messages like ``io.filament.ping``) or a
# ``branch`` dict whose ``type`` field discriminates the notification
# kind (message, invite, rechat receipt, etc.).
#
# ``parse_envelope`` handles the common work — JSON parse, branch
# extraction — so individual handlers receive pre-parsed dicts and
# never re-parse the body.


@dataclass
class Envelope:
    """The result of parsing the outer FCM data message.

    ``payload`` is the full deserialized PushPayload dict.
    ``branch`` is ``payload["branch"]`` (or ``None`` for branch-less
    payloads like ``io.filament.ping``).
    ``branch_type`` is ``branch["type"]`` for quick dispatch.
    """

    payload: dict
    branch: dict | None
    branch_type: str  # "" when no branch is present


def parse_envelope(data_message: dict[str, str]) -> Envelope | None:
    """Parse the outer FCM data message into an ``Envelope``.

    Returns ``None`` if the body is missing or not valid JSON.
    This is the single JSON-parse entry point — downstream handlers
    receive the pre-parsed ``Envelope`` and never call ``json.loads``
    themselves.
    """
    body_json = data_message.get("body")
    if not body_json:
        return None
    try:
        payload = json.loads(body_json)
    except json.JSONDecodeError:
        logger.warning("Failed to parse push payload body as JSON")
        return None
    if not isinstance(payload, dict):
        return None

    branch = payload.get("branch")
    if isinstance(branch, dict):
        return Envelope(
            payload=payload, branch=branch, branch_type=branch.get("type", "")
        )
    # Branch-less payloads (e.g. io.filament.ping) carry a top-level type.
    return Envelope(payload=payload, branch=None, branch_type=payload.get("type", ""))


# ── Branch handlers ─────────────────────────────────────────────────
#
# Each handler takes a pre-parsed Envelope and returns a typed
# dataclass, or None if the payload is malformed.


def _build_push_message(env: Envelope) -> PushMessage | None:
    """Build a ``PushMessage`` from a ``direct_message`` or
    ``channel_message`` envelope."""
    branch = env.branch
    if branch is None:
        return None

    # Extract message body from branch.content (Filament payload format)
    # or fall back to branch.body (legacy format).
    content = branch.get("content")
    if isinstance(content, dict):
        body = content.get("text", content.get("body", ""))
    else:
        body = branch.get("body", "")

    return PushMessage(
        event_id=env.payload.get("event_id", ""),
        room_id=env.payload.get("room_id", ""),
        room_name=branch.get("channel", branch.get("sender", "")),
        sender=branch.get("sender_id", branch.get("sender", "")),
        sender_display_name=branch.get("sender", ""),
        body=body,
        is_direct=env.payload.get("is_direct", False),
        branch_type=env.branch_type,
        thread_id=branch.get("thread_id"),
        is_mention=bool(branch.get("is_mention_of_recipient", False)),
        is_everyone_mention=bool(branch.get("is_everyone_mention", False)),
        raw=env.payload,
    )


def _build_invite_message(env: Envelope) -> InviteMessage | None:
    """Build an ``InviteMessage`` from an ``add_to_channel`` or
    ``add_to_space`` envelope."""
    branch = env.branch
    if branch is None:
        return None

    return InviteMessage(
        room_id=env.payload.get("room_id", ""),
        branch_type=env.branch_type,
        inviter=branch.get("inviter", ""),
        inviter_id=branch.get("inviter_id", ""),
        room_name=branch.get("channel", branch.get("space", "")),
        raw=env.payload,
    )


def _build_reaction(env: Envelope) -> "ReactionMessage | None":
    """Build a ``ReactionMessage`` from a ``reaction`` envelope."""
    branch = env.branch
    if branch is None:
        return None
    return ReactionMessage(
        event_id=env.payload.get("event_id", ""),
        room_id=env.payload.get("room_id", ""),
        room_name=branch.get("channel", ""),
        sender=branch.get("sender_id", branch.get("sender", "")),
        sender_display_name=branch.get("sender", ""),
        key=branch.get("key", ""),
        target_event_id=branch.get("target_event_id", ""),
        removed=bool(branch.get("removed", False)),
        is_direct=env.payload.get("is_direct", False),
        thread_id=branch.get("thread_id"),
        raw=env.payload,
    )


class FilamentFCMClient:
    """Manages FCM registration and message reception for a Filament agent."""

    def __init__(
        self,
        config: FCMConfig,
        on_message: Callable[["PushMessage"], Any],
        credentials: CredentialStore,
        on_ping: Callable[[dict], Any] | None = None,
        on_invite: Callable[["InviteMessage"], Any] | None = None,
        on_reaction: Callable[["ReactionMessage"], Any] | None = None,
        on_receiver_dead: Callable[[str], Any] | None = None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_ping = on_ping
        self._on_invite = on_invite
        self._on_reaction = on_reaction
        self._on_receiver_dead = on_receiver_dead
        self._credential_store = credentials
        self._push_client = None
        self._fcm_token: str | None = None
        self._stopped = False
        self._death_reported = False

    @property
    def fcm_token(self) -> str | None:
        """The FCM registration token, available after checkin_or_register."""
        return self._fcm_token

    async def checkin_or_register(self) -> str:
        """Register with FCM and return the push token.

        Loads saved credentials if available, otherwise performs a fresh
        registration with Google's Checkin and FCM APIs.
        """
        fcm_config = FcmRegisterConfig(
            self._config.project_id,
            self._config.app_id,
            self._config.api_key,
            self._config.sender_id,
        )

        saved_creds = self._credential_store.load_fcm_credentials()

        def on_credentials_updated(creds: dict) -> None:
            self._credential_store.save_fcm_credentials(creds)

        def on_notification(data: dict, persistent_id: str, obj: Any = None) -> None:
            self._handle_notification(data, persistent_id)

        self._push_client = FcmPushClient(
            callback=on_notification,
            fcm_config=fcm_config,
            credentials=saved_creds,
            credentials_updated_callback=on_credentials_updated,
        )

        self._fcm_token = await self._push_client.checkin_or_register()
        logger.info(
            "FCM token: %s...", self._fcm_token[:20] if self._fcm_token else "None"
        )
        return self._fcm_token

    async def start(self) -> None:
        """Start listening for push notifications and arm death detection.

        The underlying library spawns internal asyncio tasks and returns.
        Call ``stop()`` to cancel them.
        """
        if self._push_client is None:
            raise RuntimeError("Call checkin_or_register() before start()")

        logger.info("Starting FCM push listener")

        try:
            await self._push_client.start()
        except asyncio.CancelledError:
            logger.info("FCM push listener cancelled")
        except Exception:
            logger.exception("FCM push listener error")

        self._watch_receiver_tasks()

    async def stop(self) -> None:
        """Stop the FCM push listener by cancelling its internal tasks."""
        self._stopped = True
        if self._push_client is not None and hasattr(self._push_client, "tasks"):
            for task in self._push_client.tasks:
                if not task.done():
                    task.cancel()
        logger.info("FCM push listener stopped")

    # ── Death detection ────────────────────────────────────────────
    #
    # The library gives up in two ways, and both end at least one of its
    # internal tasks: _terminate() (sequential-error abort, heartbeat
    # loss, connect-retry exhaustion during a reset) cancels them all,
    # while an INITIAL connect that exhausts its retries just ends the
    # listen task — without _terminate(), leaving do_listen True and the
    # monitor task sleeping forever. Neither state recovers on its own,
    # so any internal task finishing before stop() means the receiver is
    # no longer listening and the owner must be told.

    def _watch_receiver_tasks(self) -> None:
        """Attach done-callbacks that report receiver death upward."""
        tasks = getattr(self._push_client, "tasks", None)
        if not tasks:
            logger.warning("filament-fcm: no push client tasks to watch")
            return
        for task in tasks:
            task.add_done_callback(self._on_push_task_done)

    def _on_push_task_done(self, task: asyncio.Task) -> None:
        if self._stopped or self._death_reported:
            return
        self._death_reported = True
        if task.cancelled():
            detail = "internal task cancelled"
        else:
            exc = task.exception()
            detail = (
                f"internal task crashed: {exc!r}" if exc else "internal task exited"
            )
        logger.error("FCM push receiver died (%s)", detail)
        if self._on_receiver_dead is not None:
            self._on_receiver_dead(detail)

    # ── Dispatch table ─────────────────────────────────────────────
    #
    # Maps ``branch.type`` (or top-level ``type`` for branch-less
    # payloads) to a handler method name.  Each handler receives the
    # pre-parsed ``Envelope`` and is responsible for building the typed
    # dataclass and invoking the appropriate callback.
    #
    # To add a new branch type (e.g. ``rechat_content``), define a
    # ``_dispatch_<name>`` method and add it here.

    _DISPATCH: ClassVar[dict[str, str]] = {
        "direct_message": "_dispatch_message",
        "channel_message": "_dispatch_message",
        "add_to_channel": "_dispatch_invite",
        "add_to_space": "_dispatch_invite",
        "reaction": "_dispatch_reaction",
        "io.filament.ping": "_dispatch_ping",
    }

    def _handle_notification(self, data: dict, persistent_id: str) -> None:
        """Called by firebase-messaging when a push arrives.

        Unwraps the outer FCM envelope, parses the body JSON once via
        ``parse_envelope``, and dispatches on ``branch_type`` to the
        appropriate handler method.
        """
        logger.info(
            "filament-fcm: FCM notification received (keys=%s, persistent_id=%s)",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            persistent_id,
        )

        if not isinstance(data, dict):
            logger.warning("filament-fcm: unexpected notification type: %s", type(data))
            return

        # The FCM payload wraps the DirectPusher data under a "data" key.
        inner = data.get("data", data)
        if not isinstance(inner, dict):
            logger.warning("filament-fcm: unexpected inner data type: %s", type(inner))
            return

        # Skip badge-only updates before parsing the body JSON.
        if inner.get("badge_only") == "true":
            logger.info("filament-fcm: skipping badge-only update")
            return

        # Parse body JSON once.
        env = parse_envelope(inner)
        if env is None:
            logger.warning(
                "filament-fcm: could not parse envelope: %s",
                json.dumps(inner, default=str),
            )
            return

        # Dispatch on branch type (or top-level type for branch-less payloads).
        handler_name = self._DISPATCH.get(env.branch_type)
        if handler_name is None:
            logger.debug("filament-fcm: unhandled branch type: %s", env.branch_type)
            return

        handler = getattr(self, handler_name)
        try:
            handler(env)
        except Exception:
            logger.exception(
                "filament-fcm: error in %s handler for %s",
                handler_name,
                env.branch_type,
            )

    def _dispatch_ping(self, env: Envelope) -> None:
        """Handle ``io.filament.ping`` — liveness probe from the principal."""
        logger.info(
            "filament-fcm: liveness ping received (nonce=%s)",
            env.payload.get("nonce"),
        )
        if self._on_ping is not None:
            self._on_ping(env.payload)

    def _dispatch_invite(self, env: Envelope) -> None:
        """Handle ``add_to_channel`` / ``add_to_space`` — room invite."""
        invite = _build_invite_message(env)
        if invite is None:
            return
        logger.info(
            "filament-fcm: invite received (%s to %s from %s)",
            invite.branch_type,
            invite.room_name or invite.room_id,
            invite.inviter,
        )
        if self._on_invite is not None:
            self._on_invite(invite)

    def _dispatch_reaction(self, env: Envelope) -> None:
        """Handle ``reaction`` — an emoji reaction, a wake-up signal."""
        reaction = _build_reaction(env)
        if reaction is None or reaction.removed:
            return  # ignore un-reacts
        logger.info(
            "filament-fcm: reaction %s by %s on %s in %s",
            reaction.key,
            reaction.sender_display_name or reaction.sender,
            reaction.target_event_id,
            reaction.room_name,
        )
        if self._on_reaction is not None:
            self._on_reaction(reaction)

    def _dispatch_message(self, env: Envelope) -> None:
        """Handle ``direct_message`` / ``channel_message`` — room message."""
        msg = _build_push_message(env)
        if msg is None:
            return
        logger.info(
            "Push from %s in %s: %s",
            msg.sender_display_name or msg.sender,
            msg.room_name,
            msg.body[:80] if msg.body else "(empty)",
        )
        self._on_message(msg)
