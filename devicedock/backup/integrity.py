# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Post-backup validation of a backup directory.

``idevicebackup2`` can exit successfully without leaving a backup the application (or Finder)
can actually read, so the transfer's exit status is not evidence of a usable backup. This
module checks the artefacts Apple defines, and understands the encrypted case where
``Manifest.db`` is deliberately not a SQLite database yet.
"""

from __future__ import annotations

import plistlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: Files every real snapshot contains; ``Manifest.db`` is absent only in encrypted backups.
_REQUIRED = ("Info.plist", "Manifest.plist", "Status.plist")


@dataclass
class IntegrityReport:
    """Outcome of inspecting a backup directory."""

    ok: bool
    encrypted: bool = False
    file_count: int = 0
    payload_bytes: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One sentence suitable for an error dialog."""
        if self.ok:
            return "The backup is complete and readable."
        return "The backup is not usable:\n\n" + "\n".join(f"• {item}" for item in self.problems)

    def summary(self) -> str:
        """Short description for logs and reports, including non-fatal warnings."""
        parts = [f"{self.file_count:,} files", f"{self.payload_bytes:,} bytes"]
        if self.problems:
            parts.append(f"{len(self.problems)} problem(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return ", ".join(parts)


def _load_plist(path: Path, problems: list[str], label: str) -> dict | None:
    if not path.is_file():
        problems.append(f"{label} is missing ({path.name}).")
        return None
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
    except Exception as exc:  # noqa: BLE001 - any parse failure disqualifies the backup
        problems.append(f"{label} cannot be read ({exc}).")
        return None
    if not isinstance(value, dict):
        problems.append(f"{label} has an unexpected structure.")
        return None
    return value


def validate_backup(device_dir: Path, *, payload_bytes: int = 0,
                    file_count: int = 0) -> IntegrityReport:
    """Check the metadata of one completed backup directory.

    ``payload_bytes``/``file_count`` are the totals the engine already measured, so no extra
    filesystem walk happens here.
    """
    device_dir = Path(device_dir)
    report = IntegrityReport(ok=False, payload_bytes=payload_bytes, file_count=file_count)
    if not device_dir.is_dir():
        report.problems.append(f"The backup directory does not exist ({device_dir}).")
        return report

    for name in _REQUIRED:
        if not (device_dir / name).is_file():
            report.problems.append(f"{name} is missing from the backup.")

    manifest_plist = _load_plist(device_dir / "Manifest.plist", report.problems,
                                 "Manifest.plist")
    _load_plist(device_dir / "Status.plist", report.problems, "Status.plist")

    encrypted = False
    if manifest_plist is not None:
        encrypted = bool(manifest_plist.get("IsEncrypted"))
        if encrypted:
            if not manifest_plist.get("ManifestKey"):
                report.problems.append(
                    "Manifest.plist says the backup is encrypted but carries no manifest key.")
            if not manifest_plist.get("BackupKeyBag"):
                report.problems.append(
                    "Manifest.plist says the backup is encrypted but carries no keybag.")
            db = device_dir / "Manifest.db"
            if not db.is_file():
                report.problems.append("Manifest.db is missing from the backup.")
            elif db.stat().st_size == 0:
                report.problems.append("Manifest.db is empty.")
        else:
            _check_plaintext_manifest(device_dir / "Manifest.db", report.problems)

    report.encrypted = encrypted
    if payload_bytes <= 0:
        report.problems.append(
            "No payload was written: the backup contains metadata only.")
    if not encrypted and not report.problems:
        _check_sampled_payloads(device_dir, report)
    report.ok = not report.problems
    return report


def _check_sampled_payloads(device_dir: Path, report: IntegrityReport, sample: int = 50) -> None:
    """Confirm that manifest entries have payload files, not just rows.

    A transfer can leave a manifest that lists files whose bytes never arrived. The sample
    is small and bounded so validation stays cheap; a missing payload is a warning rather
    than a failure because iOS legitimately prunes files between snapshots.
    """
    from .manifest import ManifestError, ManifestQuery

    try:
        manifest = ManifestQuery(device_dir / "Manifest.db")
    except ManifestError as exc:
        report.warnings.append(f"Payload presence was not checked: {exc}")
        return
    try:
        checked = 0
        missing = 0
        for record in manifest.iter_files(batch_size=200):
            if not record.is_file or not record.size:
                continue
            payload = device_dir / record.file_id[:2] / record.file_id
            if not payload.is_file():
                missing += 1
            checked += 1
            if checked >= sample:
                break
        if missing:
            report.warnings.append(
                f"{missing} of {checked} sampled manifest entries have no payload file in "
                "this backup.")
    finally:
        manifest.close()


def _check_plaintext_manifest(db_path: Path, problems: list[str]) -> None:
    if not db_path.is_file():
        problems.append("Manifest.db is missing from the backup.")
        return
    try:
        # No `immutable=1`: a freshly written backup may still have a WAL file that must be
        # read for an accurate answer.
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        problems.append(f"Manifest.db cannot be opened ({exc}).")
        return
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            problems.append(f"Manifest.db failed its integrity check ({integrity}).")
            return
        rows = connection.execute("SELECT count(*) FROM Files").fetchone()
        if not rows or not rows[0]:
            problems.append("Manifest.db contains no file records.")
    except sqlite3.Error as exc:
        problems.append(f"Manifest.db is not a readable backup manifest ({exc}).")
    finally:
        connection.close()
