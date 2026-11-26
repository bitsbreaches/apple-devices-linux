# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Streaming, observable device backup engine.

This is the backup runner from the reference implementation (``itunes-linux``,
``core/backup_engine.py``) ported into this application. The structure, the stage names,
the observation model and the command line are the reference's:

* the transfer is performed by ``idevicebackup2`` and the protocol is strictly sequential,
  so the only concurrency is *observation*,
* a reader thread classifies the tool's protocol messages into stages,
* a poller thread samples the destination on disk to derive throughput, ETA and stall
  detection from bytes that actually landed (the tool's own output carries no byte count),
* ``Status.plist`` is inspected **before** launch only — the tool rewrites it while running,
* incremental by default: ``--full`` is omitted so an existing snapshot is reused and an
  interrupted transfer resumes,
* cancel terminates gracefully (TERM, then KILL) and keeps partial data resumable.

Deliberate improvements over the reference, all made for backup *time* and visibility:

1. **The child gets a pseudo-terminal for its output.** With a plain pipe, libc fully
   buffers the tool's stdout: in the recorded logs every protocol line of a 125-second run
   arrived in one burst at exit. On a pty the tool line-buffers, so "Requesting backup from
   device…" appears in ~0.5 s (measured) instead of after the transfer finishes. ``stdin``
   stays ``/dev/null``, so the tool never enters interactive mode.
2. **Adaptive, cheap sampling.** A recursive walk of a 70 000-file backup costs seconds, so
   walking every 2 s steals I/O from the very transfer it measures and makes the display
   stale. The poller measures its own walk cost and re-walks only often enough to stay under
   a quarter of the wall clock, ticking the UI once a second from the last measurement.
3. **Cancel is non-blocking.** TERM is sent immediately and the escalation to KILL runs on a
   helper thread, so no caller waits on a tool blocked by a locked device. A cancelled run
   reports the totals of its last sample instead of walking the tree again.
4. **Actionable errors.** ``MBErrorDomain/208`` means the device is locked; that is reported
   as an instruction, not as "Backup Failed (Error Code 208)".
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ..asyncutil import Job, OperationCancelled
from ..config import APP_STATE_DIR
from ..models import human_duration, human_size

log = logging.getLogger(__name__)

STAGE_INIT = "init"
STAGE_ENUMERATE = "enumerate"
STAGE_TRANSFER = "transfer"
STAGE_VERIFY = "verify"
STAGE_DONE = "done"
STAGE_RESTORE = "restore"
STAGE_STALLED = "stalled"
STAGE_ERROR = "error"

_RECEIVED_RE = re.compile(r"received\s+(\d+)\s+files?", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%")
# "ErrorCode 208: Device locked (MBErrorDomain/208)" and "Backup Failed (Error Code 208)."
_ERROR_CODE_RES = (
    re.compile(r"ErrorCode\s+(\d+)", re.IGNORECASE),
    re.compile(r"Error\s+Code\s*(\d+)", re.IGNORECASE),
    re.compile(r"MBErrorDomain/(\d+)"),
)
_UDID_RE = re.compile(r"\b[0-9A-Fa-f]{24,40}\b")

# A snapshot always contains Info.plist, Manifest.plist, Manifest.db and Status.plist. A run
# that ends with zero bytes across no more than these files wrote no snapshot at all.
_PLIST_ONLY_FILE_BUDGET = 4

# Documented MobileBackup2 errors that a user can act on.
_ERROR_HINTS = {
    "208": ("The device is locked. Unlock the iPhone, keep it on the Home Screen, and start "
            "the backup again: iOS refuses to back up while the screen is locked."),
    "203": ("The device rejected the request because it is passcode-protected. Unlock it "
            "first."),
    "5": "The device denied the request. Unlock it and confirm “Trust This Computer”.",
}


def error_hint(code: str | None) -> str | None:
    """Return the actionable explanation for a MobileBackup2 error code."""
    if not code:
        return None
    return _ERROR_HINTS.get(str(code))


@dataclass
class BackupProgress:
    """One immutable progress observation, safe to pass across threads."""

    stage: str
    message: str
    bytes_on_disk: int = 0
    file_count: int = 0
    percent: float | None = None
    rate_bytes_per_sec: float = 0.0
    eta_seconds: float | None = None
    stall_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def text(self) -> str:
        return self.message

    @property
    def active(self) -> bool:
        return self.stage in (STAGE_INIT, STAGE_ENUMERATE, STAGE_TRANSFER, STAGE_VERIFY)


@dataclass
class BackupResult:
    """Outcome of one backup run."""

    status: str
    destination: Path
    udid: str
    full: bool
    bytes_on_disk: int = 0
    file_count: int = 0
    duration_seconds: float = 0.0
    snapshot_state: str | None = None
    resumable: bool = False
    last_line: str = ""
    log_path: Path | None = None
    error: str | None = None
    error_code: str | None = None
    hint: str | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def device_subdirectory(destination: Path, udid: str) -> Path:
    """Directory the tool actually writes a device's snapshot into (``<dest>/<UDID>``)."""
    return Path(destination) / udid


def read_snapshot_state(destination: Path, udid: str | None = None) -> str | None:
    """Return ``Status.plist`` → ``SnapshotState`` of an existing destination.

    ``idevicebackup2`` writes into ``<destination>/<UDID>/``, so that location is checked
    first; a bare ``<destination>/Status.plist`` is accepted for backups created by other
    tools. Only call this before launching the backup: the file is rewritten while running.
    """
    candidates = []
    if udid:
        candidates.append(device_subdirectory(destination, udid) / "Status.plist")
    candidates.append(Path(destination) / "Status.plist")
    status = next((path for path in candidates if path.is_file()), None)
    if status is None:
        return None
    try:
        with status.open("rb") as stream:
            value = plistlib.load(stream)
        return value.get("SnapshotState") if isinstance(value, dict) else None
    except (OSError, plistlib.InvalidFileException, ValueError):
        return "unreadable"


class BackupFailure(RuntimeError):
    """A failed backup, carrying the structured result so the UI can explain it."""

    def __init__(self, result: BackupResult) -> None:
        super().__init__(result.error or "Backup failed")
        self.result = result

    @property
    def hint(self) -> str | None:
        return self.result.hint

    @property
    def error_code(self) -> str | None:
        return self.result.error_code


class BackupEngine:
    """Run one backup with real progress, stall detection and safe cancellation."""

    def __init__(
        self,
        udid: str,
        destination: Path,
        *,
        full: bool = False,
        job: Job | None = None,
        stall_timeout: float | None = None,
        wait_notice_after: float = 10.0,
        warn_after: float = 900.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[BackupProgress], None] | None = None,
        popen: Callable = subprocess.Popen,
        log_dir: Path | None = None,
        use_pty: bool | None = None,
        terminate_grace: float = 3.0,
    ) -> None:
        self.udid = udid
        self.destination = Path(destination)
        self.full = full
        self.job = job
        # ``None`` disables the inactivity watchdog: waiting for a passcode is expected.
        self.stall_timeout = stall_timeout
        # How long the tool may be silent before the UI explains the passcode prompt.
        self.wait_notice_after = wait_notice_after
        self.warn_after = warn_after
        self.poll_interval = max(0.01, float(poll_interval))
        self.on_progress = on_progress
        self._popen = popen
        # A pty only has meaning for a real subprocess; an injected factory supplies its own
        # streams, which keeps the engine testable without hardware.
        self.use_pty = (popen is subprocess.Popen) if use_pty is None else bool(use_pty)
        self.terminate_grace = terminate_grace
        self._log_dir = log_dir or (APP_STATE_DIR / "backups")

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._cancelled = threading.Event()
        self._cancel_reason = ""
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._log_handle = None
        self.log_path: Path | None = None

        self._stage = STAGE_INIT
        self._active_stage = STAGE_INIT
        self._message = "Preparing backup…"
        self._bytes = 0
        self._files = 0
        self._percent: float | None = None
        self._last_activity = time.monotonic()
        self._samples: list[tuple[float, int]] = []
        self._started = time.monotonic()
        self._last_line = ""
        self._last_scan_at = 0.0
        self._scan_cost = 0.0
        self._next_scan = 0.0
        self._scan_root: Path | None = None
        # The message shown while the tool is waiting for the user (passcode, unlock). It is
        # kept separately from the stage so that any new activity clears it immediately.
        self._waiting_notice: str | None = None
        self._passcode_requested = False
        self._passcode_requested_at = 0.0
        self.passcode_notice_seconds = 30.0
        self._tool_lines = 0
        self._error_code: str | None = None
        self._messages: list[str] = []
        self.max_messages = 200

    # ------------------------------------------------------------------ public API
    def run(self) -> BackupResult:
        """Perform the backup and return its outcome. Raises ``BackupFailure`` on error."""
        if self.use_pty and shutil.which("idevicebackup2") is None:
            raise RuntimeError(
                "idevicebackup2 is not installed (install libimobiledevice-utils)"
            )
        self.destination.mkdir(parents=True, exist_ok=True)
        previous_state = read_snapshot_state(self.destination, self.udid)
        self._open_log()
        self._started = time.monotonic()
        self._last_activity = self._started

        args = ["idevicebackup2", "-u", self.udid, "backup"]
        if self.full:
            args.append("--full")
        args.append(str(self.destination))
        self._log(f"launch: {' '.join(args)} (previous SnapshotState={previous_state!r})")
        self._emit(
            STAGE_INIT,
            f"Starting {'full' if self.full else 'incremental'} backup"
            + (f" (resuming {previous_state} snapshot)" if previous_state else ""),
        )

        exit_code: int | None = None
        try:
            self._proc = self._spawn(args)
            self._reader = threading.Thread(target=self._read_output, name="backup-output",
                                            daemon=True)
            self._poller = threading.Thread(target=self._progress_poller, name="backup-files",
                                            daemon=True)
            self._reader.start()
            self._poller.start()
            exit_code = self._wait_for_process()
        finally:
            self._stop.set()
            # A cancelled run does not need the tool's remaining output, so the reader is
            # not allowed to hold the caller while a hung process is still being killed.
            join_timeout = 1.0 if self._cancelled.is_set() else max(
                2.0, self.poll_interval + 1.0)
            for thread in (self._reader, self._poller):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=join_timeout)
            self._release_pty()

        elapsed = time.monotonic() - self._started
        cancelled = self._cancelled.is_set()
        size, count = self._final_totals(cancelled)
        state_after = read_snapshot_state(self.destination, self.udid)
        hint = error_hint(self._error_code)
        self._close_log()

        if cancelled:
            reason = self._cancel_reason or "Cancelled"
            return BackupResult(
                status="cancelled", destination=self.destination, udid=self.udid,
                full=self.full, bytes_on_disk=size, file_count=count,
                duration_seconds=round(elapsed, 1), snapshot_state=state_after,
                resumable=state_after not in (None, "finished"),
                last_line=self._last_line, log_path=self.log_path,
                error=reason, error_code=self._error_code, hint=hint,
                messages=list(self._messages),
            )

        if exit_code not in (0, None) or self._stage == STAGE_ERROR:
            detail = self._last_line or f"idevicebackup2 exited with status {exit_code}"
            message = detail if hint is None else f"{detail}\n\n{hint}"
            result = BackupResult(
                status="error", destination=self.destination, udid=self.udid,
                full=self.full, bytes_on_disk=size, file_count=count,
                duration_seconds=round(elapsed, 1), snapshot_state=state_after,
                resumable=state_after not in (None, "finished"),
                last_line=self._last_line, log_path=self.log_path,
                error=message, error_code=self._error_code, hint=hint,
                messages=list(self._messages),
            )
            self._emit(STAGE_ERROR, message)
            raise BackupFailure(result)

        # A device that refuses the transfer can still let the tool exit successfully. Every
        # real snapshot, including one with no user payload, writes Info.plist, Manifest.plist,
        # Manifest.db and Status.plist, so a byte total of zero with at most those few files
        # means nothing was written at all. Requiring both conditions keeps a legitimately
        # empty device from being reported as a failure.
        if size == 0 and count <= _PLIST_ONLY_FILE_BUDGET:
            message = (f"The device returned without writing any backup payload "
                       f"({count} files, {size} bytes). Unlock the iPhone and try again.")
            result = BackupResult(
                status="error", destination=self.destination, udid=self.udid,
                full=self.full, bytes_on_disk=size, file_count=count,
                duration_seconds=round(elapsed, 1), snapshot_state=state_after,
                resumable=state_after not in (None, "finished"),
                last_line=self._last_line, log_path=self.log_path,
                error=message, error_code=self._error_code,
                messages=list(self._messages),
            )
            self._emit(STAGE_ERROR, message)
            raise BackupFailure(result)

        # A successful exit only means the tool finished; check that what it left behind is a
        # backup this application can actually read.
        from ..backup.integrity import validate_backup

        snapshot_root = device_subdirectory(self.destination, self.udid)
        if not snapshot_root.is_dir():
            # The tool normally writes <destination>/<UDID>/; validate whatever it produced.
            snapshot_root = self.destination
        report = validate_backup(snapshot_root, payload_bytes=size, file_count=count)
        if not report.ok:
            message = report.describe()
            result = BackupResult(
                status="error", destination=self.destination, udid=self.udid,
                full=self.full, bytes_on_disk=size, file_count=count,
                duration_seconds=round(elapsed, 1), snapshot_state=state_after,
                resumable=state_after not in (None, "finished"),
                last_line=self._last_line, log_path=self.log_path,
                error=message, error_code=self._error_code,
                messages=list(self._messages),
            )
            self._emit(STAGE_ERROR, "The backup failed validation")
            raise BackupFailure(result)

        self._emit(STAGE_DONE, "Backup completed")
        result = BackupResult(
            status="completed", destination=self.destination, udid=self.udid,
            full=self.full, bytes_on_disk=size, file_count=count,
            duration_seconds=round(elapsed, 1), snapshot_state=state_after,
            resumable=state_after not in (None, "finished"),
            last_line=self._last_line, log_path=self.log_path,
            messages=list(self._messages),
        )
        if self.job is not None:
            self.job.progress(1.0, f"Backup finished · {count:,} files · {human_size(size)}")
        return result

    def cancel(self, reason: str = "Cancelled by user") -> None:
        """Request cancellation; returns immediately.

        The transfer is signalled at once and escalated to ``SIGKILL`` on a helper thread,
        so a caller (or a UI thread) never blocks on a tool waiting for a locked device.
        Partial data is left in place and stays resumable.
        """
        if not self._cancel_reason:
            self._cancel_reason = reason
        self._cancelled.set()
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError as exc:
            self._log(f"could not terminate child: {exc}")
            return
        threading.Thread(target=self._escalate, args=(proc,), name="backup-cancel",
                         daemon=True).start()

    def _escalate(self, proc: subprocess.Popen) -> None:
        """Wait out ``terminate_grace``, then kill. Never blocks a caller."""
        deadline = time.monotonic() + self.terminate_grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        self._log("child process ignored SIGTERM and SIGKILL; giving up")

    @property
    def progress(self) -> BackupProgress:
        with self._lock:
            return self._progress_snapshot()

    # ------------------------------------------------------------------ process handling
    def _spawn(self, args: list[str]) -> subprocess.Popen:
        if not self.use_pty:
            return self._popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        master, slave = pty.openpty()
        try:
            proc = self._popen(
                args, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                start_new_session=True,
            )
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

    def _wait_for_process(self) -> int | None:
        assert self._proc is not None
        while True:
            if self._cancelled.is_set():
                return self._proc.poll()
            if self.job is not None and self.job.cancelled:
                self.cancel()
                return self._proc.poll()
            code = self._proc.poll()
            if code is not None:
                return code
            with self._lock:
                idle = time.monotonic() - self._last_activity
                active = self._active_stage
            # A transfer that is waiting for the user to unlock the phone is not stalled, so
            # the watchdog only fires when it is explicitly enabled and much later than a
            # passcode prompt. Cancelling is always available and immediate.
            if (self.stall_timeout and active in (STAGE_ENUMERATE, STAGE_TRANSFER, STAGE_VERIFY)
                    and idle >= self.stall_timeout):
                reason = (f"No progress for {human_duration(idle)}; stopped the transfer "
                          "and kept the partial backup")
                self._log(reason)
                self.cancel(reason)
                return self._proc.poll()
            time.sleep(0.2)

    # ------------------------------------------------------------------ tool output
    def _read_output(self) -> None:
        try:
            for line in self._output_lines():
                self._handle_line(line)
        except (OSError, ValueError) as exc:
            if not self._cancelled.is_set():
                self._log(f"output reader failed: {exc}")
                self._emit(STAGE_ERROR, f"Could not read backup output: {exc}")

    def _output_lines(self) -> Iterator[str]:
        """Yield whole lines from the tool, however it was spawned.

        A pty delivers chunks separated by ``\\r`` as well as ``\\n`` (the tool redraws its
        progress line), so both are treated as terminators.
        """
        master = self._master_fd
        if master is not None:
            pending = b""
            while True:
                try:
                    ready, _w, _x = select.select([master], [], [], 1.0)
                except (OSError, ValueError):
                    break
                if not ready:
                    # A prompt printed without a newline ("Waiting for passcode… ") would stay
                    # invisible until more output arrived; surface it after a second of quiet.
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

    def _handle_line(self, line: str) -> None:
        self._last_line = line
        self._log_tool_line(line)
        lowered = line.casefold()
        with self._lock:
            # New output means the tool is no longer blocked on the previous prompt.
            if "passcode" not in lowered:
                self._passcode_requested = False
            self._waiting_notice = None
            if self._stage == STAGE_STALLED:
                self._stage = self._active_stage

        if self._error_code is None:
            for pattern in _ERROR_CODE_RES:
                match = pattern.search(line)
                if match:
                    self._error_code = match.group(1)
                    break

        if ("passcode" in lowered and self._error_code is None
                and not self._cancelled.is_set()):
            # iOS 16.1+ asks for the passcode at the start of a backup; the tool may print
            # this with or without a trailing newline, and the device may show its own
            # dialog. Either way the wait ends as soon as output or payload resumes.
            with self._lock:
                self._passcode_requested = True
                self._passcode_requested_at = time.monotonic()
                self._waiting_notice = None
            self._note("passcode requested on device")
            self._emit(self._active_stage,
                       "The device is asking for its passcode. Enter it on the iPhone; the "
                       "backup continues as soon as it is accepted.")
            return
        elif "requesting backup from device" in lowered:
            self._emit(STAGE_ENUMERATE, "Enumerating files on the device…")
        elif "full backup mode" in lowered:
            self._note("the tool selected full backup mode")
        elif "incremental backup mode" in lowered:
            self._emit(STAGE_ENUMERATE, "Incremental backup: comparing with the snapshot…")
        elif _RECEIVED_RE.search(line) and self._error_code is None:
            match = _RECEIVED_RE.search(line)
            assert match is not None
            self._emit(STAGE_TRANSFER,
                       f"Receiving files · {int(match.group(1)):,} received so far")
        elif "verifying" in lowered:
            self._emit(STAGE_VERIFY, "Verifying the backup with the device…")
        elif "backup successful" in lowered:
            self._emit(STAGE_DONE, "Device reported the backup as successful")
        elif ("aborted" in lowered or "failed" in lowered or "error" in lowered
              or self._error_code is not None):
            hint = error_hint(self._error_code)
            self._emit(STAGE_ERROR, line if hint is None else f"{line}\n{hint}")
        else:
            match = _PERCENT_RE.search(line)
            if match:
                self._set_percent(float(match.group(1)))
            self._note(line)
            self._emit(None, line)

    def _note(self, line: str) -> None:
        """Keep a bounded transcript of the run for the UI and the result.

        A full backup prints hundreds of thousands of progress lines; keeping all of them
        would grow without bound for no diagnostic value.
        """
        if self._messages and self._messages[-1] == line:
            return
        if len(self._messages) >= self.max_messages:
            del self._messages[0]
        self._messages.append(line)

    # ------------------------------------------------------------------ observation
    def _progress_poller(self) -> None:
        """Sample the destination, cheaply and often enough to be useful.

        One walk of a 70 000-file tree costs seconds, so the walk interval adapts to its own
        measured cost (never below ``poll_interval``, always keeping the walker under a
        quarter of the wall clock). Between walks the UI is ticked once a second from the
        last measurement, so throughput and ETA stay live without extra I/O.
        """
        tick = min(1.0, self.poll_interval)
        while not self._stop.wait(tick):
            try:
                now = time.monotonic()
                if now >= self._next_scan:
                    scan_started = time.monotonic()
                    size, count = self._snapshot()
                    self._scan_cost = time.monotonic() - scan_started
                    self._next_scan = time.monotonic() + max(self.poll_interval,
                                                             self._scan_cost * 4.0)
                    self._last_scan_at = time.monotonic()
                    self._record_totals(size, count)
                self._emit_tick()
            except Exception:  # noqa: BLE001 - an observer must never stop observing
                log.exception("backup progress poller iteration failed")

    def _record_totals(self, size: int, count: int) -> None:
        now = time.monotonic()
        with self._lock:
            if self._stop.is_set():
                return
            changed = size != self._bytes or count != self._files
            if changed:
                self._last_activity = now
                # Data is arriving: whatever the tool was waiting for has been supplied, so
                # the stage and its "waiting" text return to the live transfer immediately.
                self._waiting_notice = None
                self._passcode_requested = False
                if self._active_stage in (STAGE_INIT, STAGE_ENUMERATE):
                    self._active_stage = STAGE_TRANSFER
                if self._stage in (STAGE_INIT, STAGE_ENUMERATE, STAGE_STALLED):
                    self._stage = self._active_stage
            self._bytes, self._files = size, count
            self._samples.append((now, size))
            while self._samples and now - self._samples[0][0] > 60:
                self._samples.pop(0)

    def _emit_tick(self) -> None:
        """Publish live throughput between scans, and explain a wait exactly once.

        A wait for the user (the passcode prompt iOS shows when a backup starts) is normal
        and must never look like a stall or abort the run. It is reported once, as a notice
        held apart from the stage, and cleared the moment a byte or a line of tool output
        arrives — so entering the passcode is reflected immediately.
        """
        with self._lock:
            now = time.monotonic()
            idle = now - self._last_activity
            active = self._active_stage
            stage = self._stage
            notice = self._waiting_notice
            # A passcode prompt that produced no output for a while is stale: the device may
            # have accepted it and still be preparing, so stop insisting on the passcode.
            passcode = (self._passcode_requested
                        and now - self._passcode_requested_at < self.passcode_notice_seconds)
            if not passcode:
                self._passcode_requested = False
        if stage in (STAGE_DONE, STAGE_ERROR):
            return

        if active in (STAGE_ENUMERATE, STAGE_TRANSFER, STAGE_VERIFY) and stage != STAGE_STALLED:
            if self.stall_timeout and idle >= self.stall_timeout:
                text = (f"No file activity for {human_duration(idle)}; the transfer may be "
                        "stalled")
                if notice != text:
                    self._set_waiting_notice(text)
                    self._emit(STAGE_STALLED, text)
                return
            if idle >= self.wait_notice_after:
                text = self._waiting_text(passcode)
                if notice != text:
                    self._set_waiting_notice(text)
                    self._emit(STAGE_STALLED, text)
                return

        if active == STAGE_INIT:
            return
        if active == STAGE_ENUMERATE and not (self._bytes or self._files):
            # Keep the tool's own message ("Comparing with the snapshot…") visible until
            # there is something to count.
            return
        self._emit(active, self._activity_text())

    @staticmethod
    def _waiting_text(passcode: bool) -> str:
        """Explain a quiet device accurately.

        Only claim the device is asking for a passcode when the tool actually said so: a
        backup of a large library spends a minute or two comparing files before any data
        moves, and calling that "waiting for the passcode" made a healthy run look stuck.
        """
        if passcode:
            return ("The iPhone is asking for its passcode. Enter it on the device; the "
                    "backup continues as soon as it is accepted.")
        return ("Preparing the backup on the device (comparing files, nothing is wrong). "
                "If the iPhone is locked, wake it and enter its passcode once — after that "
                "the screen can lock again and the backup keeps running.")

    def _set_waiting_notice(self, text: str | None) -> None:
        with self._lock:
            self._waiting_notice = text

    def _activity_text(self) -> str:
        with self._lock:
            size, count = self._bytes, self._files
            rate = self._rate()
            eta = self._eta(rate)
        parts = [f"{count:,} files", human_size(size)]
        if rate > 0:
            parts.append(f"{human_size(rate)}/s")
        if eta is not None:
            parts.append(f"~{human_duration(eta)} left")
        return " · ".join(parts)

    def _rate(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        first_t, first_bytes = self._samples[0]
        last_t, last_bytes = self._samples[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return 0.0
        return max(0.0, (last_bytes - first_bytes) / elapsed)

    def _eta(self, rate: float) -> float | None:
        if rate <= 0 or self._percent is None or not 0 < self._percent < 100:
            return None
        estimated_total = self._bytes * 100.0 / self._percent
        return max(0.0, (estimated_total - self._bytes) / rate)

    def _progress_snapshot(self) -> BackupProgress:
        now = time.monotonic()
        rate = self._rate()
        return BackupProgress(
            stage=self._stage,
            message=self._message,
            bytes_on_disk=self._bytes,
            file_count=self._files,
            percent=self._percent,
            rate_bytes_per_sec=rate,
            eta_seconds=self._eta(rate),
            stall_seconds=max(0.0, now - self._last_activity),
            elapsed_seconds=now - self._started,
            error=None if self._stage != STAGE_ERROR else self._message,
        )

    def _set_percent(self, value: float) -> None:
        with self._lock:
            if self._percent is None or value >= self._percent:
                self._percent = min(100.0, value)

    def _final_totals(self, cancelled: bool) -> tuple[int, int]:
        """Totals for the result: never re-walk the tree just to report a number."""
        fresh = self._last_scan_at and (time.monotonic() - self._last_scan_at) < 3.0
        if (cancelled or fresh) and (self._bytes or self._files):
            return self._bytes, self._files
        return self._snapshot()

    def _snapshot(self) -> tuple[int, int]:
        """Iterative scan of the destination; never retains a path list."""
        device_dir = device_subdirectory(self.destination, self.udid)
        if device_dir.is_dir():
            # The tool writes into <dest>/<UDID>; scanning only that skips its sibling
            # scratch files and needs one level less of walking.
            self._scan_root = device_dir
        root = self._scan_root or self.destination
        total = 0
        files = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                files += 1
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
        return total, files

    # ------------------------------------------------------------------ reporting
    def _emit(self, stage: str | None, message: str | None) -> BackupProgress:
        with self._lock:
            if stage is not None:
                self._stage = stage
                if stage != STAGE_STALLED:
                    self._active_stage = stage
            if message is not None:
                self._message = message
            progress = self._progress_snapshot()
        if self.on_progress is not None:
            try:
                self.on_progress(progress)
            except Exception:  # noqa: BLE001 - a UI callback must not break the transfer
                log.debug("progress callback failed", exc_info=True)
        if self.job is not None:
            fraction = -1.0
            if progress.percent is not None:
                fraction = progress.percent / 100.0
            self.job.progress(fraction, f"{progress.message} · {self._activity_text()}"
                              if progress.stage in (STAGE_TRANSFER, STAGE_ENUMERATE)
                              else progress.message)
        return progress

    # ------------------------------------------------------------------ logging
    def _open_log(self) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.udid)[-8:]
            self.log_path = self._log_dir / f"backup-{safe}-{stamp}.log"
            self._log_handle = self.log_path.open("w", encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot create backup log: %s", exc)
            self._log_handle = None

    def _log_tool_line(self, message: str) -> None:
        """Log every protocol line at first, then thin them out.

        A full backup prints hundreds of thousands of progress lines; keeping all of them
        would write tens of megabytes per run for no diagnostic value.
        """
        self._tool_lines += 1
        if self._tool_lines <= 400 or self._tool_lines % 200 == 0:
            self._log(f"tool: {message}")

    def _log(self, message: str) -> None:
        if self._log_handle is None:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Device identifiers stay out of the log; the transfer itself is not logged.
        safe = _UDID_RE.sub("[device]", str(message))
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


def create_backup(
    udid: str,
    destination: Path,
    job: Job | None,
    *,
    full: bool = False,
    stall_timeout: float | None = None,
    on_progress: Callable[[BackupProgress], None] | None = None,
) -> BackupResult:
    """Create a backup, reporting progress through ``job``.

    ``full=False`` (the default) reuses an existing snapshot on the device and destination,
    which is what makes repeat and resumed backups fast. A cancelled run raises
    ``OperationCancelled`` so callers never mistake it for a completed backup.
    """
    engine = BackupEngine(udid, destination, full=full, job=job,
                          stall_timeout=stall_timeout, on_progress=on_progress)
    result = engine.run()
    if result.status == "cancelled":
        raise OperationCancelled("backup")
    return result
