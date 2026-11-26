# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""The postinst hook must bring usbmuxd up without disturbing administrator choices."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

POSTINST = Path(__file__).resolve().parent.parent / "packaging" / "deb" / "postinst"

pytestmark = pytest.mark.skipif(not Path("/run/systemd/system").is_dir(),
                                reason="exercises the systemd branch of the hook")


def _run(tmp_path: Path, *, active: bool, enabled_state: str, enable_succeeds: bool,
         start_succeeds: bool = True) -> list[str]:
    """Run the hook with a stub systemctl and return the commands it received."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f'  is-enabled) echo {enabled_state}; exit 0 ;;\n'
        f'  is-active) exit {0 if active else 3} ;;\n'
        f'  enable) exit {0 if enable_succeeds else 1} ;;\n'
        f'  start) exit {0 if start_succeeds else 1} ;;\n'
        'esac\n'
        'exit 0\n',
        encoding="utf-8")
    stub.chmod(0o755)
    environment = {"PATH": str(bindir), "LC_ALL": "C"}
    result = subprocess.run(["/bin/sh", str(POSTINST), "configure"], capture_output=True,
                            text=True, env=environment, timeout=30, check=False,
                            cwd=os.fspath(tmp_path))
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8").splitlines()


def test_a_running_daemon_is_never_restarted(tmp_path: Path) -> None:
    calls = _run(tmp_path, active=True, enabled_state="enabled", enable_succeeds=True)
    assert not [call for call in calls if call.startswith(("enable", "start", "restart"))]


def test_an_enabled_but_stopped_unit_is_enabled_and_started(tmp_path: Path) -> None:
    calls = _run(tmp_path, active=False, enabled_state="enabled", enable_succeeds=True)
    assert "is-active --quiet usbmuxd.service" in calls
    assert "enable --now usbmuxd.service" in calls
    assert not [call for call in calls if call.startswith("restart")]


def test_a_static_unit_is_started_without_changing_enablement(tmp_path: Path) -> None:
    calls = _run(tmp_path, active=False, enabled_state="static", enable_succeeds=False)
    assert "enable --now usbmuxd.service" in calls
    assert "start usbmuxd.service" in calls


def test_a_masked_unit_is_left_alone(tmp_path: Path) -> None:
    calls = _run(tmp_path, active=False, enabled_state="masked", enable_succeeds=True)
    assert not [call for call in calls
                if call.startswith(("enable", "start", "stop", "restart"))]
