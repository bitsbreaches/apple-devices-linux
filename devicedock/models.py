# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Domain models shared by the backup, device and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class BackupInfo:
    """A discovered iTunes/Finder backup directory."""

    path: Path
    udid: str = ""
    device_name: str = ""
    product_type: str = ""
    product_version: str = ""
    build_version: str = ""
    serial_number: str = ""
    imei: str = ""
    phone_number: str = ""
    last_backup: datetime | None = None
    is_encrypted: bool = False
    was_passcode_set: bool = False
    is_full_backup: bool = False
    status_version: str = ""
    manifest_version: str = ""
    size_bytes: int = 0
    file_count: int = 0
    applications: int = 0
    error: str | None = None

    @property
    def display_name(self) -> str:
        name = self.device_name or self.product_type or self.udid or self.path.name
        return name

    @property
    def model_name(self) -> str:
        from .devices.models_map import marketing_name

        return marketing_name(self.product_type) if self.product_type else ""

    @property
    def firmware(self) -> str:
        if self.product_version and self.build_version:
            return f"iOS {self.product_version} ({self.build_version})"
        return f"iOS {self.product_version}" if self.product_version else ""

    @property
    def last_backup_text(self) -> str:
        return self.last_backup.strftime("%Y-%m-%d %H:%M") if self.last_backup else "unknown"


@dataclass
class FileRecord:
    """One row of the backup manifest ``Files`` table."""

    file_id: str
    domain: str
    relative_path: str
    flags: int = 0
    size: int | None = None
    mtime: float | None = None
    mode: int | None = None
    protection_class: int | None = None
    encryption_key: bytes | None = None
    symlink_target: str | None = None
    digest: bytes | None = None

    @property
    def is_file(self) -> bool:
        return self.flags == 1

    @property
    def is_directory(self) -> bool:
        return self.flags == 2

    @property
    def is_symlink(self) -> bool:
        return self.flags == 4

    @property
    def mtime_text(self) -> str:
        if not self.mtime:
            return "—"
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def size_text(self) -> str:
        if self.is_directory:
            return "—"
        return human_size(self.size)


@dataclass
class DeviceInfo:
    """A connected iOS device as reported by libimobiledevice."""

    udid: str
    name: str = ""
    product_type: str = ""
    product_version: str = ""
    build_version: str = ""
    serial_number: str = ""
    imei: str = ""
    phone_number: str = ""
    connection_type: str = ""
    # ``True`` when Find My iPhone / Activation Lock is on; ``None`` when the device did not
    # report it. iOS refuses both a backup restore and an erase while this is ``True``.
    activation_locked: bool | None = None
    # ``"Activated"``/``"Unactivated"`` as reported by Lockdown; ``""`` when not reported.
    # An unactivated device is still at the setup assistant, so it can be neither restored
    # to nor erased.
    activation_state: str = ""
    raw: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def model_name(self) -> str:
        from .devices.models_map import marketing_name

        return marketing_name(self.product_type) if self.product_type else ""

    @property
    def firmware(self) -> str:
        if self.product_version and self.build_version:
            return f"iOS {self.product_version} ({self.build_version})"
        return f"iOS {self.product_version}" if self.product_version else ""

    @property
    def display_name(self) -> str:
        return self.name or self.model_name or self.udid


def human_size(size: int | float | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds: float | None) -> str:
    """Compact duration for progress and summaries: ``45s``, ``7m 12s``, ``2h 05m``."""
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
