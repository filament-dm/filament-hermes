#!/usr/bin/env python3
"""Run Hermes's plugin security scan on this repo, the way an install would.

`hermes plugins install` / `update` (and the Hermes portal's install-from-GitHub
flow) scan the whole cloned tree and only a `safe` verdict installs
unattended: any HIGH finding makes it `caution`, which needs interactive
confirmation and so blocks the portal, and any CRITICAL makes it `dangerous`,
which cannot be forced. MEDIUM/LOW findings are informational.

The scanner lives in hermes-agent's `tools/` and, since late 2026, also
imports `hermes_cli` (to learn which env names hold model-provider keys), so
it runs from a whole hermes-agent checkout, with that checkout's Python
(`uv sync --frozen --no-dev --project <checkout>`, then
`<checkout>/.venv/bin/python scripts/plugin-scan.py ...`; Hermes needs Python
3.14). Give that checkout with --hermes-dir, or --ref to download upstream at
a ref (its dependencies must then be installed in the Python you run). It scans a
copy of exactly what a clone would contain: tracked files plus new,
non-ignored ones, as they are in the working tree.

    python3 scripts/plugin-scan.py --hermes-dir ~/.hermes/hermes-agent
    python3 scripts/plugin-scan.py --ref v2026.9.24   # downloads that ref
    python3 scripts/plugin-scan.py --all ...          # list MEDIUM/LOW findings too

Exits 0 on `safe`, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/{ref}"


def _download_hermes(dest: Path, ref: str) -> Path:
    """Unpack hermes-agent at `ref` into `dest`; returns the checkout's root."""
    with urllib.request.urlopen(ARCHIVE.format(ref=ref), timeout=60) as r:
        data = r.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    return next(dest.iterdir())


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
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--hermes-dir", type=Path, help="a hermes-agent checkout")
    where.add_argument("--ref", help="download hermes-agent at this ref")
    ap.add_argument("--all", action="store_true", help="also list MEDIUM/LOW findings")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        hermes = args.hermes_dir or _download_hermes(Path(tmp) / "hermes", args.ref)
        tree = Path(tmp) / "filament"
        tree.mkdir()
        _stage_tree(tree)

        sys.path.insert(0, str(Path(hermes).resolve()))
        from tools.plugin_guard import (  # noqa: PLC0415
            scan_plugin,
            should_allow_plugin_install,
        )

        try:
            result = scan_plugin(tree, source="filament-dm/filament-hermes")
        except ModuleNotFoundError as e:
            print(
                f"The scanner needs Hermes's dependencies ({e}): run "
                f"`uv sync --frozen --no-dev --project {hermes}` and use "
                f"{hermes}/.venv/bin/python",
                file=sys.stderr,
            )
            return 2
        _, reason = should_allow_plugin_install(result)

    scanner = args.hermes_dir or f"hermes-agent@{args.ref}"
    print(f"Scanner: {scanner}")
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
