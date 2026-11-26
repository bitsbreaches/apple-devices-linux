# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Discovery and metadata reading for iTunes/Finder backups."""

from __future__ import annotations

import logging
import os
import plistlib
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..asyncutil import Job
from ..models import BackupInfo

log = logging.getLogger(__name__)

MANIFEST_DB = "Manifest.db"
MANIFEST_PLIST = "Manifest.plist"
INFO_PLIST = "Info.plist"
STATUS_PLIST = "Status.plist"


def is_backup_directory(path: Path) -> bool:
    return (path / MANIFEST_DB).is_file() or (path / MANIFEST_PLIST).is_file()


def _read_plist(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            value = plistlib.load(handle)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - malformed plists are reported, not fatal
        log.warning("Cannot read %s: %s", path, exc)
        return {}


def read_backup_info(path: Path, *, compute_size: bool = False, job: Job | None = None) -> BackupInfo:
    """Read the four metadata plists and return a populated :class:`BackupInfo`."""
    path = Path(path)
    info = BackupInfo(path=path)

    info_plist = _read_plist(path / INFO_PLIST)
    manifest_plist = _read_plist(path / MANIFEST_PLIST)
    status_plist = _read_plist(path / STATUS_PLIST)

    info.udid = str(info_plist.get("Target Identifier") or info_plist.get("Unique Identifier") or path.name)
    info.device_name = str(info_plist.get("Device Name") or info_plist.get("Display Name") or "")
    info.product_type = str(info_plist.get("Product Type") or "")
    info.product_version = str(info_plist.get("Product Version") or "")
    info.build_version = str(info_plist.get("Build Version") or "")
    info.serial_number = str(info_plist.get("Serial Number") or "")
    info.imei = str(info_plist.get("IMEI") or "")
    info.phone_number = str(info_plist.get("Phone Number") or "")
    applications = info_plist.get("Applications")
    info.applications = len(applications) if isinstance(applications, dict) else 0

    info.is_encrypted = bool(manifest_plist.get("IsEncrypted"))
    info.was_passcode_set = bool(manifest_plist.get("WasPasscodeSet"))
    info.manifest_version = str(manifest_plist.get("Version") or "")
    info.is_full_backup = bool(status_plist.get("IsFullBackup"))
    info.status_version = str(status_plist.get("Version") or "")

    last_backup = info_plist.get("Last Backup Date") or status_plist.get("Date") or manifest_plist.get("Date")
    if isinstance(last_backup, datetime):
        info.last_backup = last_backup

    if compute_size:
        info.size_bytes, info.file_count = directory_stats(path, job=job)

    return info


def directory_stats(path: Path, job: Job | None = None) -> tuple[int, int]:
    """Return ``(total_bytes, file_count)`` for a backup directory tree."""
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            count += 1
        if job is not None and count % 2000 == 0:
            job.check_cancelled()
    return total, count


def discover(directories: Iterable[str | Path], *, job: Job | None = None) -> list[BackupInfo]:
    """Scan every configured directory for backup folders."""
    found: list[BackupInfo] = []
    seen: set[Path] = set()
    candidates = list(directories)
    for index, directory in enumerate(candidates):
        base = Path(directory).expanduser()
        if job is not None:
            job.progress(index / max(1, len(candidates)), f"Scanning {base}")
        if not base.is_dir():
            continue
        entries = [base] if is_backup_directory(base) else []
        try:
            entries.extend(sorted(p for p in base.iterdir() if p.is_dir() and is_backup_directory(p)))
        except OSError as exc:
            log.warning("Cannot list %s: %s", base, exc)
            continue
        for entry in entries:
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                found.append(read_backup_info(entry))
            except Exception as exc:  # noqa: BLE001 - keep scanning other backups
                log.error("Failed to read backup %s: %s", entry, exc)
                found.append(BackupInfo(path=entry, error=str(exc)))
    if job is not None:
        job.progress(1.0, f"{len(found)} backup(s) found")
    return found
