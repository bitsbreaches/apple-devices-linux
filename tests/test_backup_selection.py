# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backup engine selection and contract tests.

Phase 1 replaced the pymobiledevice3 backup path with the ``idevicebackup2`` engine. These
tests pin that down without a device: the single entry point used by the UI must reach the
CLI engine, keep the ``BackupResult`` contract, and never import the copyleft package.
"""

from __future__ import annotations

import importlib.util
import plistlib
from pathlib import Path

import pytest

from devicedock.asyncutil import Job
from devicedock.devices import backup_engine, libimobile
from devicedock.devices.backup_engine import BackupResult

ROOT = Path(__file__).resolve().parents[1]
UDID = "0000811000000000000000AA"


def test_the_backup_entry_point_calls_the_cli_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    expected = BackupResult(status="completed", destination=tmp_path, udid=UDID, full=True,
                            bytes_on_disk=1, file_count=1, duration_seconds=0.1,
                            snapshot_state="finished", resumable=False, last_line="")

    def fake_cli(udid, destination, job, **kwargs):
        calls.append({"udid": udid, "destination": destination, "job": job, **kwargs})
        return expected

    monkeypatch.setattr(backup_engine, "create_backup", fake_cli)
    job = Job("backup")
    seen: list[object] = []

    result = libimobile.create_backup(UDID, tmp_path, job, full=True,
                                      on_progress=seen.append)

    assert result is expected
    assert len(calls) == 1
    assert calls[0]["udid"] == UDID
    assert calls[0]["destination"] == tmp_path
    assert calls[0]["full"] is True
    assert calls[0]["on_progress"].__self__ is seen


def test_the_ui_entry_point_returns_the_shared_backup_result(monkeypatch,
                                                             tmp_path: Path) -> None:
    expected = BackupResult(status="completed", destination=tmp_path, udid=UDID, full=False,
                            bytes_on_disk=1, file_count=1, duration_seconds=0.1,
                            snapshot_state="finished", resumable=False, last_line="")
    monkeypatch.setattr(backup_engine, "create_backup", lambda *a, **k: expected)

    assert isinstance(libimobile.create_backup(UDID, tmp_path, Job("backup")), BackupResult)


def test_a_password_argument_changes_nothing_about_the_run(monkeypatch,
                                                           tmp_path: Path) -> None:
    """Encryption is a device setting; the backup call must not forward a password."""
    calls: list[dict] = []
    monkeypatch.setattr(backup_engine, "create_backup",
                        lambda *a, **k: calls.append(k) or object())

    libimobile.create_backup(UDID, tmp_path, Job("backup"), password="secret-passphrase")

    assert calls and "password" not in calls[0]


def test_the_pymobiledevice3_backup_module_is_gone() -> None:
    assert importlib.util.find_spec("devicedock.devices.backup_pmd3") is None


@pytest.mark.parametrize("path", sorted((ROOT / "devicedock").rglob("*.py")),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_application_modules_do_not_reference_pymobiledevice3(path: Path) -> None:
    """All device access uses libimobiledevice and its command-line tools."""
    if "pymobiledevice3" not in path.read_text(encoding="utf-8"):
        return
    raise AssertionError(f"{path.relative_to(ROOT)} still imports pymobiledevice3")


def test_the_cli_engine_writes_into_a_per_udid_directory(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    device_dir = destination / UDID
    device_dir.mkdir(parents=True)
    with (device_dir / "Status.plist").open("wb") as stream:
        plistlib.dump({"SnapshotState": "finished"}, stream)

    assert backup_engine.device_subdirectory(destination, UDID) == device_dir
    assert backup_engine.read_snapshot_state(destination, UDID) == "finished"
