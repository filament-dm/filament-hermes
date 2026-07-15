"""Structured logging helpers for the Filament Hermes plugin."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_hex
from typing import Any

import structlog


def _configure_structlog() -> None:
    """Configure structlog to render key=value messages into gateway.log."""
    if structlog.is_configured():
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.KeyValueRenderer(
                key_order=[
                    "event",
                    "level",
                    "logger",
                    "installation_id",
                    "gateway_instance_id",
                    "connect_attempt_id",
                    "fcm_client_id",
                    "push_receive_id",
                    "turn_id",
                    "call_origin",
                    "trigger_event_id",
                    "persistent_id",
                ],
                sort_keys=True,
            ),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


_configure_structlog()


_CONTEXT_KEYS = {
    "installation_id",
    "gateway_instance_id",
    "connect_attempt_id",
    "fcm_client_id",
    "push_receive_id",
    "turn_id",
    "call_origin",
    "trigger_event_id",
    "persistent_id",
}


def get_logger(name: str = "gateway.filament_fcm") -> Any:
    return structlog.get_logger(name)


def new_id(prefix: str) -> str:
    """Return a short opaque id safe to ask users to paste into support reports."""
    return f"{prefix}_{token_hex(5)}"


def fingerprint(value: str | None, *, size: int = 12) -> str | None:
    """Stable non-secret fingerprint for tokens/pushkeys/opaque identifiers."""
    if not value:
        return None
    return sha256(value.encode("utf-8")).hexdigest()[:size]


def current_context() -> dict[str, str]:
    """Return non-empty correlation fields for the current task."""
    return {
        key: value
        for key, value in structlog.contextvars.get_contextvars().items()
        if key in _CONTEXT_KEYS and isinstance(value, str) and value
    }


@contextlib.contextmanager
def bound_context(**values: str | None) -> Iterator[None]:
    """Temporarily bind correlation fields to the current context/task."""
    bind_values = {
        key: value for key, value in values.items() if value and key in _CONTEXT_KEYS
    }
    with structlog.contextvars.bound_contextvars(**bind_values):
        yield


@dataclass
class Stopwatch:
    started: float

    @classmethod
    def start(cls) -> Stopwatch:
        return cls(time.monotonic())

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)
