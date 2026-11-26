# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Payload decryption wiring and post-backup integrity validation."""

from __future__ import annotations

import plistlib
import sqlite3
from pathlib import Path

import pytest

from devicedock.backup.integrity import validate_backup
from devicedock.backup.vault import (
    PAYLOAD_EMPTY,
    PAYLOAD_MISSING,
    PAYLOAD_NOT_A_FILE,
    PAYLOAD_PRESENT,
    BackupVault,
    PayloadMissingError,
)
from devicedock.models import BackupInfo, FileRecord

UDID = "0000811000000000000000AA"
FILE_KEY = bytes(range(32))


def _encrypt(data: bytes, key: bytes = FILE_KEY) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padding = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return encryptor.update(data + bytes([padding]) * padding) + encryptor.finalize()


class _FakeKeybag:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bytes]] = []
        self.key = FILE_KEY

    def unwrap_key_for_class(self, protection_class, wrapped_file_key):
        self.calls.append((protection_class, wrapped_file_key))
        if len(wrapped_file_key) != 40:
            raise ValueError("Invalid wrapped file key length!")
        return self.key


class _FakeEngine:
    def __init__(self) -> None:
        self.keybag = _FakeKeybag()


def _vault(tmp_path: Path) -> tuple[BackupVault, _FakeEngine]:
    info = BackupInfo(path=tmp_path, udid=UDID, is_encrypted=True)
    vault = BackupVault(info)
    engine = _FakeEngine()
    vault._manifest = object()          # already unlocked, as far as this test cares
    vault._engine = engine
    return vault, engine


def _write_payload(root: Path, file_id: str, data: bytes) -> None:
    target = root / file_id[:2] / file_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_encrypted_payload_is_decrypted_using_the_manifest_key(tmp_path: Path) -> None:
    plaintext = b"SQLite format 3\x00contacts payload"
    file_id = "a" * 40
    _write_payload(tmp_path, file_id, _encrypt(plaintext))
    wrapped = b"\x03\x00\x00\x00" + b"W" * 40

    vault, engine = _vault(tmp_path)
    record = FileRecord(file_id=file_id, domain="HomeDomain",
                        relative_path="Library/AddressBook/AddressBook.sqlitedb",
                        flags=1, size=len(plaintext), protection_class=3,
                        encryption_key=wrapped)

    assert vault.read_file(record) == plaintext
    assert engine.keybag.calls == [(3, b"W" * 40)]


def test_a_payload_without_a_key_is_returned_unsliced(tmp_path: Path) -> None:
    """Some entries inside an encrypted backup are stored unencrypted."""
    file_id = "b" * 40
    _write_payload(tmp_path, file_id, b"plain")
    vault, engine = _vault(tmp_path)
    record = FileRecord(file_id=file_id, domain="HomeDomain", relative_path="x", flags=1)

    assert vault.read_file(record) == b"plain"
    assert engine.keybag.calls == []


def test_a_missing_payload_raises_instead_of_reading_as_empty(tmp_path: Path) -> None:
    """A pruned payload must never be indistinguishable from a zero-byte file."""
    vault, _ = _vault(tmp_path)
    record = FileRecord(file_id="c" * 40, domain="HomeDomain", relative_path="gone",
                        flags=1, encryption_key=b"\x03\x00\x00\x00" + b"W" * 40)

    with pytest.raises(PayloadMissingError) as excinfo:
        vault.read_file(record)
    assert "gone" in str(excinfo.value)
    assert "not present" in str(excinfo.value)


def test_a_genuinely_empty_payload_reads_as_empty(tmp_path: Path) -> None:
    file_id = "d" * 40
    _write_payload(tmp_path, file_id, b"")
    vault, engine = _vault(tmp_path)
    record = FileRecord(file_id=file_id, domain="HomeDomain", relative_path="empty.txt",
                        flags=1, size=0)

    assert vault.read_file(record) == b""
    assert engine.keybag.calls == []


def test_payload_state_distinguishes_present_empty_and_missing(tmp_path: Path) -> None:
    present_id, empty_id, missing_id = "1" * 40, "2" * 40, "3" * 40
    _write_payload(tmp_path, present_id, b"data")
    _write_payload(tmp_path, empty_id, b"")
    vault, _ = _vault(tmp_path)

    def record(file_id: str, flags: int = 1) -> FileRecord:
        return FileRecord(file_id=file_id, domain="HomeDomain", relative_path="f", flags=flags)

    assert vault.payload_state(record(present_id)) == PAYLOAD_PRESENT
    assert vault.payload_state(record(empty_id)) == PAYLOAD_EMPTY
    assert vault.payload_state(record(missing_id)) == PAYLOAD_MISSING
    assert vault.payload_state(record(present_id, flags=2)) == PAYLOAD_NOT_A_FILE


def test_copy_file_raises_payload_missing(tmp_path: Path) -> None:
    vault, _ = _vault(tmp_path)
    record = FileRecord(file_id="e" * 40, domain="HomeDomain", relative_path="gone",
                        flags=1)

    with pytest.raises(PayloadMissingError):
        vault.copy_file(record, tmp_path / "out.bin")


