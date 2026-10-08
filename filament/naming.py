"""Names used for fresh installs and compatibility with existing Hermes sessions."""

import os
from pathlib import Path

# Match setup_cli on both Hermes layouts: pm-managed releases use hermes_yaml.
try:
    import yaml
except ImportError:
    import hermes_yaml as yaml

PLATFORM_NAME = "filament"
LEGACY_PLATFORM_NAME = "filament-fcm"


def platform_name() -> str:
    """Keep existing Hermes conversation keys; use filament for new installs."""
    configured_home = os.environ.get("HERMES_HOME")
    home = Path(configured_home) if configured_home else Path.home() / ".hermes"
    config_path = home / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        for section in (config, config.get("display") or {}):
            if LEGACY_PLATFORM_NAME in (section.get("platforms") or {}):
                return LEGACY_PLATFORM_NAME
    # State can precede config (old installer and custom HERMES_HOME installs).
    if (home / LEGACY_PLATFORM_NAME).exists():
        return LEGACY_PLATFORM_NAME
    if (
        home.parent.name != "profiles"
        and (Path.home() / ".hermes" / LEGACY_PLATFORM_NAME).exists()
    ):
        return LEGACY_PLATFORM_NAME
    return PLATFORM_NAME
