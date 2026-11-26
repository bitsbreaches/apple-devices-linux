# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Erase all content and settings: restore an iPhone to its factory defaults.

The device's own ``EraseDevice`` MobileBackup2 message is the request Finder and Apple
Configurator send for “Erase All Content and Settings”, so the iPhone keeps its activation
and firmware and comes back with the “Hello” setup assistant instead of a wiped backup.

Because this destroys every item on the device it is gated in two places:

* the UI asks for an explicit typed acknowledgement before it starts, and
* iOS refuses the request while Find My iPhone / Activation Lock is on, so the engine
  refuses to send it at all in that state and explains how to turn it off.

The erase request is sent through the LGPL libimobiledevice MobileBackup2 C API.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..asyncutil import Job
from ..config import APP_STATE_DIR
from ..models import human_duration
from .backup_engine import STAGE_DONE, STAGE_ERROR, STAGE_INIT, BackupProgress
from .safety import UNACTIVATED_REASON, activation_lock_reason

log = logging.getLogger(__name__)

ACTIVATION_LOCK_REASON = activation_lock_reason("an erase")


class EraseRejected(RuntimeError):
    """The device cannot be erased (checked before anything is sent)."""


@dataclass(frozen=True)
class ErasePlan:
    """What is about to be destroyed, resolved before the erase starts."""

    udid: str
    device_name: str
    product_type: str = ""
    product_version: str = ""


@dataclass
class EraseResult:
    status: str
    udid: str
    device_name: str
    seconds: float = 0.0
    error: str | None = None
    log_path: Path | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class EraseFailure(RuntimeError):
    def __init__(self, result: EraseResult) -> None:
        super().__init__(result.error or "Erase failed")
        self.result = result


class _EraseCancelled(Exception):
    pass


def build_erase_plan(udid: str, device_name: str = "", product_type: str = "",
                     product_version: str = "",
                     activation_locked: bool | None = None,
                     activation_state: str = "") -> ErasePlan:
    """Validate the preconditions for an erase. Raises :class:`EraseRejected`.

    ``activation_locked``/``activation_state`` come from
    :func:`devicedock.devices.safety.destructive_preflight`; the UI refuses before this
    is called, and these checks make the engine safe to use on its own.
    """
    if not udid:
        raise EraseRejected("No device is connected.")
    if activation_locked:
        raise EraseRejected(ACTIVATION_LOCK_REASON)
    if activation_state.casefold() == "unactivated":
        raise EraseRejected(UNACTIVATED_REASON)
    return ErasePlan(
        udid=udid,
        device_name=device_name or "the device",
        product_type=product_type,
        product_version=product_version,
    )


class EraseEngine:
    """Send ``EraseDevice`` to one device, with a live stage and a log file."""

    def __init__(
        self,
        plan: ErasePlan,
        *,
        job: Job | None = None,
        on_progress: Callable[[BackupProgress], None] | None = None,
        log_dir: Path | None = None,
        erase_dir: Path | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.plan = plan
        self.job = job
        self.on_progress = on_progress
        self._log_dir = log_dir or (APP_STATE_DIR / "erases")
        self.erase_dir = erase_dir or (APP_STATE_DIR / "erase-channel")
        self.timeout = timeout

        self._lock = threading.RLock()
        self._stage = STAGE_INIT
        self._message = "Preparing erase…"
        self._started = time.monotonic()
        self._log_handle = None
        self.log_path: Path | None = None

    # ------------------------------------------------------------------ run
    def run(self) -> EraseResult:
        self._open_log()
        self._started = time.monotonic()
        self._log(f"launch: EraseDevice {self.plan.udid}")
        self._emit(STAGE_INIT, "Contacting the device…")
        if self._cancel_requested():
            self._close_log()
            return EraseResult("cancelled", self.plan.udid, self.plan.device_name,
                               error="Cancelled by the user", log_path=self.log_path)

        try:
            self._run()
        except _EraseCancelled:
            self._close_log()
            self._emit(STAGE_ERROR, "Erase cancelled before it started")
            return EraseResult("cancelled", self.plan.udid, self.plan.device_name,
                               round(time.monotonic() - self._started, 1),
                               error="Cancelled by the user", log_path=self.log_path)
        except Exception as exc:  # noqa: BLE001 - surfaced through the result
            message = f"{type(exc).__name__}: {exc}"
            self._log(f"failed: {message}")
            self._close_log()
            self._emit(STAGE_ERROR, message)
            raise EraseFailure(EraseResult(
                "error", self.plan.udid, self.plan.device_name,
                round(time.monotonic() - self._started, 1), error=message,
                log_path=self.log_path,
            )) from exc

        seconds = time.monotonic() - self._started
        self._log(f"erase request sent after {human_duration(seconds)}")
        self._close_log()
        self._emit(STAGE_DONE, "Erase started")
        if self.job is not None:
            self.job.progress(1.0, "Erase started")
        return EraseResult("completed", self.plan.udid, self.plan.device_name,
                           round(seconds, 1), log_path=self.log_path)

    # ------------------------------------------------------------------ request
    def _run(self) -> None:
        self.erase_dir.mkdir(parents=True, exist_ok=True)
        if self._cancel_requested():
            raise _EraseCancelled()
        self._emit(STAGE_INIT, "Asking iOS to erase all content and settings…")
        from .libimobile import erase_device_request
        erase_device_request(self.plan.udid)
        self._emit(STAGE_INIT,
                   "The erase request was sent; the phone will restart at the setup screen.")

    # ------------------------------------------------------------------ reporting
    def _cancel_requested(self) -> bool:
        return self.job is not None and self.job.cancelled

    def _emit(self, stage: str | None, message: str | None) -> BackupProgress:
        with self._lock:
            if stage is not None:
                self._stage = stage
            if message is not None:
                self._message = message
            progress = BackupProgress(
                stage=self._stage, message=self._message, percent=None,
                elapsed_seconds=time.monotonic() - self._started,
            )
        if self.on_progress is not None:
            try:
                self.on_progress(progress)
            except Exception:  # noqa: BLE001
                log.debug("erase progress callback failed", exc_info=True)
        if self.job is not None:
            self.job.progress(-1.0, f"{progress.message} · "
                                    f"{human_duration(progress.elapsed_seconds)}")
        return progress

    # ------------------------------------------------------------------ logging
    def _open_log(self) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.plan.udid)[-8:]
            self.log_path = self._log_dir / f"erase-{safe}-{stamp}.log"
            self._log_handle = self.log_path.open("w", encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot create erase log: %s", exc)

    def _log(self, message: str) -> None:
        if self._log_handle is None:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            self._log_handle.write(f"[{stamp}] [{self._stage}] {message}\n")
            self._log_handle.flush()
        except OSError:
            pass

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None
