"""The settings of the Hermes profile this copy of the plugin serves.

A multiplexed Hermes gateway (``gateway.multiplex_profiles``) serves every
profile from one process. Each profile loads its own copy of this plugin, but
``os.environ`` holds only the launch profile's ``.env``: another profile's
values reach code only through the secret scope Hermes installs around that
profile's work, ``register()`` included. The adapter's tasks and threads run
outside that scope, so ``pin()`` copies the profile's values at registration
and ``setting()`` serves them from then on.

The launch profile, and any Hermes without profile scopes, keeps reading
``os.environ`` live, as the plugin always has. Stdlib-only: loaded
standalone by the tests.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

HOME_SETTING = "HERMES_HOME"


@dataclass(frozen=True)
class _Pin:
    home: Path
    # The process was launched for this profile: its .env is os.environ.
    launch: bool
    scoped: dict[str, str] = field(default_factory=dict)


# The one pin, set by pin(): a holder rather than a rebound global.
_pinned: list[_Pin] = []


def _current() -> _Pin | None:
    return _pinned[0] if _pinned else None


def _current_home() -> Path:
    """The Hermes home of the profile Hermes has scoped the current task to."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415

        return Path(get_hermes_home())
    except ImportError:
        home = os.environ.get(HOME_SETTING)
        return Path(home) if home else Path.home() / ".hermes"


def _read_scope(home: Path) -> _Pin:
    try:
        from agent.secret_scope import current_secret_scope  # noqa: PLC0415
        from hermes_constants import (  # noqa: PLC0415
            get_routing_process_hermes_home,
            hermes_home_key,
        )
    except ImportError:
        # A Hermes from before profile scopes: one profile per process.
        return _Pin(home=home, launch=True)
    launch = hermes_home_key(get_routing_process_hermes_home()) == hermes_home_key(home)
    return _Pin(home=home, launch=launch, scoped=dict(current_secret_scope() or {}))


def pin() -> None:
    """Remember the current profile's home and settings. Call from ``register()``,
    which Hermes runs inside the profile's scope."""
    _pinned[:] = [_read_scope(_current_home())]


def _refresh(current: _Pin) -> None:
    """Take this profile's values afresh when Hermes has scoped the current
    task to it, as it does to start the profile's adapter after a setup."""
    try:
        from agent.secret_scope import (  # noqa: PLC0415
            current_secret_scope,
            current_secret_scope_home,
        )
        from hermes_constants import hermes_home_key  # noqa: PLC0415
    except ImportError:
        return
    scope, home = current_secret_scope(), current_secret_scope_home()
    if (
        scope is not None
        and home
        and hermes_home_key(home) == hermes_home_key(current.home)
    ):
        current.scoped.update(scope)


def setting(name: str, default: str | None = None) -> str | None:
    """A setting (an environment variable's name) for this profile."""
    current = _current()
    if current is not None:
        _refresh(current)
    if current is not None and name == HOME_SETTING:
        return str(current.home)
    if current is None or current.launch:
        value = os.environ.get(name)
        if value is not None:
            return value
    if current is not None and name in current.scoped:
        return current.scoped[name]
    return default


def set_setting(name: str, value: str) -> None:
    """Change a setting for the rest of this gateway run. Only the launch
    profile's reaches ``os.environ``, which every profile's process shares."""
    current = _current()
    if current is None or current.launch:
        os.environ[name] = value
    if current is not None:
        current.scoped[name] = value


def hermes_home() -> Path:
    """This profile's Hermes home."""
    home = setting(HOME_SETTING)
    return Path(home) if home else Path.home() / ".hermes"


def profile_name() -> str:
    """This profile's name: ``default`` for the root home."""
    home = hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


@contextlib.contextmanager
def home_scope() -> Iterator[None]:
    """Point Hermes's own file helpers (``save_env_value`` and the like) at this
    profile's home, whichever task or thread this runs on."""
    current = _current()
    try:
        from hermes_constants import (  # noqa: PLC0415
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except ImportError:
        current = None
    if current is None:
        yield
        return
    token = set_hermes_home_override(str(current.home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)
