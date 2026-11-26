# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Application state: discovered backups, devices and the currently opened vault."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from collections import defaultdict
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .. import secrets
from ..asyncutil import run_in_thread
from ..backup import BackupVault, discover, read_backup_info
from ..config import Settings
from ..devices import libimobile
from ..models import BackupInfo, DeviceInfo

log = logging.getLogger(__name__)


class AppState:
    """Single source of truth shared by all pages.

    Listener events: ``backups``, ``devices``, ``vault``, ``settings``, ``busy``.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backups: list[BackupInfo] = []
        self.devices: list[DeviceInfo] = []
        self.device_error: str | None = None
        self._devices_refreshing = False
        self._device_refresh_details_pending = False
        self._device_refresh_waiters: list[Callable[[], None]] = []
        self.tool_status: dict[str, bool] = libimobile.tool_status()
        self.vault: BackupVault | None = None
        self.active_backup: BackupInfo | None = None
        self.busy_message: str = ""
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._session_passwords: dict[str, str] = {}
        self._main_thread = threading.main_thread()

    # --- observer plumbing -------------------------------------------
    def subscribe(self, event: str, callback: Callable) -> None:
        self._listeners[event].append(callback)

    def emit(self, event: str, *args: object) -> None:
        callbacks = list(self._listeners[event])

        def dispatch() -> bool:
            for callback in callbacks:
                try:
                    callback(*args)
                except Exception:  # noqa: BLE001 - a broken page must not break others
                    log.exception("Listener for %s failed", event)
            return GLib.SOURCE_REMOVE

        if threading.current_thread() is self._main_thread:
            dispatch()
        else:
            GLib.idle_add(dispatch)

    # --- settings ------------------------------------------------------
    def update_settings(self, **changes: object) -> None:
        for key, value in changes.items():
            setattr(self.settings, key, value)
        self.settings.save()
        self.emit("settings")

    # --- backups -------------------------------------------------------
    def scan_backups(self, on_done: Callable[[list[BackupInfo]], None] | None = None) -> None:
        def work(job):
            return discover(self.settings.backup_directories, job=job)

        def success(result: list[BackupInfo]) -> None:
            self.backups = result
            self.emit("backups")
            self._measure_missing_sizes()
            if on_done is not None:
                on_done(result)

        run_in_thread(work, name="scan-backups", on_success=success,
                      on_error=lambda exc: self.emit("busy", ""), )

    def add_backup_size_hint(self, result) -> None:
        """Apply size/file counts from a finished backup, avoiding a second tree walk."""
        destination = Path(result.destination).resolve()
        for info in self.backups:
            if info.path.resolve() == destination:
                info.size_bytes = result.bytes_on_disk
                info.file_count = result.file_count
                self.emit("backups")
                return

    def _measure_missing_sizes(self) -> None:
        """Fill in sizes of newly discovered backups in one background pass."""
        pending = [info for info in self.backups if not info.size_bytes and not info.error]
        if not pending:
            return

        def work(job):
            for index, info in enumerate(pending):
                job.check_cancelled()
                job.progress(index / len(pending), f"Measuring {info.display_name}")
                try:
                    updated = read_backup_info(info.path, compute_size=True, job=job)
                except Exception as exc:  # noqa: BLE001 - a size is optional
                    log.warning("Cannot measure %s: %s", info.path, exc)
                    continue
                info.size_bytes = updated.size_bytes
                info.file_count = updated.file_count
                self.emit("backups")

        run_in_thread(work, name="measure-backups")

    def fill_backup_size(self, info: BackupInfo, on_done: Callable[[], None] | None = None) -> None:
        def work(job):
            return read_backup_info(info.path, compute_size=True, job=job)

        def success(updated: BackupInfo) -> None:
            info.size_bytes = updated.size_bytes
            info.file_count = updated.file_count
            self.emit("backups")
            if on_done is not None:
                on_done()

        run_in_thread(work, name="backup-size", on_success=success)

    # --- devices -------------------------------------------------------
    @staticmethod
    def _device_fingerprint(devices: list[DeviceInfo]) -> tuple:
        return tuple(
            (device.udid, device.name, device.product_type, device.product_version,
             device.build_version, device.connection_type, device.activation_locked,
             device.error)
            for device in devices
        )

    def _finish_device_refresh(self) -> None:
        self._devices_refreshing = False
        if self._device_refresh_details_pending:
            self._device_refresh_details_pending = False
            self.refresh_devices(refresh_details=True)
            return
        waiters, self._device_refresh_waiters = self._device_refresh_waiters, []
        for callback in waiters:
            callback()

    def refresh_devices(self, on_done: Callable[[], None] | None = None, *,
                        refresh_details: bool = False) -> None:
        """Refresh device state with a single in-flight discovery operation.

        Timer polls reuse the identity data for devices that remain connected, so they only
        query usbmuxd for connection transitions. Explicit user refreshes can request fresh
        Lockdown metadata. Concurrent callers share one discovery result.
        """
        if self._devices_refreshing:
            if on_done is not None:
                self._device_refresh_waiters.append(on_done)
            if refresh_details:
                self._device_refresh_details_pending = True
            return
        if on_done is not None:
            self._device_refresh_waiters.append(on_done)
        self._devices_refreshing = True

        old_fingerprint = self._device_fingerprint(self.devices)
        old_error = self.device_error

        def publish(result: list[DeviceInfo], error: str | None) -> None:
            self.devices = result
            self.device_error = error
            if (self._device_fingerprint(result) != old_fingerprint or error != old_error):
                self.emit("devices")
            self._finish_device_refresh()

        def work(_job):
            return libimobile.discover_devices(self.devices, refresh_details=refresh_details)

        def success(result: list[DeviceInfo]) -> None:
            publish(result, None)

        def failure(exc: Exception) -> None:
            publish([], str(exc))

        if libimobile.missing_required_tools():
            message = (
                "libimobiledevice tools are not installed (need idevice_id and ideviceinfo)."
            )
            publish([], message)
            return
        usbmuxd_message = libimobile.usbmuxd_unavailable_message()
        if usbmuxd_message is not None:
            publish([], usbmuxd_message)
            return
        try:
            run_in_thread(work, name="refresh-devices", on_success=success, on_error=failure)
        except Exception as exc:
            failure(exc)

    # --- vault ---------------------------------------------------------
    def activate_backup(self, info: BackupInfo | None) -> None:
        if self.active_backup is not info:
            self.close_vault()
            self.active_backup = info

    def close_vault(self) -> None:
        if self.vault is not None:
            self.vault.close()
            self.vault = None
            self.emit("vault")

    def password_for(self, info: BackupInfo) -> str | None:
        cached = self._session_passwords.get(info.udid)
        if cached:
            return cached
        if self.settings.store_passwords:
            stored = secrets.lookup_password(info.udid)
            if stored:
                self._session_passwords[info.udid] = stored
                return stored
        return None

    def password_for_udid(self, udid: str) -> str | None:
        cached = self._session_passwords.get(udid)
        if cached:
            return cached
        if self.settings.store_passwords:
            return secrets.lookup_password(udid)
        return None

    def remember_password_for_udid(self, udid: str, password: str) -> None:
        self._session_passwords[udid] = password
        if self.settings.store_passwords:
            secrets.store_password(udid, password)

    def remember_password(self, info: BackupInfo, password: str) -> None:
        self._session_passwords[info.udid] = password
        if self.settings.store_passwords:
            secrets.store_password(info.udid, password)

    def forget_password(self, info: BackupInfo) -> None:
        self._session_passwords.pop(info.udid, None)
        if self.settings.store_passwords:
            secrets.clear_password(info.udid)

    def unlock(
        self,
        info: BackupInfo,
        password: str | None,
        *,
        on_success: Callable[[BackupVault], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.activate_backup(info)

        def work(job):
            job.progress(0.1, "Deriving key from password")
            vault = BackupVault(info)
            try:
                vault.unlock(password)
            except Exception:
                vault.close()
                raise
            job.progress(0.8, "Opening manifest")
            return vault

        def success(vault: BackupVault) -> None:
            if password:
                self.remember_password(info, password)
            self.vault = vault
            self.emit("vault")
            if on_success is not None:
                on_success(vault)

        def failure(exc: Exception) -> None:
            self.emit("vault")
            if on_error is not None:
                on_error(exc)

        run_in_thread(work, name="unlock-backup", on_success=success, on_error=failure)

    @property
    def unlocked(self) -> bool:
        return self.vault is not None and self.vault.unlocked
