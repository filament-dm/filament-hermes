"""Exercise legacy imports in a fresh process, without Hermes installed."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_old_imports_resolve_to_the_maintained_source():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
import hermes_filament_fcm.credentials as credentials
import hermes_filament_fcm.reactive as reactive
from hermes_filament_fcm._version import version_headers
for module in (credentials, reactive):
    assert Path(module.__file__).parent.name == 'filament'
assert version_headers()['User-Agent'].startswith('filament/')
""",
        ],
        cwd=ROOT,
        check=True,
    )


def test_platform_selection_on_pm_layout_without_pyyaml(tmp_path):
    # pm-managed Hermes supplies hermes_yaml instead of PyYAML. The naming
    # module must use the same compatibility seam as setup_cli.
    (tmp_path / "hermes_yaml.py").write_text("def safe_load(stream): return {}\n")
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            """
from hermes_filament_fcm.naming import platform_name
assert platform_name() == 'filament'
""",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path),
            "PYTHONPATH": str(tmp_path),
        },
        check=True,
    )
