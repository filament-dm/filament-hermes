"""Structured logging helpers for the Filament Hermes plugin.

``structlog`` is a real dependency (see pyproject / install.sh), but it is
imported **defensively** here: this module is pulled in at plugin-load time (the
adapter and fcm_client create a module-level logger), so a hard ``import
structlog`` would make the whole plugin fail to load if the dependency is ever
missing — e.g. after a code-only ``hermes plugins update`` that pulled new code
before the deps were refreshed. Instead we fall back to a small stdlib-logging
shim so the gateway keeps running with (plainer) logs; ``deps.py`` nudges the
principal to refresh dependencies to restore full structured logging.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_hex
from typing import Any

try:
    import structlog
except ImportError:  # pragma: no cover - exercised only when the dep is absent
    structlog = None


class _FallbackLogger:
    """Minimal structlog-style logger over stdlib logging.

    Used only when ``structlog`` is not installed. Supports the surface the
    plugin uses: ``bind(**)`` plus ``debug/info/warning/error/exception(event,
    **fields)``, rendering ``event key=value`` so logs stay readable.
    """

    def __init__(self, name: str = "gateway.filament_fcm", **bound: Any) -> None:
        self._logger = logging.getLogger(name)
        self._bound = bound

    def bind(self, **kw: Any) -> _FallbackLogger:
        return _FallbackLogger(self._logger.name, **{**self._bound, **kw})

    def _emit(self, level: int, event: str, exc_info: bool = False, **kw: Any) -> None:
        fields = {**self._bound, **kw}
        suffix = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        tail = f" {suffix}" if suffix else ""
        self._logger.log(level, "%s%s", event, tail, exc_info=exc_info)

    def debug(self, event: str = "", **kw: Any) -> None:
        self._emit(logging.DEBUG, event, **kw)

    def info(self, event: str = "", **kw: Any) -> None:
        self._emit(logging.INFO, event, **kw)

    def warning(self, event: str = "", **kw: Any) -> None:
        self._emit(logging.WARNING, event, **kw)

    def error(self, event: str = "", **kw: Any) -> None:
        self._emit(logging.ERROR, event, **kw)

    def exception(self, event: str = "", **kw: Any) -> None:
        kw.pop("exc_info", None)
        self._emit(logging.ERROR, event, exc_info=True, **kw)


def _configure_structlog() -> None:
    """Configure structlog to render key=value messages into gateway.log."""
    if structlog is None or structlog.is_configured():
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
    if structlog is None:
        return _FallbackLogger(name)
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
    if structlog is None:
        return {}
    return {
        key: value
        for key, value in structlog.contextvars.get_contextvars().items()
        if key in _CONTEXT_KEYS and isinstance(value, str) and value
    }


@contextlib.contextmanager
def bound_context(**values: str | None) -> Iterator[None]:
    """Temporarily bind correlation fields to the current context/task."""
    if structlog is None:
        yield
        return
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
