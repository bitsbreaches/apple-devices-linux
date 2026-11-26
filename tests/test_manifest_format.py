# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Regression tests for Apple's real manifest ``file`` blob format.

The application used to read only lowercase flat plist keys, which no real backup contains:
Apple writes an ``NSKeyedArchiver`` archive with an ``MBFile`` object whose fields are
capitalised. Every real file therefore reported a size of 0 and had no per-file key, so
encrypted payloads could not be decrypted and the browser showed no usable data.
"""

from __future__ import annotations

import plistlib
import sqlite3
from pathlib import Path

import pytest

from devicedock.backup.manifest import ManifestQuery, parse_file_blob, record_from_row
from devicedock.backup.vault import split_file_key
from devicedock.models import FileRecord

UDID = "0000811000000000000000AA"


def nskeyed_blob(*, size: int = 4096, mtime: int = 1794960449, mode: int = 0o100644,
                 protection_class: int | None = 3, encryption_key: bytes | None = None,
                 relative_path: str = "Library/thing.db", use_nsdate: bool = False) -> bytes:
    """Build an ``NSKeyedArchiver`` ``MBFile`` blob shaped like the one iOS writes."""
    objects: list = ["$null", None]          # index 1 is the root, filled below
    root: dict = {
        "$class": plistlib.UID(0),           # replaced after the class entries are appended
        "RelativePath": relative_path,
        "Size": size,
        "Mode": mode,
        "Flags": 0,
        "UserID": 501,
        "GroupID": 501,
        "InodeNumber": 213341,
    }
    if use_nsdate:
        objects.append({"$class": plistlib.UID(0), "NS.time": mtime - 978307200})
        root["LastModified"] = plistlib.UID(len(objects) - 1)
    else:
        root["LastModified"] = mtime
    if protection_class is not None:
        root["ProtectionClass"] = protection_class
    if encryption_key is not None:
        objects.append({"$class": plistlib.UID(0), "NS.data": encryption_key})
        root["EncryptionKey"] = plistlib.UID(len(objects) - 1)

    file_class = len(objects)
    objects.append({"$classes": ["MBFile", "NSObject"], "$classname": "MBFile"})
    data_class = len(objects)
    objects.append({"$classes": ["NSMutableData", "NSData", "NSObject"],
                    "$classname": "NSMutableData"})
    objects[1] = root
    for entry in objects:
        if isinstance(entry, dict) and entry.get("$class") == plistlib.UID(0):
            entry["$class"] = plistlib.UID(file_class if entry is root else data_class)

    archive = {"$archiver": "NSKeyedArchiver", "$version": 100000,
               "$top": {"root": plistlib.UID(1)}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def test_archive_metadata_is_decoded() -> None:
    """The exact defect: a real blob must yield a real size, not 0."""
    meta = parse_file_blob(nskeyed_blob(size=4502040))

    assert meta["size"] == 4502040
    assert meta["mode"] == 0o100644
    assert meta["protection_class"] == 3
    assert meta["mtime"] == 1794960449


def test_archive_encryption_key_is_decoded_to_bytes() -> None:
    key = b"\x03\x00\x00\x00" + bytes(range(40))
    meta = parse_file_blob(nskeyed_blob(encryption_key=key))

    assert meta["encryption_key"] == key
    assert len(meta["encryption_key"]) == 44


def test_archive_nsdate_is_converted_to_unix_time() -> None:
    meta = parse_file_blob(nskeyed_blob(mtime=1794960449, use_nsdate=True))

    assert meta["mtime"] == pytest.approx(1794960449, abs=1)


def test_flat_capitalised_plist_is_still_accepted() -> None:
    """Older backups use the same field names in a flat plist."""
    blob = plistlib.dumps({"Size": 12, "LastModified": 1700000000, "ProtectionClass": 3,
                           "Mode": 0o100600, "RelativePath": "x"})
    meta = parse_file_blob(blob)

    assert meta["size"] == 12
    assert meta["mtime"] == 1700000000
    assert meta["mode"] == 0o100600


def test_legacy_lowercase_plist_is_still_accepted() -> None:
    meta = parse_file_blob(plistlib.dumps({"size": 7, "mtime": 1.5}))

    assert meta["size"] == 7
    assert meta["mtime"] == 1.5


@pytest.mark.parametrize("blob", [b"not a plist", plistlib.dumps([1, 2, 3]), b"",
                                  plistlib.dumps({"$archiver": "NSKeyedArchiver"})])
def test_unusable_blobs_degrade_to_no_metadata(blob: bytes) -> None:
    assert parse_file_blob(blob) == {}


def test_record_from_real_blob_has_a_size_and_a_key() -> None:
    key = b"\x03\x00\x00\x00" + b"k" * 40
    row = ("a" * 40, "CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG", 1,
           nskeyed_blob(size=1382847, encryption_key=key))

    record = record_from_row(row)

    assert isinstance(record, FileRecord)
    assert record.size == 1382847
    assert record.encryption_key == key
    assert record.protection_class == 3
    assert record.size_text != "0 B"


def test_missing_manifest_size_stays_unknown_not_zero() -> None:
    record = record_from_row(("a" * 40, "HomeDomain", "Documents/notes", 1, b""))

    assert record.size is None
    assert record.size_text == "—"


def test_manifest_query_reports_real_sizes(tmp_path: Path) -> None:
    db = tmp_path / "Manifest.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                       "relativePath TEXT, flags INTEGER, file BLOB)")
    for index, size in enumerate((1024, 2048)):
        connection.execute(
            "INSERT INTO Files VALUES (?, 'CameraRollDomain', ?, 1, ?)",
            (f"{index:040x}", f"Media/DCIM/IMG_{index}.JPG", nskeyed_blob(size=size)))
    connection.commit()
    connection.close()

    manifest = ManifestQuery(db)
    records = manifest.files_in("CameraRollDomain", "Media/DCIM")

    assert sorted(record.size for record in records) == [1024, 2048]


def test_manifest_iter_files_uses_bounded_batches_without_truncation(tmp_path: Path) -> None:
    db = tmp_path / "Manifest.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                       "relativePath TEXT, flags INTEGER, file BLOB)")
    for index in range(7):
        connection.execute(
            "INSERT INTO Files VALUES (?, 'HomeDomain', ?, 1, ?)",
            (f"{index:040x}", f"Documents/{index}.txt", nskeyed_blob(size=index)),
        )
    connection.commit()
    connection.close()

    manifest = ManifestQuery(db)
    records = list(manifest.iter_files(batch_size=2))

    assert len(records) == 7
    assert [record.file_id for record in records] == sorted(record.file_id for record in records)


# ------------------------------------------------------------------ per-file key layout
def test_split_file_key_strips_the_protection_class_prefix() -> None:
    wrapped = bytes(range(40))
    protection_class, key = split_file_key(b"\x03\x00\x00\x00" + wrapped, None)

    assert protection_class == 3
    assert key == wrapped


def test_split_file_key_accepts_a_bare_wrapped_key() -> None:
    wrapped = b"w" * 40
    assert split_file_key(wrapped, 4) == (4, wrapped)


def test_split_file_key_rejects_a_key_without_a_protection_class() -> None:
    from devicedock.backup.vault import BackupLockedError

    with pytest.raises(BackupLockedError):
        split_file_key(b"short", None)
