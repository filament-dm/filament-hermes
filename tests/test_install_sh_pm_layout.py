"""Guards for install.sh's detection of pm-layout Hermes.

Newer Hermes has no venv at a fixed path: its launcher runs a Hermes-managed
Python, and the dependencies live in an environment generation whose path
changes. install.sh asks the launcher (``hermes --run-module site``) which
generation it runs. These tests run that block for real against fake launchers,
under /bin/bash too when it exists (bash 3.2 on macOS, what ``curl | bash``
runs there).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"


def _block() -> str:
    lines = INSTALL_SH.read_text().splitlines()
    start = lines.index(
        "# BEGIN pm-detect (extracted and run by tests/test_install_sh_pm_layout.py)"
    )
    end = lines.index("# END pm-detect")
    return "\n".join(lines[start : end + 1])


def _bashes() -> list[str]:
    found = [b for b in (shutil.which("bash"), "/bin/bash") if b and Path(b).exists()]
    return list(dict.fromkeys(found))


def _venv(path: Path) -> Path:
    (path / "bin").mkdir(parents=True)
    python = path / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (path / "pyvenv.cfg").write_text("home = /nowhere\n")
    (path / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    return path


def _launcher(bindir: Path, body: str) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "hermes"
    launcher.write_text("#!/bin/sh\n" + body)
    launcher.chmod(0o755)
    return launcher


def _site_output(*entries: str) -> str:
    # The shape `python -m site` prints.
    rows = "".join(f"    '{e}',\n" for e in entries)
    return (
        f"cat <<'EOF'\nsys.path = [\n{rows}]\n"
        "USER_BASE: '/home/u/.local' (exists)\nENABLE_USER_SITE: False\nEOF\n"
    )


def _run(bash: str, tmp_path: Path, *, venv: str = "", path_dirs=(), stdin="") -> dict:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"HERMES_HOME={tmp_path / 'home'}",
            'is_venv() { [ -x "$1/bin/python" ]; }',
            f'VENV="{venv}"',
            _block(),
            'printf "PM_LAYOUT=%s\\nVENV=%s\\nPM_LAUNCHER=%s\\n" '
            '"$PM_LAYOUT" "$VENV" "$PM_LAUNCHER"',
        ]
    )
    env_path = ":".join([*map(str, path_dirs), "/usr/bin", "/bin"])
    out = subprocess.run(
        [bash, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
        env={"PATH": env_path},
    )
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


@pytest.fixture(params=_bashes())
def bash(request):
    return request.param


def test_finds_the_generation_the_launcher_runs(bash, tmp_path):
    venv = _venv(tmp_path / "installs" / "x" / "environments" / "abc" / "venv")
    store = tmp_path / "tools" / "python-3.14"
    (store / "bin").mkdir(parents=True)
    (store / "bin" / "python").write_text("")
    (store / "bin" / "python").chmod(0o755)
    launcher = _launcher(
        tmp_path / "bin",
        _site_output(
            "/opt/hermes-agent",
            f"{venv}/lib/python3.14/site-packages",
            f"{store}/lib/python3.14",
            # The store Python has a bin/python but is no venv (no pyvenv.cfg).
            f"{store}/lib/python3.14/site-packages",
        ),
    )
    got = _run(bash, tmp_path, path_dirs=[tmp_path / "bin"])
    assert got == {"PM_LAYOUT": "1", "VENV": str(venv), "PM_LAUNCHER": str(launcher)}


def test_skips_a_venv_less_store_python(bash, tmp_path):
    # Only the store Python on the path (a bin/python, no pyvenv.cfg): no venv.
    store = tmp_path / "tools" / "python-3.14"
    (store / "bin").mkdir(parents=True)
    (store / "bin" / "python").write_text("")
    (store / "bin" / "python").chmod(0o755)
    _launcher(tmp_path / "bin", _site_output(f"{store}/lib/python3.14/site-packages"))
    got = _run(bash, tmp_path, path_dirs=[tmp_path / "bin"])
    assert got["PM_LAYOUT"] == "0"
    assert got["VENV"] == ""


def test_an_older_launcher_that_rejects_the_flag_finds_nothing(bash, tmp_path):
    # Venv-layout Hermes has no --run-module; argparse errors out.
    _launcher(tmp_path / "bin", "echo 'hermes: unrecognized arguments' >&2\nexit 2\n")
    got = _run(bash, tmp_path, path_dirs=[tmp_path / "bin"])
    assert got == {"PM_LAYOUT": "0", "VENV": "", "PM_LAUNCHER": ""}


def test_falls_back_to_the_checkout_launcher_when_hermes_is_off_path(bash, tmp_path):
    venv = _venv(tmp_path / "gen" / "venv")
    launcher = _launcher(
        tmp_path / "home" / "hermes-agent" / ".hermes" / "bin",
        _site_output(f"{venv}/lib/python3.14/site-packages"),
    )
    got = _run(bash, tmp_path)
    assert got == {"PM_LAYOUT": "1", "VENV": str(venv), "PM_LAUNCHER": str(launcher)}


def test_never_runs_when_a_venv_layout_install_was_found(bash, tmp_path):
    # Backward compatibility: every host the venv checks already serve keeps
    # its path. A launcher that would answer is not even asked.
    marker = tmp_path / "asked"
    _launcher(tmp_path / "bin", f"touch {marker}\n")
    got = _run(bash, tmp_path, venv="/opt/hermes/.venv", path_dirs=[tmp_path / "bin"])
    assert got == {"PM_LAYOUT": "0", "VENV": "/opt/hermes/.venv", "PM_LAUNCHER": ""}
    assert not marker.exists()


def test_launcher_gets_no_stdin(bash, tmp_path):
    # Under `curl | bash` stdin is the rest of install.sh; a launcher that read
    # it would eat the script.
    venv = _venv(tmp_path / "gen" / "venv")
    _launcher(
        tmp_path / "bin",
        "if read -r _; then exit 3; fi\n"
        + _site_output(f"{venv}/lib/python3.14/site-packages"),
    )
    got = _run(bash, tmp_path, path_dirs=[tmp_path / "bin"], stdin="rest of script\n")
    assert got["PM_LAYOUT"] == "1"