def test_export_reports_missing_payloads_separately_from_errors(tmp_path: Path) -> None:
    from devicedock.backup.extract import export_records

    _write_payload(tmp_path / "backup", "f" * 40, b"payload")
    vault, _ = _vault(tmp_path / "backup")
    present = FileRecord(file_id="f" * 40, domain="HomeDomain", relative_path="kept.txt",
                         flags=1, size=7)
    absent = FileRecord(file_id="0" * 40, domain="HomeDomain", relative_path="pruned.txt",
                        flags=1, size=7)

    result = export_records(vault, [present, absent], tmp_path / "export")

    assert len(result.exported) == 1
    assert result.missing == ["HomeDomain/pruned.txt"]
    assert result.errors == []
    assert "not in this backup" in result.summary


# ------------------------------------------------------------------ integrity validation
def _plain_snapshot(root: Path, *, rows: int = 1, payload: int = 4096) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "iPhone"}))
    (root / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    (root / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    connection = sqlite3.connect(root / "Manifest.db")
    connection.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                       "relativePath TEXT, flags INTEGER, file BLOB)")
    for index in range(rows):
        connection.execute("INSERT INTO Files VALUES (?, 'HomeDomain', ?, 1, ?)",
                           (f"{index:040x}", f"f{index}", plistlib.dumps({})))
    connection.commit()
    connection.close()
    return root


def test_a_complete_plaintext_backup_passes(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup")

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert report.ok, report.problems
    assert report.encrypted is False


@pytest.mark.parametrize("missing", ["Info.plist", "Manifest.plist", "Status.plist"])
def test_missing_metadata_fails(tmp_path: Path, missing: str) -> None:
    root = _plain_snapshot(tmp_path / "backup")
    (root / missing).unlink()

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert not report.ok
    assert any(missing in problem for problem in report.problems)


def test_a_corrupt_manifest_plist_fails(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup")
    (root / "Manifest.plist").write_bytes(b"not a plist")

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert not report.ok
    assert any("Manifest.plist" in problem for problem in report.problems)


def test_an_unreadable_manifest_db_fails(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup")
    (root / "Manifest.db").write_bytes(b"this is not sqlite")

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert not report.ok
    assert any("Manifest.db" in problem for problem in report.problems)


def test_an_empty_manifest_db_fails(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup", rows=0)

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert not report.ok
    assert any("no file records" in problem for problem in report.problems)


def test_metadata_only_backup_fails(tmp_path: Path) -> None:
    """The reported failure: 'transferred' data that left no payload."""
    root = _plain_snapshot(tmp_path / "backup")

    report = validate_backup(root, payload_bytes=0, file_count=4)

    assert not report.ok
    assert any("metadata only" in problem for problem in report.problems)


def test_an_encrypted_backup_is_validated_without_a_sqlite_manifest(tmp_path: Path) -> None:
    """An encrypted backup's Manifest.db is deliberately not a database yet."""
    root = tmp_path / "backup"
    root.mkdir()
    (root / "Info.plist").write_bytes(plistlib.dumps({}))
    (root / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    (root / "Manifest.plist").write_bytes(plistlib.dumps({
        "IsEncrypted": True, "ManifestKey": b"\x04\x00\x00\x00" + b"k" * 40,
        "BackupKeyBag": b"bplist00-keybag"}))
    (root / "Manifest.db").write_bytes(b"\x00" * 128)

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert report.ok, report.problems
    assert report.encrypted is True


@pytest.mark.parametrize("field", ["ManifestKey", "BackupKeyBag"])
def test_an_encrypted_backup_without_its_key_material_fails(tmp_path: Path,
                                                            field: str) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    (root / "Info.plist").write_bytes(plistlib.dumps({}))
    (root / "Status.plist").write_bytes(plistlib.dumps({}))
    material = {"IsEncrypted": True, "ManifestKey": b"k" * 44, "BackupKeyBag": b"bag"}
    del material[field]
    (root / "Manifest.plist").write_bytes(plistlib.dumps(material))
    (root / "Manifest.db").write_bytes(b"\x00" * 128)

    report = validate_backup(root, payload_bytes=4096, file_count=1)

    assert not report.ok
    assert report.problems


def test_a_missing_backup_directory_fails(tmp_path: Path) -> None:
    report = validate_backup(tmp_path / "nope", payload_bytes=1, file_count=1)

    assert not report.ok
    assert "does not exist" in report.describe()


def test_describe_lists_every_problem(tmp_path: Path) -> None:
    report = validate_backup(tmp_path / "nope", payload_bytes=0, file_count=0)

    assert "not usable" in report.describe()
    assert report.describe().count("•") >= 1


def test_integrity_warns_when_manifest_entries_have_no_payload(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup", rows=2)
    connection = sqlite3.connect(root / "Manifest.db")
    connection.execute("UPDATE Files SET file = ?", (plistlib.dumps({"Size": 128}),))
    connection.commit()
    connection.close()

    report = validate_backup(root, payload_bytes=4096, file_count=2)

    assert report.ok, report.problems
    assert report.warnings, "a missing payload file is a warning, not silently ignored"
    assert any("no payload file" in warning for warning in report.warnings)


def test_integrity_accepts_present_payloads(tmp_path: Path) -> None:
    root = _plain_snapshot(tmp_path / "backup", rows=2)
    for index in range(2):
        file_id = f"{index:040x}"
        target = root / file_id[:2] / file_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 128)
    connection = sqlite3.connect(root / "Manifest.db")
    connection.execute("UPDATE Files SET file = ?", (plistlib.dumps({"Size": 128}),))
    connection.commit()
    connection.close()

    report = validate_backup(root, payload_bytes=4096, file_count=2)

    assert report.ok, report.problems
    assert not report.warnings, report.warnings
