"""Guards for install.sh registering the gateway with the host's service manager.

A gateway the installer merely starts dies on reboot. On a host with launchd or
a systemd user manager the installer registers it as a login service instead.
These tests run that block for real against a fake ``hermes`` that logs its
calls, under /bin/bash too when it exists (bash 3.2 on macOS).
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
        "# BEGIN native-service (extracted and run by tests/test_install_sh_service.py)"
    )
    end = lines.index("# END native-service")
    return "\n".join(lines[start : end + 1])


def _bashes() -> list[str]:
    found = [b for b in (shutil.which("bash"), "/bin/bash") if b and Path(b).exists()]
    return list(dict.fromkeys(found))


def _fake_hermes(bindir: Path, log: Path, *, fail: str = "") -> None:
    """A ``hermes`` that logs each call; ``fail`` names a subcommand that exits 1."""
    bindir.mkdir(parents=True, exist_ok=True)
    hermes = bindir / "hermes"
    hermes.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'[ "$2" = "{fail}" ] && exit 1\n'
        '[ "$2" = status ] && echo "✓ Gateway service is running"\n'
        "exit 0\n"
    )
    hermes.chmod(0o755)


def _run(
    bash: str, tmp_path: Path, *, native: str, supervised: str = "0", fail: str = ""
):
    log = tmp_path / "calls.log"
    log.touch()
    _fake_hermes(tmp_path / "bin", log, fail=fail)
    script = "\n".join(
        [
            "set -euo pipefail",
            'info() { printf "info: %s\\n" "$*"; }',
            'warn() { printf "warn: %s\\n" "$*"; }',
            f'NATIVE_SERVICE="{native}"',
            f"SUPERVISED={supervised}",
            _block(),
            'printf "SUPERVISED=%s\\n" "$SUPERVISED"',
        ]
    )
    out = subprocess.run(
        [bash, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"},
    )
    return out.stdout + out.stderr, log.read_text().splitlines()


@pytest.mark.parametrize("bash", _bashes())
def test_registers_a_login_service_and_starts_it(bash, tmp_path):
    output, calls = _run(bash, tmp_path, native="launchd")
    assert calls[:4] == [
        "gateway stop",
        "gateway install --start-on-login --start-now",
        "gateway start",
        "gateway status",
    ]
    assert "SUPERVISED=1" in output
    assert "start automatically at login" in output
    assert "log in after a reboot" in output


@pytest.mark.parametrize("bash", _bashes())
def test_a_failed_install_falls_back_to_a_session_start(bash, tmp_path):
    output, calls = _run(bash, tmp_path, native="systemd", fail="install")
    assert "gateway start" not in calls
    assert "SUPERVISED=0" in output
    assert "will not survive a reboot" in output


@pytest.mark.parametrize("bash", _bashes())
def test_does_nothing_without_a_service_manager(bash, tmp_path):
    output, calls = _run(bash, tmp_path, native="")
    assert calls == []
    assert "SUPERVISED=0" in output


@pytest.mark.parametrize("bash", _bashes())
def test_leaves_an_s6_supervised_gateway_alone(bash, tmp_path):
    _, calls = _run(bash, tmp_path, native="systemd", supervised="1")
    assert calls == []
