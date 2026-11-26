# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backup engine tests.

The device is replaced by a fake ``idevicebackup2`` process that writes files into the
destination and prints realistic protocol lines, so argument construction, progress
derivation, stage transitions, cancellation and error handling are all exercised without
hardware.
"""

from __future__ import annotations

import io
import plistlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from devicedock.asyncutil import Job, OperationCancelled
from devicedock.devices.backup_engine import (
    STAGE_DONE,
    STAGE_ENUMERATE,
    STAGE_TRANSFER,
    BackupEngine,
    BackupFailure,
    read_snapshot_state,
)


class FakeProcess:
    """Minimal stand-in for ``subprocess.Popen``."""

    def __init__(self, lines: list[str], writer=None, exit_code: int = 0,
                 duration: float = 0.6) -> None:
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.returncode: int | None = None
        self._writer = writer
        self._exit_code = exit_code
        self._duration = duration
        self._deadline = time.monotonic() + duration
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.terminated or self.killed:
            self.returncode = -15 if not self.killed else -9
            return self.returncode
        if self._writer is not None:
            self._writer()
        if time.monotonic() >= self._deadline:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self.returncode if self.returncode is not None else -15
        return self.returncode


def write_snapshot(root: Path, *, encrypted: bool = False, file_rows: int = 1) -> None:
    """Write the metadata every real snapshot has, without touching existing files."""
    root.mkdir(parents=True, exist_ok=True)
    info = {"Device Name": "Test iPhone", "Product Type": "iPhone14,8",
            "Product Version": "17.0", "Serial Number": "F2LTEST"}
    status = {"BackupState": "new", "IsFullBackup": False, "SnapshotState": "finished",
              "Version": "3.3", "UUID": "00000000-0000-0000-0000-000000000000"}
    manifest = {"IsEncrypted": encrypted, "Version": "10.0",
                "Date": datetime(2026, 11, 19), "SystemDomainsVersion": "24.0"}
    if encrypted:
        manifest["ManifestKey"] = b"\x04\x00\x00\x00" + b"k" * 40
        manifest["BackupKeyBag"] = b"bplist00-fake-keybag"
    for name, value in (("Info.plist", info), ("Status.plist", status),
                        ("Manifest.plist", manifest)):
        target = root / name
        if not target.exists():
            target.write_bytes(plistlib.dumps(value))

    db = root / "Manifest.db"
    if not db.exists():
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
            "relativePath TEXT, flags INTEGER, file BLOB)")
        for index in range(file_rows):
            connection.execute(
                "INSERT INTO Files VALUES (?, 'HomeDomain', ?, 2, ?)",
                (f"{index:040x}", f"Library/Directory{index}", plistlib.dumps({})))
        connection.commit()
        connection.close()


def make_popen(lines: list[str], *, payload_steps: int = 4, exit_code: int = 0,
               hang: bool = False, metadata: bool = True):
    calls: list[dict] = []

    def factory(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs})
        destination = Path(args[-1])
        if metadata:
            write_snapshot(destination)

        def writer() -> None:
            existing = len(list(destination.glob("payload-*")))
            if existing < payload_steps:
                (destination / f"payload-{existing}").write_bytes(b"x" * 4096)

        if hang:
            process = FakeProcess(lines, writer=writer, exit_code=exit_code,
                                  duration=10_000.0)

            def never_finishes() -> None:
                raise AssertionError("hang fixture should not complete")

            process._duration = 10_000.0
            return process
        return FakeProcess(lines, writer=writer, exit_code=exit_code)

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


def test_incremental_is_default_and_omits_full(tmp_path: Path) -> None:
    factory = make_popen(["Backup will be encrypted.", "Requesting backup from device…"])
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory)
    result = engine.run()

    assert result.completed
    args = factory.calls[0]["args"]
    assert "--full" not in args
    assert args[0] == "idevicebackup2"
    assert args[-1] == str(tmp_path / "dest")
    assert factory.calls[0]["kwargs"]["stdin"] is not None  # /dev/null, never a TTY


def test_full_mode_passes_full_flag(tmp_path: Path) -> None:
    factory = make_popen(["Full backup mode."])
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", full=True,
                          popen=factory)
    engine.run()
    assert "--full" in factory.calls[0]["args"]


def test_progress_reports_files_and_bytes(tmp_path: Path) -> None:
    factory = make_popen(["Requesting backup from device…", "Receiving files…"])
    seen: list[tuple[int, int]] = []
    engine = BackupEngine(
        "0000811000000000000000AA", tmp_path / "dest", popen=factory,
        poll_interval=0.05,
        on_progress=lambda p: seen.append((p.file_count, p.bytes_on_disk)),
    )
    result = engine.run()

    assert result.file_count >= 1
    assert result.bytes_on_disk >= 4096
    assert seen, "progress callback should have been invoked"
    assert max(count for count, _ in seen) >= 1


def test_progress_fraction_is_pulse_when_percent_unknown(tmp_path: Path) -> None:
    factory = make_popen(["Requesting backup from device…"])
    job = Job("test")
    fractions: list[float] = []
    job.set_progress_handler(lambda fraction, _text: fractions.append(fraction) or False)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", job=job,
                          popen=factory, poll_interval=0.05)
    engine.run()
    # Job.progress is dispatched through GLib; the negative value marks "unknown amount".
    assert engine.progress.stage in (STAGE_DONE, STAGE_TRANSFER)


def test_stage_transitions_from_output(tmp_path: Path) -> None:
    factory = make_popen([
        "Started \"com.apple.mobilebackup2\" service on port 51190.",
        "Requesting backup from device...",
        "Received 0 files from device.",
        "Verifying backup...",
        "Backup successful.",
    ])
    stages: list[str] = []
    engine = BackupEngine(
        "0000811000000000000000AA", tmp_path / "dest", popen=factory, poll_interval=0.05,
        on_progress=lambda p: stages.append(p.stage),
    )
    result = engine.run()
    assert STAGE_ENUMERATE in stages
    assert result.completed
    assert result.snapshot_state in (None, "finished", "unreadable")


def test_error_exit_raises_with_tool_message(tmp_path: Path) -> None:
    factory = make_popen(["Requesting backup from device...", "Backup Aborted."],
                         exit_code=1)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)
    with pytest.raises(RuntimeError) as excinfo:
        engine.run()
    assert "Aborted" in str(excinfo.value) or "status 1" in str(excinfo.value)


def test_cancel_terminates_and_keeps_destination(tmp_path: Path) -> None:
    process = FakeProcess(["Requesting backup from device..."], writer=lambda: None,
                          duration=10_000.0)
    created: list[FakeProcess] = []

    def factory(args, **kwargs):
        created.append(process)
        destination = Path(args[-1])
        (destination / "partial").write_bytes(b"partial payload")
        # A real tool writes Status.plist as soon as the snapshot starts.
        (destination / "Status.plist").write_bytes(plistlib.dumps({
            "Version": "3.3", "SnapshotState": "unfinished", "IsFullBackup": False,
        }))
        return process

    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = engine.run()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.3)
    engine.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert process.terminated or process.killed
    result = outcome.get("result")
    assert result is not None and result.status == "cancelled"
    assert result.resumable is True
    assert (tmp_path / "dest" / "partial").exists()


def test_job_cancellation_stops_the_run(tmp_path: Path) -> None:
    process = FakeProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        return process

    job = Job("cancel-me")
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", job=job,
                          popen=factory, poll_interval=0.05)
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["result"] = engine.run()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.3)
    job.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert outcome["result"].status == "cancelled"


def test_stall_timeout_stops_a_silent_transfer(tmp_path: Path) -> None:
    process = FakeProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        (Path(args[-1]) / "one").write_bytes(b"x")
        return process

    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05, stall_timeout=0.4)
    started = time.monotonic()
    result = engine.run()
    assert time.monotonic() - started < 10
    assert result.status == "cancelled"
    assert "No progress" in (result.error or "")


def test_snapshot_state_is_read_before_launch(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "Status.plist").write_bytes(plistlib.dumps({
        "Version": "3.3", "SnapshotState": "unfinished", "IsFullBackup": False,
    }))
    assert read_snapshot_state(destination) == "unfinished"

    factory = make_popen(["Requesting backup from device..."])
    engine = BackupEngine("0000811000000000000000AA", destination, popen=factory,
                          poll_interval=0.05)
    result = engine.run()
    assert "--full" not in factory.calls[0]["args"]
    assert result.resumable is True


def test_snapshot_state_unreadable_is_reported(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    destination.mkdir()
    (destination / "Status.plist").write_bytes(b"not a plist")
    assert read_snapshot_state(destination) == "unreadable"


def test_backup_log_redacts_device_identifier(tmp_path: Path) -> None:
    udid = "0000811000000000000000AA"
    factory = make_popen([f"Device {udid} connected", "Backup successful."])
    engine = BackupEngine(udid, tmp_path / "dest", popen=factory, poll_interval=0.05,
                          log_dir=tmp_path / "logs")
    result = engine.run()
    assert result.log_path is not None and result.log_path.exists()
    content = result.log_path.read_text()
    assert udid not in content
    assert "[device]" in content


def test_sample_window_is_time_bounded(tmp_path: Path) -> None:
    """Samples are taken per poll and pruned to a 60-second window, not per write."""
    factory = make_popen(["Requesting backup from device..."], payload_steps=1)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.01)
    started = time.monotonic()
    engine.run()
    elapsed = time.monotonic() - started
    samples = engine._samples
    assert samples, "poller should have sampled"
    assert len(samples) <= elapsed / 0.01 + 5
    newest = samples[-1][0]
    assert all(timestamp >= newest - 61.0 for timestamp, _ in samples)


class StubbornProcess(FakeProcess):
    """A tool that ignores SIGTERM, the way idevicebackup2 can when blocked on the device."""

    def terminate(self) -> None:  # pragma: no cover - asserted through behaviour
        self.terminate_calls = getattr(self, "terminate_calls", 0) + 1

    def kill(self) -> None:
        self.kill_calls = getattr(self, "kill_calls", 0) + 1
        self.killed = True


def test_error_208_is_explained_as_a_locked_device(tmp_path: Path) -> None:
    """The recorded failure from a locked iPhone must produce an actionable message."""
    from devicedock.devices.backup_engine import BackupFailure

    factory = make_popen([
        "Backup will be encrypted.",
        "Requesting backup from device...",
        "Incremental backup mode.",
        "ErrorCode 208: Device locked (MBErrorDomain/208)",
        "Received 0 files from device.",
        "Backup Failed (Error Code 208).",
    ], payload_steps=0, exit_code=2)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)
    with pytest.raises(BackupFailure) as excinfo:
        engine.run()
    failure = excinfo.value
    assert failure.error_code == "208"
    assert failure.hint and "locked" in failure.hint.lower()
    assert "locked" in str(failure).lower()


def test_cancel_returns_immediately_when_the_tool_ignores_sigterm(tmp_path: Path) -> None:
    """Cancelling must not wait for the transfer: the escalation runs on its own thread."""
    process = StubbornProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        return process

    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05, terminate_grace=0.3)
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["result"] = engine.run()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.3)

    started = time.monotonic()
    engine.cancel()
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, f"cancel() blocked for {elapsed:.2f}s"
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome["result"].status == "cancelled"
    # The helper thread still escalates for a tool that ignores SIGTERM.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not getattr(process, "kill_calls", 0):
        time.sleep(0.05)
    assert getattr(process, "kill_calls", 0) == 1


def test_create_backup_raises_operation_cancelled(tmp_path: Path,
                                                   monkeypatch) -> None:
    """The wrapper must not report a cancelled run as a completed backup."""
    import functools

    import devicedock.devices.backup_engine as module

    process = FakeProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        return process

    monkeypatch.setattr(module, "BackupEngine", functools.partial(
        module.BackupEngine, popen=factory, poll_interval=0.05))
    job = Job("cancel-me")

    def canceller() -> None:
        time.sleep(0.3)
        job.cancel()

    threading.Thread(target=canceller, daemon=True).start()
    with pytest.raises(OperationCancelled):
        module.create_backup("0000811000000000000000AA", tmp_path / "dest", job)


def test_cancelled_run_returns_without_waiting_for_a_hung_reader(tmp_path: Path) -> None:
    """A cancelled run must return promptly even if the tool never releases its output."""
    process = StubbornProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        return process

    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05, terminate_grace=5.0)
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["result"] = engine.run()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(0.3)
    engine.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive(), "run() should not wait for the stuck process to exit"
    assert outcome["result"].status == "cancelled"


def test_run_transcript_is_bounded(tmp_path: Path) -> None:
    """A full backup prints hundreds of thousands of lines; the transcript must not grow."""
    lines = [f"Receiving files {index} of 500000" for index in range(500)]
    factory = make_popen(lines, payload_steps=1)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)
    result = engine.run()
    assert len(result.messages) <= engine.max_messages
    assert engine.max_messages <= 1000


def test_a_waiting_device_is_not_killed_or_called_a_stall(tmp_path: Path) -> None:
    """iOS asks for the passcode after the transfer starts; that wait must never abort."""
    process = FakeProcess(["Requesting backup from device..."], duration=10_000.0)

    def factory(args, **kwargs):
        (Path(args[-1]) / "one").write_bytes(b"x")
        return process

    messages: list[str] = []
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05, wait_notice_after=0.3,
                          on_progress=lambda p: messages.append(p.message))
    outcome: dict[str, object] = {}

    def run() -> None:
        outcome["result"] = engine.run()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(1.5)
    assert worker.is_alive(), "the engine must keep waiting instead of aborting"
    assert any("passcode" in message.lower() for message in messages), messages
    engine.cancel()
    worker.join(timeout=5)
    assert outcome["result"].status == "cancelled"


# ------------------------------------------------------------------ command construction
def test_command_has_no_filter_argument(tmp_path: Path) -> None:
    """`idevicebackup2` 1.3.0 has no --filter, so the engine must never emit one."""
    factory = make_popen(["Incremental backup mode."])
    BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory).run()

    args = factory.calls[0]["args"]
    assert "--filter" not in args
    assert args == ["idevicebackup2", "-u", "0000811000000000000000AA", "backup",
                    str(tmp_path / "dest")]


def test_full_command_is_udid_scoped_and_direct_to_disk(tmp_path: Path) -> None:
    factory = make_popen(["Full backup mode."])
    destination = tmp_path / "dest"
    BackupEngine("0000811000000000000000AA", destination, full=True, popen=factory).run()

    args = factory.calls[0]["args"]
    assert args[1:3] == ["-u", "0000811000000000000000AA"]
    assert args[3] == "backup"
    assert args[-1] == str(destination), "the tool writes straight into the destination"


# ------------------------------------------------------------------ no-payload safeguard
def test_a_run_that_writes_no_payload_is_a_failure(tmp_path: Path) -> None:
    """The tool can exit 0 without transferring anything; that must not read as success."""
    factory = make_popen(["Requesting backup from device…"], payload_steps=0,
                         metadata=False)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)

    with pytest.raises(BackupFailure) as excinfo:
        engine.run()

    assert "without writing any backup payload" in str(excinfo.value)
    assert excinfo.value.result.status == "error"


def test_a_legitimately_empty_backup_is_not_a_failure(tmp_path: Path) -> None:
    """Every real snapshot writes its metadata, so a device with no user data still passes."""
    destination = tmp_path / "dest"

    def factory(args, **kwargs):
        write_snapshot(Path(args[-1]) / "0000811000000000000000AA", file_rows=2)
        return FakeProcess(["Backup Successful."], duration=0.2)

    engine = BackupEngine("0000811000000000000000AA", destination, popen=factory,
                          poll_interval=0.05)
    result = engine.run()

    assert result.completed
    assert result.file_count == 4
    assert result.bytes_on_disk > 0


def test_cancelled_run_is_never_reported_as_a_missing_payload(tmp_path: Path) -> None:
    factory = make_popen(["Requesting backup from device…"], payload_steps=0, hang=True)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)
    engine.cancel("Cancelled by user")
    result = engine.run()

    assert result.status == "cancelled"


# ------------------------------------------------------------------ failure hygiene
def test_failed_run_cleans_up_and_leaves_the_destination(tmp_path: Path) -> None:
    factory = make_popen(["Could not connect to device."], payload_steps=0, exit_code=1)
    destination = tmp_path / "dest"
    engine = BackupEngine("0000811000000000000000AA", destination, popen=factory,
                          poll_interval=0.05)

    with pytest.raises(BackupFailure):
        engine.run()

    assert destination.is_dir(), "the destination must survive a failed run"
    assert engine.log_path is not None and engine.log_path.is_file()
    for thread in (engine._reader, engine._poller):
        assert thread is None or not thread.is_alive(), "no thread may outlive the run"


def test_device_disconnection_is_an_error_with_the_tool_message(tmp_path: Path) -> None:
    """A device unplugged mid-run makes the tool exit non-zero; that is a failure, not a stall."""
    factory = make_popen(["No device found, exiting."], payload_steps=2, exit_code=255)
    engine = BackupEngine("0000811000000000000000AA", tmp_path / "dest", popen=factory,
                          poll_interval=0.05)

    with pytest.raises(BackupFailure) as excinfo:
        engine.run()

    result = excinfo.value.result
    assert result.status == "error"
    assert result.error_code is None
    assert "No device found" in str(excinfo.value)
