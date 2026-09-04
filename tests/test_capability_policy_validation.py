"""Tests for ``_capability_policy_error`` in the package ``__init__``.

The validator lives in ``__init__.py``, which imports the Hermes ``gateway``
package at module level via ``adapter.py`` — not present in a bare test
environment. So the Hermes-side modules are stubbed (same shapes as
``test_media_notes.py``) and ``__init__.py`` is loaded under an alias module
name inside a real ``hermes_filament_fcm`` package entry, letting its
relative imports pull the actual submodules off disk.
"""

import importlib.util
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _install_stubs() -> None:
    fb = types.ModuleType("firebase_messaging")
    fb.FcmPushClient = type("FcmPushClient", (), {})
    fb.FcmRegisterConfig = type("FcmRegisterConfig", (), {})
    sys.modules["firebase_messaging"] = fb

    agent_pkg = types.ModuleType("agent")
    async_utils = types.ModuleType("agent.async_utils")
    async_utils.safe_schedule_threadsafe = lambda coro, loop, log_message="": None
    agent_pkg.async_utils = async_utils
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.async_utils"] = async_utils

    gateway_pkg = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = lambda name: name
    platforms_pkg = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _BaseAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

    base_mod.BasePlatformAdapter = _BaseAdapter
    base_mod.MessageEvent = type("MessageEvent", (), {})
    base_mod.MessageType = types.SimpleNamespace(TEXT="text")
    base_mod.ProcessingOutcome = type("ProcessingOutcome", (), {})
    base_mod.SendResult = type("SendResult", (), {})

    gateway_pkg.config = config_mod
    gateway_pkg.platforms = platforms_pkg
    platforms_pkg.base = base_mod
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_pkg
    sys.modules["gateway.platforms.base"] = base_mod

    hermes_cli_pkg = types.ModuleType("hermes_cli")
    setup_mod = types.ModuleType("hermes_cli.setup")
    for fn in (
        "get_env_value",
        "print_header",
        "print_info",
        "print_success",
        "print_warning",
        "prompt",
        "prompt_yes_no",
        "remove_env_value",
        "save_env_value",
    ):
        setattr(setup_mod, fn, lambda *a, **k: None)
    hermes_cli_pkg.setup = setup_mod
    sys.modules["hermes_cli"] = hermes_cli_pkg
    sys.modules["hermes_cli.setup"] = setup_mod

    try:
        import yaml  # noqa: F401, PLC0415
    except ImportError:
        yaml_mod = types.ModuleType("yaml")
        yaml_mod.safe_load = lambda *a, **k: {}
        yaml_mod.safe_dump = lambda *a, **k: ""
        sys.modules["yaml"] = yaml_mod


def _load_plugin_init():
    _install_stubs()
    pkg = types.ModuleType("hermes_filament_fcm")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hermes_filament_fcm"] = pkg
    spec = importlib.util.spec_from_file_location(
        "hermes_filament_fcm.plugin_init", _PKG_DIR / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_filament_fcm.plugin_init"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_init()


def test_valid_policy_passes():
    assert (
        plugin._capability_policy_error(
            {
                "default_capabilities": ["read_history", "post"],
                "bundles": {"calendar": ["list_events", "@directory"]},
                "per_channel": {"!room:x": ["calendar", "escalate"]},
            }
        )
        is None
    )


def test_mcp_prefix_rejected_as_custom_bundle_name():
    # 'mcp:' names are reserved for the automatic per-server bundles — a
    # custom definition would shadow the live expansion.
    err = plugin._capability_policy_error({"bundles": {"mcp:linear": ["create_issue"]}})
    assert err is not None and "reserved" in err


def test_toolset_prefix_rejected_as_custom_bundle_name():
    # Same rule for the other reserved spelling.
    err = plugin._capability_policy_error(
        {"bundles": {"toolset:spotify": ["spotify_search"]}}
    )
    assert err is not None and "reserved" in err


def test_toolset_grants_pass_validation_without_being_defined():
    # Auto-bundles resolve against the live registry at turn time, so there is
    # no name list to validate them against — an undefined-looking grant is
    # legal exactly like 'mcp:<server>'.
    assert (
        plugin._capability_policy_error(
            {
                "default_capabilities": ["read_history", "toolset:spotify"],
                "bundles": {"media": ["@toolset:spotify", "post_message"]},
                "per_channel": {"!room:x": ["toolset:web"]},
            }
        )
        is None
    )


def test_mcp_grants_accepted_in_grant_lists():
    # Grant lists (and @includes) may reference auto-bundles freely: they are
    # resolved against the live registry at turn time, so there is no name
    # list to validate them against here.
    assert (
        plugin._capability_policy_error(
            {
                "default_capabilities": ["read_history", "mcp:linear"],
                "bundles": {"pm": ["@mcp:linear", "post_message"]},
                "per_channel": {"!room:x": ["mcp:web"]},
                "per_user": {"@vip:x": ["mcp:calendar"]},
            }
        )
        is None
    )


def test_unknown_plain_grant_still_rejected():
    # The mcp: allowance must not loosen validation for ordinary names.
    err = plugin._capability_policy_error({"default_capabilities": ["no_such"]})
    assert err is not None and "no_such" in err
    err = plugin._capability_policy_error({"bundles": {"b": ["@no_such"]}})
    assert err is not None and "no_such" in err


def test_keep_visible_pins_only_registered_named_tools():
    # tool_search never defers a core tool; the instruction-named Filament
    # tools join that list, once, and nothing unregistered does.
    fake = types.ModuleType("toolsets")
    fake._HERMES_CORE_TOOLS = ["terminal"]
    sys.modules["toolsets"] = fake
    try:
        added = plugin._keep_visible(
            {"message_principal", "get_thread"} & set(plugin.ALWAYS_VISIBLE_TOOLS)
        )
        assert added == 2
        assert fake._HERMES_CORE_TOOLS == [
            "terminal",
            "get_thread",
            "message_principal",
        ]
        assert plugin._keep_visible({"message_principal"}) == 0
        assert fake._HERMES_CORE_TOOLS.count("message_principal") == 1
    finally:
        del sys.modules["toolsets"]


def test_keep_visible_without_a_core_list_is_a_no_op():
    sys.modules.pop("toolsets", None)
    assert plugin._keep_visible({"message_principal"}) == 0
