#!/usr/bin/env python3
"""Run Hermes's plugin security scan on this repo, the way an install would.

`hermes plugins install` / `update` (and the Hermes portal's install-from-GitHub
flow) scan the whole cloned tree and only a `safe` verdict installs
unattended: any HIGH finding makes it `caution`, which needs interactive
confirmation and so blocks the portal, and any CRITICAL makes it `dangerous`,
which cannot be forced. MEDIUM/LOW findings are informational.

The scanner is three stdlib-only modules in hermes-agent. This fetches them
from upstream at a ref (default: main) — or reads them from a local checkout
with --hermes-dir — and scans a copy of exactly what a clone would contain:
tracked files plus new, non-ignored ones, as they are in the working tree.

    python3 scripts/plugin-scan.py              # upstream main
    python3 scripts/plugin-scan.py --ref v2026.9.24
    python3 scripts/plugin-scan.py --hermes-dir ~/.hermes/hermes-agent
    python3 scripts/plugin-scan.py --all        # list MEDIUM/LOW findings too

Exits 0 on `safe`, 1 otherwise.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER_FILES = ("plugin_guard.py", "plugin_guard_context.py", "skills_guard.py")
UPSTREAM = "https://raw.githubusercontent.com/NousResearch/hermes-agent"


def _stage_scanner(dest: Path, ref: str, hermes_dir: Path | None) -> None:
    tools = dest / "tools"
    tools.mkdir(parents=True)
    (tools / "__init__.py").write_text("")
    for name in SCANNER_FILES:
        if hermes_dir is not None:
            shutil.copyfile(hermes_dir / "tools" / name, tools / name)
            continue
        with urllib.request.urlopen(f"{UPSTREAM}/{ref}/tools/{name}", timeout=30) as r:
            (tools / name).write_bytes(r.read())


def _stage_tree(dest: Path) -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for raw in filter(None, listed):
        rel = raw.decode()
        src = ROOT / rel
        if not src.is_file():  # deleted in the working tree
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="main", help="hermes-agent ref (default: main)")
    ap.add_argument("--hermes-dir", type=Path, help="use a local hermes-agent checkout")
    ap.add_argument("--all", action="store_true", help="also list MEDIUM/LOW findings")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        scanner, tree = Path(tmp) / "scanner", Path(tmp) / "filament"
        _stage_scanner(scanner, args.ref, args.hermes_dir)
        tree.mkdir()
        _stage_tree(tree)

        sys.path.insert(0, str(scanner))
        from tools.plugin_guard import (  # noqa: PLC0415
            scan_plugin,
            should_allow_plugin_install,
        )

        result = scan_plugin(tree, source="filament-dm/filament-hermes")
        _, reason = should_allow_plugin_install(result)

    where = args.hermes_dir or f"hermes-agent@{args.ref}"
    print(f"Scanner: {where}")
    print(f"Verdict: {result.verdict.upper()} — {reason}")
    shown = [
        f for f in result.findings if args.all or f.severity in ("critical", "high")
    ]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in sorted(shown, key=lambda f: order.get(f.severity, 4)):
        where = f"{f.file}:{f.line}"
        print(f"  {f.severity.upper():8} {f.category:13} {where}  {f.match[:70]!r}")
    hidden = len(result.findings) - len(shown)
    if hidden:
        print(f"  (+{hidden} MEDIUM/LOW, informational — --all to list)")
    return 0 if result.verdict == "safe" else 1


if __name__ == "__main__":
    sys.exit(main())
