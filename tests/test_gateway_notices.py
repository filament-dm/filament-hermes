"""Hermes's shutdown and restart notices stay out of the owner's chat.

Hermes posts "⚠️ Hermes is shutting down — your current task will be
interrupted…" to every platform's home channel on every gateway stop or
restart, idle or not. Filament's home channel is the owner's private chat
with the agent, so the owner saw it on each restart, the sign-in's own
hand-over and a reconnect included (live runs on 2026-11-13). The adapter
turns the notices off on its platform config, unless FILAMENT_GATEWAY_NOTICES
asks to keep them.

The adapter is loaded with the Hermes stubs ``test_media_send`` installs.
"""

import importlib.util
import types
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_media_send", Path(__file__).with_name("test_media_send.py")
)
_media_send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_media_send)
adapter = _media_send.adapter


def _config():
    # Hermes's PlatformConfig defaults gateway_restart_notification to True.
    return types.SimpleNamespace(gateway_restart_notification=True, extra={})


def test_the_notices_are_off_for_filament(monkeypatch):
    monkeypatch.delenv(adapter.GATEWAY_NOTICES_ENV, raising=False)
    config = _config()
    adapter._quiet_gateway_notices(config)
    assert config.gateway_restart_notification is False


def test_an_operator_can_keep_them(monkeypatch):
    monkeypatch.setenv(adapter.GATEWAY_NOTICES_ENV, "true")
    config = _config()
    adapter._quiet_gateway_notices(config)
    assert config.gateway_restart_notification is True


def test_a_config_without_the_setting_is_left_alone(monkeypatch):
    # An older Hermes whose PlatformConfig has no such field: nothing to set.
    monkeypatch.delenv(adapter.GATEWAY_NOTICES_ENV, raising=False)
    config = types.SimpleNamespace(extra={})
    adapter._quiet_gateway_notices(config)
    assert not hasattr(config, "gateway_restart_notification")


def test_the_adapter_applies_it_before_hermes_reads_its_config():
    source = (Path(adapter.__file__)).read_text(encoding="utf-8")
    init = source[source.index("class FCMFilamentAdapter") :]
    init = init[init.index("def __init__") :]
    assert init.index("_quiet_gateway_notices(config)") < init.index(
        "super().__init__(config"
    )
