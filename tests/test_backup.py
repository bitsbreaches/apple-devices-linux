# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
from __future__ import annotations

import os
from pathlib import Path

import pytest

from devicedock.backup import (
    BackupLockedError,
    BackupVault,
    BadPasswordError,
    DecryptionUnavailableError,
    ManifestQuery,
    discover,
    read_backup_info,
)
from conftest import ENCRYPTED_PASSWORD, UDID

# Optional check against a real encrypted backup on this machine. Point
# DEVICE_DOCK_REAL_BACKUP at the backup directory to enable it; the suite skips it
# otherwise so no developer path is published.
REAL_ENCRYPTED_BACKUP = Path(os.environ.get("DEVICE_DOCK_REAL_BACKUP", "/nonexistent"))


def test_read_backup_info(backup_dir: Path) -> None:
    info = read_backup_info(backup_dir)
    assert info.udid == UDID
    assert info.device_name == "Test iPhone"
    assert info.product_type == "iPhone14,8"
    assert info.model_name == "iPhone 14 Plus"
    assert info.product_version == "27.0.1"
    assert info.firmware == "iOS 27.0.1 (24A446)"
    assert info.is_encrypted is False
    assert info.is_full_backup is True
    assert info.applications == 1
    assert info.last_backup is not None


def test_read_backup_info_computes_size(backup_dir: Path) -> None:
    info = read_backup_info(backup_dir, compute_size=True)
    assert info.size_bytes > 0
    assert info.file_count > 5


def test_discover_finds_backups(tmp_path: Path, backup_dir: Path) -> None:
    found = discover([backup_dir.parent])
    assert len(found) == 1
    assert found[0].path == backup_dir
    assert discover([tmp_path / "missing"]) == []


def test_manifest_children_and_search(vault: BackupVault) -> None:
    manifest = vault.manifest()
    assert manifest.total_files() == 10
    roots = manifest.children("HomeDomain")
    names = {node.name for node in roots}
    assert "Library" in names
    library = next(node for node in roots if node.name == "Library")
    assert library.is_dir
    assert library.child_count >= 5

    inner = {node.name for node in manifest.children("HomeDomain", "Library")}
    assert {"AddressBook", "SMS", "prefs.txt", "link"} <= inner

    results = manifest.search("sms.db")
    assert len(results) == 1
    assert results[0].domain == "HomeDomain"
    assert results[0].size > 0

    record = manifest.find("HomeDomain", "Library/prefs.txt")
    assert record is not None and record.is_file
    assert vault.read_file(record) == b"plain text payload\n"


def test_manifest_domains(vault: BackupVault) -> None:
    domains = {stat.domain: stat for stat in vault.manifest().domains_with_sizes()}
    assert "CameraRollDomain" in domains
    assert domains["CameraRollDomain"].file_count == 1
    assert domains["HomeDomain"].total_size > 0


def test_encrypted_backup_requires_password(backup_dir: Path, tmp_path: Path) -> None:
    import plistlib

    encrypted = tmp_path / "enc"
    encrypted.mkdir()
    for name in ("Info.plist", "Status.plist", "Manifest.db"):
        (encrypted / name).write_bytes((backup_dir / name).read_bytes())
    (encrypted / "Manifest.plist").write_bytes(
        plistlib.dumps({"IsEncrypted": True, "Version": "10.0", "BackupKeyBag": b"\x00" * 64})
    )
    info = read_backup_info(encrypted)
    assert info.is_encrypted is True
    vault = BackupVault(info)
    with pytest.raises(BackupLockedError):
        vault.manifest()
    with pytest.raises(BackupLockedError):
        vault.unlock(None)
    vault.close()


@pytest.mark.skipif(not REAL_ENCRYPTED_BACKUP.is_dir(), reason="real encrypted backup not mounted")
def test_real_encrypted_backup_rejects_wrong_password() -> None:
    info = read_backup_info(REAL_ENCRYPTED_BACKUP)
    assert info.is_encrypted is True
    assert info.product_type.startswith("iPhone")
    assert info.applications > 0
    vault = BackupVault(info)
    try:
        with pytest.raises(BadPasswordError):
            vault.unlock("definitely-not-the-password")
        assert vault.unlocked is False
        with pytest.raises(BackupLockedError):
            vault.manifest()
    finally:
        vault.close()


def test_real_backup_manifest_is_not_plain_sqlite() -> None:
    if not REAL_ENCRYPTED_BACKUP.is_dir():
        pytest.skip("real encrypted backup not mounted")
    with pytest.raises(Exception):
        ManifestQuery(REAL_ENCRYPTED_BACKUP / "Manifest.db")


# --- encrypted backups -----------------------------------------------------------
def test_encrypted_backup_unlocks_and_decrypts_with_the_correct_password(
        encrypted_backup_dir: Path) -> None:
    info = read_backup_info(encrypted_backup_dir)
    assert info.is_encrypted is True
    vault = BackupVault(info)
    try:
        vault.unlock(ENCRYPTED_PASSWORD)
        assert vault.unlocked is True
        records = [record for record in vault.manifest().search("", limit=50)
                   if record.is_file and record.relative_path == "Library/prefs.txt"]
        assert records, "the decrypted manifest must list the backup's files"
        assert vault.read_file(records[0]) == b"plain text payload\n"
    finally:
        vault.close()


def test_encrypted_backup_rejects_an_incorrect_password(encrypted_backup_dir: Path) -> None:
    info = read_backup_info(encrypted_backup_dir)
    vault = BackupVault(info)
    try:
        with pytest.raises(BadPasswordError):
            vault.unlock("definitely-not-the-password")
        assert vault.unlocked is False
        with pytest.raises(BackupLockedError):
            vault.manifest()
    finally:
        vault.close()


def test_a_missing_decryption_engine_reports_an_installation_problem(
        encrypted_backup_dir: Path, monkeypatch) -> None:
    """An incomplete installation must never tell the user to run pip."""
    import sys

    monkeypatch.setitem(sys.modules, "iphone_backup_decrypt", None)
    vault = BackupVault(read_backup_info(encrypted_backup_dir))
    try:
        with pytest.raises(DecryptionUnavailableError) as excinfo:
            vault.unlock(ENCRYPTED_PASSWORD)
        message = str(excinfo.value)
        assert "pip install" not in message
        assert "Reinstall the device-dock package" in message
        assert "no Python packages need to be installed" in message
    finally:
        vault.close()
