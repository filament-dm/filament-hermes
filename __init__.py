"""Directory-plugin entry point: Hermes executes this file and calls register().

Must stay committed — `hermes plugins install` copies the cloned tree as-is.
"""

from __future__ import annotations

import sys
from pathlib import Path

# On sys.path before anything imports the package, which needs
# firebase_messaging. Rebuild it with scripts/vendor-deps.sh.
#
# Prepended, so the vendored copy wins. Nothing else in the process imports
# these names — Hermes declares none of them — so there is nobody to shadow,
# and losing to a stale copy left in site-packages by an older install is a
# failure we have already hit: the agent connects, looks healthy, and never
# receives a push.
#
# Whether a package is vendored is decided by whether it is in this tree, not by
# where the tree sits on the path. If Hermes ever ships one of these, drop it
# from vendor/ and widen the range in pyproject rather than reordering here —
# deps.vendor_shadow_warnings() watches for exactly that.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))


def register(ctx) -> None:
    """Register the Filament platform and its tools."""
    # Import here, not at the top. pytest imports this file before every test. A
    # top-level import of the package then fails, and every test errors. Hermes
    # calls register() inside the try/except that guards the module exec, so an
    # ImportError is still reported as the plugin's load error.
    from .hermes_filament_fcm import register as _register  # noqa: PLC0415

    return _register(ctx)


__all__ = ["register"]
