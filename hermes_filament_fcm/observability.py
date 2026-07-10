"""Structured logging helpers for the Filament Hermes plugin."""

from __future__ import annotations

import contextlib
import contextvars
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
                    "gateway_instance_id",
                    "connect_attempt_id",
                    "fcm_client_id",
                    "push_receive_id",
                    "turn_id",
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


def snippet(value: str | None, *, limit: int = 80) -> str | None:
    """Bounded single-line text for debug logs."""
    if value is None:
        return None
    flat = " ".join(str(value).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


_gateway_instance_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_gateway_instance_id", default=None
)
_connect_attempt_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_connect_attempt_id", default=None
)
_fcm_client_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_fcm_client_id", default=None
)
_push_receive_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_push_receive_id", default=None
)
_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_turn_id", default=None
)
_trigger_event_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_trigger_event_id", default=None
)
_persistent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "filament_persistent_id", default=None
)


_CONTEXT_VARS: dict[str, contextvars.ContextVar[str | None]] = {
    "gateway_instance_id": _gateway_instance_id,
    "connect_attempt_id": _connect_attempt_id,
    "fcm_client_id": _fcm_client_id,
    "push_receive_id": _push_receive_id,
    "turn_id": _turn_id,
    "trigger_event_id": _trigger_event_id,
    "persistent_id": _persistent_id,
}


def current_context() -> dict[str, str]:
    """Return non-empty correlation fields for the current task."""
    return {key: value for key, var in _CONTEXT_VARS.items() if (value := var.get())}


@contextlib.contextmanager
def bound_context(**values: str | None) -> Iterator[None]:
    """Temporarily bind correlation fields to the current context/task."""
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token]] = []
    bind_values = {key: value for key, value in values.items() if value}
    try:
        for key, value in bind_values.items():
            var = _CONTEXT_VARS.get(key)
            if var is None:
                continue
            tokens.append((var, var.set(value)))
        structlog.contextvars.bind_contextvars(**bind_values)
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*bind_values.keys())
        for var, token in reversed(tokens):
            var.reset(token)


@dataclass
class Stopwatch:
    started: float

    @classmethod
    def start(cls) -> Stopwatch:
        return cls(time.monotonic())

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)
