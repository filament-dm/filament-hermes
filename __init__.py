"""Directory-plugin entry point: Hermes executes this file and calls register().

Must stay committed — `hermes plugins install` copies the cloned tree as-is.
"""

from __future__ import annotations

import sys
from pathlib import Path

# On sys.path before anything imports the package, which needs
# firebase_messaging. Appended, not prepended, so an installed copy wins per
# package and vendor/ fills only what is missing. Rebuild it with
# scripts/vendor-deps.sh.
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
