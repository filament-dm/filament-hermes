"""Hermes import paths the plugin must never use.

Hermes's September 2026 reorganisation moved names to new modules and kept the
old paths alive only briefly: releases from v2026.9.14 until the compat layer's
removal disable any plugin that still imports one (``hermes plugins compat``),
and current Hermes has no old paths left at all. One stale import takes the whole
plugin down, so this scans the package source statically (it can't be imported
without Hermes).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# old module -> where its names live now
REMOVED = {
    "tools.mcp_tool": "tools.mcp_tool_discovery",
}


def _imports():
    package = sorted((ROOT / "hermes_filament_fcm").glob("*.py"))
    for path in [ROOT / "__init__.py", *package]:
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield path, node.lineno, node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    yield path, node.lineno, alias.name


def test_no_imports_from_paths_hermes_removed():
    stale = [
        f"{path.relative_to(ROOT)}:{line} imports {module} (use {REMOVED[module]})"
        for path, line, module in _imports()
        if module in REMOVED
    ]
    assert not stale, "\n".join(stale)
