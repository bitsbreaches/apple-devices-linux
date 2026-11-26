# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Restore a local backup to the connected device.

``idevicebackup2 restore`` is the only operation in this application that writes to a
device, so it is wrapped in the same explicit safety checks the reference implementation
applies before it will touch anything:

* the selected folder must be a **completed** backup (``Status.plist`` → ``SnapshotState``
  ``finished``), with the three metadata plists present and readable,
* its ``Target Identifier`` must match the connected device,
* the device model must match, and the backup must not come from a newer iOS version,
* an encrypted backup needs its password — ``idevicebackup2`` reports
  ``MBErrorDomain/207`` when it is missing or wrong.

Transfer, observation and cancellation mirror the backup engine: the tool runs on a pty so
its output is live, progress comes from the tool's own percentage, and cancel signals the
process and returns immediately.
"""

from __future__ import annotations

import errno
import logging
import os
import plistlib
import pty
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ..asyncutil import Job, OperationCancelled
from ..config import APP_STATE_DIR
from ..models import human_duration
from .backup_engine import (
    STAGE_DONE,
    STAGE_ERROR,
    STAGE_INIT,
    STAGE_RESTORE,
    BackupProgress,
    error_hint,
)
from .safety import UNACTIVATED_REASON, activation_lock_reason

log = logging.getLogger(__name__)

_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%")
_ERROR_CODE_RES = (
    re.compile(r"ErrorCode\s+(\d+)", re.IGNORECASE),
    re.compile(r"Error\s+Code\s*(\d+)", re.IGNORECASE),
    re.compile(r"MBErrorDomain/(\d+)"),
)
_UDID_RE = re.compile(r"\b[0-9A-Fa-f]{24,40}\b")

RESTORE_HINTS = {
    "207": ("The backup password did not unlock this encrypted backup. Enter the password "
            "chosen when the backup was made."),
    "208": ("The device is locked. Unlock the iPhone and keep it on the Home Screen when the "
            "restore starts."),
    "211": ("Turn off Find My iPhone on this device before restoring: open Settings → "
            "[your name] → Find My → Find My iPhone, switch it off and enter the Apple ID "
            "password. Keep the iPhone unlocked and connected, then choose “Restore to "
            "device…” again."),
}

ACTIVATION_LOCK_REASON = activation_lock_reason("a backup restore")


class RestoreRejected(RuntimeError):
    """The selected backup cannot be restored (checked before the device is touched)."""


@dataclass
class RestoreResult:
    status: str
    udid: str
    backup: Path
    seconds: float = 0.0
    error: str | None = None
    error_code: str | None = None
    hint: str | None = None
    last_line: str = ""
    log_path: Path | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class RestoreFailure(RuntimeError):
    def __init__(self, result: RestoreResult) -> None:
        super().__init__(result.error or "Restore failed")
        self.result = result

    @property
    def hint(self) -> str | None:
        return self.result.hint

    @property
    def error_code(self) -> str | None:
        return self.result.error_code


@dataclass(frozen=True)
class RestorePlan:
    """Everything the user is about to overwrite, resolved before the restore starts."""

    backup: Path
    parent: Path
    udid: str
    encrypted: bool
    product_type: str
    product_version: str
    device_name: str


def _plist(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise RestoreRejected(f"Cannot read {path.name}: {exc}") from exc


def build_restore_plan(backup: Path, udid: str | None = None,
                       product_type: str | None = None,
                       product_version: str | None = None,
                       activation_locked: bool | None = None,
                       activation_state: str = "") -> RestorePlan:
    """Validate a backup folder as a restore source. Raises :class:`RestoreRejected`."""
    backup = Path(backup).expanduser()
    if not backup.is_dir() or backup.is_symlink():
        raise RestoreRejected("The backup folder is unavailable or is a symbolic link.")
    for name in ("Info.plist", "Manifest.plist", "Status.plist"):
        path = backup / name
        if path.is_symlink() or not path.is_file():
            raise RestoreRejected(f"“{name}” is missing from the backup folder.")

    info = _plist(backup / "Info.plist")
    manifest = _plist(backup / "Manifest.plist")
    status = _plist(backup / "Status.plist")

    identifier = info.get("Target Identifier") or info.get("Unique Identifier")
    if not isinstance(identifier, str) or not identifier:
        raise RestoreRejected("The backup does not name the device it came from.")
    if udid and identifier.casefold() != udid.casefold():
        raise RestoreRejected(
            f"This backup belongs to {identifier}, which is not the connected device.")
    if activation_locked:
        raise RestoreRejected(ACTIVATION_LOCK_REASON)
    if activation_state.casefold() == "unactivated":
        raise RestoreRejected(UNACTIVATED_REASON)
    if status.get("SnapshotState") != "finished":
        raise RestoreRejected(
            "This backup was never completed (its snapshot state is "
            f"“{status.get('SnapshotState')}”), so it cannot be restored.")
    if not isinstance(manifest.get("IsEncrypted"), bool):
        raise RestoreRejected("The backup's encryption metadata is malformed.")

    backup_type = info.get("Product Type") or ""
    if product_type and backup_type and backup_type != product_type:
        raise RestoreRejected(
            f"The backup was made on {backup_type}, but the connected device is {product_type}.")

    def dotted(value: str) -> tuple[int, ...] | None:
        if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+)*", value):
            return None
        return tuple(int(part) for part in value.split("."))

    backup_version = dotted(info.get("Product Version") or "")
    current_version = dotted(product_version or "")
    if backup_version and current_version:
        width = max(len(backup_version), len(current_version))
        padded = backup_version + (0,) * (width - len(backup_version))
        current = current_version + (0,) * (width - len(current_version))
        if padded > current:
            raise RestoreRejected(
                f"The backup comes from iOS {' '.join(map(str, backup_version))}, which is "
                f"newer than the device's {' '.join(map(str, current_version))}.")

    return RestorePlan(
        backup=backup,
        parent=backup.parent,
        udid=identifier,
        encrypted=bool(manifest.get("IsEncrypted")),
        product_type=backup_type,
        product_version=str(info.get("Product Version") or ""),
        device_name=str(info.get("Device Name") or "the device"),
    )


class RestoreEngine:
    """Run ``idevicebackup2 restore`` with live progress and instant cancellation."""

    def __init__(
        self,
        udid: str,
        plan: RestorePlan,
        *,
        password: str | None = None,
        job: Job | None = None,
        on_progress: Callable[[BackupProgress], None] | None = None,
        popen: Callable = subprocess.Popen,
        log_dir: Path | None = None,
        use_pty: bool | None = None,
        terminate_grace: float = 3.0,
        system: bool = False,
    ) -> None:
        self.udid = udid
        self.plan = plan
        self.password = password
        self.job = job
        self.on_progress = on_progress
        self._popen = popen
        self._log_dir = log_dir or (APP_STATE_DIR / "restores")
        self.use_pty = (popen is subprocess.Popen) if use_pty is None else bool(use_pty)
        self.terminate_grace = terminate_grace
        self.system = system

        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._log_handle = None
        self.log_path: Path | None = None
        self._percent: float | None = None
        self._message = "Preparing restore…"
        self._stage = STAGE_INIT
        self._last_line = ""
        self._error_code: str | None = None
        self._started = time.monotonic()

    # ------------------------------------------------------------------ run
    def run(self) -> RestoreResult:
        if self.use_pty and shutil.which("idevicebackup2") is None:
            raise RuntimeError("idevicebackup2 is not installed (install libimobiledevice-utils)")
        self._open_log()
        self._started = time.monotonic()
        args = ["idevicebackup2", "-u", self.udid, "restore"]
        if self.system:
            args.append("--system")
        args.append("--no-reboot")
        if self.password:
            args += ["--password", self.password]
        args.append(str(self.plan.parent))
        self._log(f"launch: idevicebackup2 -u {self.udid} restore … {self.plan.parent}"
                  f" (encrypted={self.plan.encrypted})")
        self._emit(STAGE_INIT, "Starting restore…")

        exit_code: int | None = None
        try:
            self._proc = self._spawn(args)
            reader = threading.Thread(target=self._read_output, name="restore-output",
                                      daemon=True)
            reader.start()
            exit_code = self._wait()
        finally:
            for thread in threading.enumerate():
                if thread.name == "restore-output" and thread is not threading.current_thread():
                    thread.join(timeout=1.0)
            self._release_pty()

        seconds = time.monotonic() - self._started
        hint = RESTORE_HINTS.get(self._error_code or "") or error_hint(self._error_code)
        self._close_log()

        if self._cancelled.is_set():
            self._emit(STAGE_ERROR, "Restore cancelled")
            return RestoreResult("cancelled", self.udid, self.plan.backup, round(seconds, 1),
                                 error="Cancelled by the user", error_code=self._error_code,
                                 hint=hint, last_line=self._last_line, log_path=self.log_path)

        if exit_code not in (0, None) or self._stage == STAGE_ERROR:
            detail = self._last_line or f"idevicebackup2 exited with status {exit_code}"
            message = detail if hint is None else f"{detail}\n\n{hint}"
            result = RestoreResult("error", self.udid, self.plan.backup, round(seconds, 1),
                                   error=message, error_code=self._error_code, hint=hint,
                                   last_line=self._last_line, log_path=self.log_path)
            self._emit(STAGE_ERROR, message)
            raise RestoreFailure(result)

        self._emit(STAGE_DONE, "Restore finished")
        if self.job is not None:
            self.job.progress(1.0, "Restore finished")
        return RestoreResult("completed", self.udid, self.plan.backup, round(seconds, 1),
                             last_line=self._last_line, log_path=self.log_path)

    # ------------------------------------------------------------------ process
    def cancel(self) -> None:
        self._cancelled.set()
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        threading.Thread(target=self._escalate, args=(proc,), daemon=True).start()

    def _escalate(self, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + self.terminate_grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            return

    def _wait(self) -> int | None:
        assert self._proc is not None
        while True:
            if self._cancelled.is_set() or (self.job is not None and self.job.cancelled):
                self.cancel()
                return self._proc.poll()
            code = self._proc.poll()
            if code is not None:
                return code
            time.sleep(0.2)

    def _spawn(self, args: list[str]) -> subprocess.Popen:
        if not self.use_pty:
            return self._popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
        master, slave = pty.openpty()
        try:
            proc = self._popen(args, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                               start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master_fd = master
        return proc

    def _release_pty(self) -> None:
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    # ------------------------------------------------------------------ output
    def _read_output(self) -> None:
        try:
            for line in self._lines():
                self._handle(line)
        except (OSError, ValueError) as exc:
            if not self._cancelled.is_set():
                self._log(f"output reader failed: {exc}")

    def _lines(self) -> Iterator[str]:
        master = self._master_fd
        if master is not None:
            pending = b""
            while True:
                try:
                    ready, _w, _x = select.select([master], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if not ready:
                    text = pending.decode("utf-8", "replace").strip()
                    pending = b""
                    if text:
                        yield text
                    continue
                try:
                    chunk = os.read(master, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not chunk:
                    break
                pending += chunk
                while True:
                    match = re.search(rb"[\r\n]", pending)
                    if match is None:
                        break
                    raw, pending = pending[:match.start()], pending[match.end():]
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        yield text
            trailing = pending.decode("utf-8", "replace").strip()
            if trailing:
                yield trailing
            return
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            for piece in str(raw).replace("\r", "\n").split("\n"):
                text = piece.strip()
                if text:
                    yield text

    def _handle(self, line: str) -> None:
        self._last_line = line
        self._log(f"tool: {line}")
        lowered = line.casefold()
        for pattern in _ERROR_CODE_RES:
            match = pattern.search(line)
            if match:
                self._error_code = match.group(1)
                break
        match = _PERCENT_RE.search(line)
        if match:
            self._set_percent(float(match.group(1)))
        if "restor" in lowered:
            self._emit(STAGE_RESTORE, line)
        elif self._error_code or "failed" in lowered or "error" in lowered:
            hint = RESTORE_HINTS.get(self._error_code or "")
            self._emit(STAGE_ERROR, line if hint is None else f"{line}\n{hint}")
        else:
            self._emit(None, line)

    # ------------------------------------------------------------------ reporting
    def _set_percent(self, value: float) -> None:
        with self._lock:
            if self._percent is None or value >= self._percent:
                self._percent = min(100.0, value)

    def _emit(self, stage: str | None, message: str | None) -> BackupProgress:
        with self._lock:
            if stage is not None:
                self._stage = stage
            if message is not None:
                self._message = message
            progress = BackupProgress(
                stage=self._stage, message=self._message, percent=self._percent,
                elapsed_seconds=time.monotonic() - self._started,
            )
        if self.on_progress is not None:
            try:
                self.on_progress(progress)
            except Exception:  # noqa: BLE001
                log.debug("restore progress callback failed", exc_info=True)
        if self.job is not None:
            fraction = -1.0 if self._percent is None else self._percent / 100.0
            self.job.progress(fraction, f"{progress.message} · {human_duration(progress.elapsed_seconds)}")
        return progress

    # ------------------------------------------------------------------ logging
    def _open_log(self) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.udid)[-8:]
            self.log_path = self._log_dir / f"restore-{safe}-{stamp}.log"
            self._log_handle = self.log_path.open("w", encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot create restore log: %s", exc)

    def _log(self, message: str) -> None:
        if self._log_handle is None:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        safe = _UDID_RE.sub("[device]", str(message))
        if self.password:
            safe = safe.replace(self.password, "[redacted]")
        try:
            self._log_handle.write(f"[{stamp}] [{self._stage}] {safe}\n")
            self._log_handle.flush()
        except OSError:
            pass

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None


def restore_backup(
    udid: str,
    backup: Path,
    job: Job | None,
    *,
    password: str | None = None,
    product_type: str | None = None,
    product_version: str | None = None,
    on_progress: Callable[[BackupProgress], None] | None = None,
) -> RestoreResult:
    """Validate and restore one backup. Raises ``RestoreRejected``, ``RestoreFailure``
    or ``OperationCancelled``."""
    plan = build_restore_plan(backup, udid, product_type, product_version)
    engine = RestoreEngine(udid, plan, password=password, job=job, on_progress=on_progress)
    result = engine.run()
    if result.status == "cancelled":
        raise OperationCancelled("restore")
    return result
