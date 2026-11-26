# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Restore validation and engine behaviour (no device is touched)."""

from __future__ import annotations

import io
import plistlib
import threading
import time
from pathlib import Path

import pytest

from devicedock.asyncutil import Job, OperationCancelled
from devicedock.devices import restore_engine
from devicedock.devices.restore_engine import (
    RestoreEngine,
    RestoreFailure,
    RestoreRejected,
    build_restore_plan,
    restore_backup,
)

UDID = "0000811000000000000000AA"


def make_backup(root: Path, *, encrypted: bool = False, state: str = "finished",
                target: str = UDID, product_type: str = "iPhone14,8",
                product_version: str = "27.2.1") -> Path:
    backup = root / target
    backup.mkdir(parents=True)
    (backup / "Info.plist").write_bytes(plistlib.dumps({
        "Target Identifier": target,
        "Product Type": product_type,
        "Product Version": product_version,
        "Device Name": "Test iPhone",
    }))
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted}))
    (backup / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": state}))
    return backup


class FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0, duration: float = 0.5) -> None:
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._deadline = time.monotonic() + duration
        self.terminated = False

    def poll(self) -> int | None:
        if self.terminated:
            self.returncode = -15
        elif time.monotonic() >= self._deadline:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def test_plan_accepts_a_finished_backup(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    plan = build_restore_plan(backup, UDID, "iPhone14,8", "27.2.1")
    assert plan.encrypted is False
    assert plan.parent == tmp_path
    assert plan.device_name == "Test iPhone"


def test_plan_rejects_an_unfinished_backup(tmp_path: Path) -> None:
    backup = make_backup(tmp_path, state="unfinished")
    with pytest.raises(RestoreRejected, match="never completed"):
        build_restore_plan(backup, UDID)


def test_plan_rejects_a_backup_for_another_device(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    with pytest.raises(RestoreRejected, match="belongs to"):
        build_restore_plan(backup, "0000811000000000000000BB")


def test_plan_rejects_a_different_model(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    with pytest.raises(RestoreRejected, match="made on"):
        build_restore_plan(backup, UDID, "iPhone16,1", "27.2.1")


def test_plan_rejects_a_newer_ios_backup(tmp_path: Path) -> None:
    backup = make_backup(tmp_path, product_version="27.2.1")
    with pytest.raises(RestoreRejected, match="newer than"):
        build_restore_plan(backup, UDID, "iPhone14,8", "26.4")


def test_plan_rejects_missing_metadata(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    (backup / "Manifest.plist").unlink()
    with pytest.raises(RestoreRejected, match="Manifest.plist"):
        build_restore_plan(backup, UDID)


def test_engine_passes_the_backup_password_and_parent_directory(tmp_path: Path) -> None:
    backup = make_backup(tmp_path, encrypted=True)
    plan = build_restore_plan(backup, UDID)
    calls: list[dict] = []

    def factory(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs})
        return FakeProcess(["Restoring files…", "Restore Successful."])

    engine = RestoreEngine(UDID, plan, password="s3cret", popen=factory,
                           log_dir=tmp_path / "logs")
    result = engine.run()

    assert result.completed
    args = calls[0]["args"]
    assert args[:5] == ["idevicebackup2", "-u", UDID, "restore", "--no-reboot"]
    assert "--password" in args and "s3cret" in args
    assert args[-1] == str(tmp_path)
    log_text = (result.log_path).read_text()
    assert "s3cret" not in log_text and UDID not in log_text


def test_plan_rejects_activation_locked_device(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    with pytest.raises(RestoreRejected) as excinfo:
        build_restore_plan(backup, UDID, "iPhone14,8", "27.2.1", activation_locked=True)
    message = str(excinfo.value)
    assert "Find My" in message
    assert "Settings" in message and "Find My iPhone" in message
    assert "211" not in message  # the user is given steps, not an error code


def test_plan_ignores_an_unknown_activation_state(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    plan = build_restore_plan(backup, UDID, "iPhone14,8", "27.2.1", activation_locked=None)
    assert plan.udid == UDID


def test_engine_reports_error_211_with_a_hint(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    plan = build_restore_plan(backup, UDID)

    def factory(args, **kwargs):
        return FakeProcess([
            "ErrorCode 211: Find My iPhone must be disabled before restoring "
            "(MBErrorDomain/211)",
            "Restore Failed (Error Code 211).",
        ], exit_code=2)

    engine = RestoreEngine(UDID, plan, popen=factory, log_dir=tmp_path / "logs")
    with pytest.raises(RestoreFailure) as excinfo:
        engine.run()
    assert excinfo.value.error_code == "211"
    hint = excinfo.value.hint or ""
    assert "Find My iPhone" in hint and "Settings" in hint
    assert "211" not in hint  # the user sees instructions, not the device's error code


def test_engine_reports_error_207_with_a_hint(tmp_path: Path) -> None:
    backup = make_backup(tmp_path, encrypted=True)
    plan = build_restore_plan(backup, UDID)

    def factory(args, **kwargs):
        return FakeProcess([
            "ErrorCode 207: Invalid password when restoring encrypted backup "
            "(MBErrorDomain/207).",
        ], exit_code=2)

    engine = RestoreEngine(UDID, plan, popen=factory, log_dir=tmp_path / "logs")
    with pytest.raises(RestoreFailure) as excinfo:
        engine.run()
    assert excinfo.value.error_code == "207"
    assert "password" in (excinfo.value.hint or "").lower()


def test_cancel_returns_immediately(tmp_path: Path) -> None:
    backup = make_backup(tmp_path)
    plan = build_restore_plan(backup, UDID)
    process = FakeProcess(["Restoring…"], duration=10_000)

    def factory(args, **kwargs):
        return process

    engine = RestoreEngine(UDID, plan, popen=factory, log_dir=tmp_path / "logs")
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["result"] = engine.run()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    engine.cancel()
    assert time.monotonic() - started < 0.2
    worker.join(timeout=5)
    assert outcome["result"].status == "cancelled"


def test_restore_backup_raises_operation_cancelled(tmp_path: Path, monkeypatch) -> None:
    backup = make_backup(tmp_path)
    process = FakeProcess(["Restoring…"], duration=10_000)

    def factory(args, **kwargs):
        return process

    monkeypatch.setattr(restore_engine, "RestoreEngine",
                        lambda *a, **k: RestoreEngine(*a, popen=factory,
                                                      log_dir=tmp_path / "logs", **k))
    job = Job("restore")
    threading.Thread(target=lambda: (time.sleep(0.2), job.cancel()), daemon=True).start()
    with pytest.raises(OperationCancelled):
        restore_backup(UDID, backup, job)
