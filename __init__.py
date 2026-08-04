"""Directory-plugin entry point.

Hermes reads ``plugin.yaml`` for the manifest and this file for the code. Its
loader requires an ``__init__.py`` here, executes it, then calls ``register()``
(``hermes_cli/plugins.py:_load_directory_module``). The code lives in the nested
``hermes_filament_fcm`` package, so this file is a shim.

Keep this file committed. ``hermes plugins install`` copies the cloned tree as
it is, so a generated file does not exist in the installed plugin.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put the vendored dependencies on sys.path before anything imports the package.
# The package imports firebase_messaging, and no step of the install provides it:
# `hermes plugins install` is a git clone and runs neither pip nor uv, and the
# gateway runs as an unprivileged uid that cannot write /opt/hermes/.venv. See
# scripts/vendor-deps.sh for the tree.
#
# Append, do not prepend. Python then finds an installed copy first, for each
# package, and vendor/ supplies only what the machine does not have.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
    sys.path.append(str(_VENDOR_DIR))


def register(ctx) -> None:
    """Register the Filament platform and its tools."""
    # Import here, not at the top. pytest imports this file before every test. A
    # top-level import of the package then fails, and every test errors. Hermes
    # calls register() inside the try/except that guards the module exec, so an
    # ImportError is still reported as the plugin's load error.
    from .hermes_filament_fcm import register as _register  # noqa: PLC0415

    return _register(ctx)


__all__ = ["register"]
