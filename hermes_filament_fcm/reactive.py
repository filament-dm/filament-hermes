"""Reactive-mode plumbing for the Filament FCM adapter.

Shared channels (everything except the principal's backchannel) run in
"reactive mode": an inbound event is a wake-up signal, not a command. The
adapter wakes the agent according to a tunable WAKE POLICY, and the agent acts
on the event data according to tunable STANDING INSTRUCTIONS — never treating the data
itself as instructions.

Both the standing instructions and the wake policy are *data the adapter reads
fresh on every event* (not startup config), so the principal can retune them
from the backchannel with the ``set_instructions`` / ``set_wake_policy`` tools,
and the next event uses the new value — no restart. ``current_zone`` is the
per-turn gate that keeps those tools control-plane-only.
"""

import contextvars
import json
import logging
import os
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger("gateway.filament_fcm")

# Per-turn trust zone. The adapter sets this immediately before dispatching a
# turn ("control" for the backchannel, "data" for shared channels); the
# control-plane tools (set_instructions/set_wake_policy) read it to refuse edits from
# a reactive turn. ContextVars are task-local, so concurrent turns don't race.
# Default "data" = fail-closed (no policy edits unless explicitly control).
current_zone: contextvars.ContextVar[str] = contextvars.ContextVar(
    "filament_zone", default="data"
)


def _default_dir() -> Path:
    return Path(
        os.environ.get("FILAMENT_FCM_CREDENTIALS_DIR")
        or (Path.home() / ".hermes" / "filament-fcm")
    )


class InstructionsStore:
    """The agent's standing instructions for reactive channels.

    Plain text on disk, read fresh on every wake so a backchannel edit takes
    effect on the next event. Not the agent's built-in memory (that's unkeyed,
    char-limited, and frozen at session start).

    Precedence: the principal's file (written by ``set_instructions``) wins; if
    it's absent or empty, fall back to the bundled ``default_instructions.md``
    (a safe generic starter: greet back, escalate other requests to the
    principal); if even that is unreadable, a hard-coded observe-silently string.
    """

    _BUNDLED = Path(__file__).parent / "default_instructions.md"
    _FALLBACK = "(No standing instructions set; observe silently, take no action.)"

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(
            path
            or os.environ.get("FILAMENT_INSTRUCTIONS_FILE")
            or _default_dir() / "instructions.md"
        )

    def read(self) -> str:
        for label, path in (("user", self._path), ("bundled-default", self._BUNDLED)):
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    logger.info(
                        "filament-fcm: loaded standing instructions (%s, %s, %d chars)",
                        label,
                        path,
                        len(text),
                    )
                    return text
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning(
                    "filament-fcm: failed to read %s", path, exc_info=True
                )
        logger.info("filament-fcm: no standing instructions found — using fallback")
        return self._FALLBACK

    def write(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, encoding="utf-8")
        logger.info("filament-fcm: standing instructions updated (%d bytes)", len(text))


class WakePolicyStore:
    """The wake policy — the cheap, pre-LLM gate deciding *whether* to spend a
    turn (separate from the standing instructions, which decide *what* to do).

    Declarative JSON on disk, read fresh per event:

        {
          "trigger_emojis": ["🐞", "🐛", "🤖"],   # reactions that wake
          "reactive_wake": "mention",               # "mention" | "all" | "off"
          "per_channel": {"<room_id>": {"reactive_wake": "all",
                                         "trigger_emojis": [...]}}
        }

    Defaults are conservative: respond only when @-mentioned, no reaction
    triggers, until the principal configures it from the backchannel.
    """

    _DEFAULTS: ClassVar[dict] = {
        "trigger_emojis": [],
        "reactive_wake": "mention",
        "per_channel": {},
    }

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(
            path
            or os.environ.get("FILAMENT_WAKE_POLICY_FILE")
            or _default_dir() / "wake_policy.json"
        )

    def read(self) -> dict:
        policy = dict(self._DEFAULTS)
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                policy.update(loaded)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("filament-fcm: failed to read wake policy", exc_info=True)
        return policy

    def write(self, policy: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(policy, indent=2), encoding="utf-8")
        logger.info("filament-fcm: wake policy updated")

    # ── Wake decisions (read fresh each call) ───────────────────────

    def _channel(self, policy: dict, room_id: str) -> dict:
        per = policy.get("per_channel") or {}
        return per.get(room_id, {}) if isinstance(per, dict) else {}

    def should_wake_message(self, room_id: str, is_mention: bool) -> bool:
        policy = self.read()
        ch = self._channel(policy, room_id)
        mode = ch.get("reactive_wake", policy.get("reactive_wake", "mention"))
        woke = mode == "all" or (mode != "off" and bool(is_mention))
        logger.info(
            "filament-fcm: wake(message) room=%s mode=%s mention=%s → %s",
            room_id,
            mode,
            is_mention,
            woke,
        )
        return woke

    def should_wake_reaction(self, room_id: str, emoji: str) -> bool:
        policy = self.read()
        ch = self._channel(policy, room_id)
        emojis = ch.get("trigger_emojis", policy.get("trigger_emojis", []))
        woke = emoji in (emojis or [])
        logger.info(
            "filament-fcm: wake(reaction) room=%s emoji=%s triggers=%s → %s",
            room_id,
            emoji,
            emojis,
            woke,
        )
        return woke
