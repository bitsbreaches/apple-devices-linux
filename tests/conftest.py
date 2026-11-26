# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Test fixtures: build synthetic iTunes/Finder backup folders.

The fixture writes exactly the structures Apple uses: the four top-level plists, a
``Files`` manifest table with binary-plist payload metadata, and payload files stored in
``<fileID[:2]>/<fileID>``. It also injects a traversal record and a symlink record so the
safety paths are exercised.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

UDID = "0000811000000000000000AA"
DEVICE_NAME = "Test iPhone"


def _file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


def _write_payload(root: Path, domain: str, relative_path: str, data: bytes,
                   *, mtime: float | None = None, symlink: str | None = None) -> str:
    file_id = _file_id(domain, relative_path)
    target = root / file_id[:2] / file_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return file_id


def _make_sqlite(path: Path, statements: list[str], rows: list[tuple[str, tuple]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.commit()
    conn.close()
    return path.read_bytes()


def _insert(conn: sqlite3.Connection, file_id: str, domain: str, relative_path: str,
            flags: int, meta: dict) -> None:
    conn.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
        (file_id, domain, relative_path, flags, plistlib.dumps(meta)),
    )


def build_backup(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 11, 11, 19, 55, 0)

    (root / "Info.plist").write_bytes(plistlib.dumps({
        "Device Name": DEVICE_NAME,
        "Display Name": DEVICE_NAME,
        "Product Name": "iPhone14,8",
        "Product Type": "iPhone14,8",
        "Product Version": "27.0.1",
        "Build Version": "24A446",
        "Serial Number": "TESTSERIAL01",
        "IMEI": "000000000000000",
        "Phone Number": "+61 400 000 000",
        "Target Identifier": UDID,
        "Unique Identifier": UDID,
        "Target Type": "Device",
        "Last Backup Date": now,
        "Applications": {"com.example.app": {"CFBundleIdentifier": "com.example.app"}},
    }))
    (root / "Manifest.plist").write_bytes(plistlib.dumps({
        "IsEncrypted": False,
        "Version": "10.0",
        "Date": now,
        "WasPasscodeSet": True,
        "Lockdown": {},
    }))
    (root / "Status.plist").write_bytes(plistlib.dumps({
        "Version": "3.3",
        "Date": now,
        "IsFullBackup": True,
        "UUID": "test-uuid",
    }))

    db_path = root / "Manifest.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, "
        "flags INTEGER, file BLOB)"
    )
    conn.execute("CREATE INDEX FilesDomainIdx ON Files(domain)")

    work = root.parent / "work"
    work.mkdir(parents=True, exist_ok=True)

    addressbook = _make_sqlite(
        work / "AddressBook.sqlitedb",
        [
            "CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY, First TEXT, Last TEXT, "
            "Organization TEXT, Department TEXT, JobTitle TEXT)",
            "CREATE TABLE ABMultiValue (record_id INTEGER, property INTEGER, value TEXT)",
        ],
        [
            ("INSERT INTO ABPerson VALUES (1,'Ada','Lovelace','Analytical','Engines','Engineer')", ()),
            ("INSERT INTO ABPerson VALUES (2,'Alan','Turing',NULL,NULL,NULL)", ()),
            ("INSERT INTO ABMultiValue VALUES (1,3,'+61 400 111 222')", ()),
            ("INSERT INTO ABMultiValue VALUES (1,4,'ada@example.com')", ()),
        ],
    )
    file_id = _write_payload(root, "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb", addressbook)
    _insert(conn, file_id, "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb", 1,
            {"size": len(addressbook), "mtime": now.timestamp(), "mode": 33188,
             "protection_class": 3, "relativePath": "Library/AddressBook/AddressBook.sqlitedb",
             "domain": "HomeDomain", "flags": 1,
             "digest": hashlib.sha1(addressbook).digest()})

    sms = _make_sqlite(
        work / "sms.db",
        [
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, "
            "is_from_me INTEGER, service TEXT, handle_id INTEGER)",
            "CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)",
        ],
        [
            ("INSERT INTO handle VALUES (1,'+61 400 333 444')", ()),
            ("INSERT INTO message VALUES (1,'Hello there',700000000000000000,0,'iMessage',1)", ()),
            ("INSERT INTO message VALUES (2,'Hi back',700000060000000000,1,'iMessage',1)", ()),
        ],
    )
    file_id = _write_payload(root, "HomeDomain", "Library/SMS/sms.db", sms)
    _insert(conn, file_id, "HomeDomain", "Library/SMS/sms.db", 1,
            {"size": len(sms), "mtime": now.timestamp(), "protection_class": 3,
             "relativePath": "Library/SMS/sms.db", "domain": "HomeDomain", "flags": 1})

    calls = _make_sqlite(
        work / "CallHistory.storedata",
        [
            "CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZADDRESS TEXT, ZDATE REAL, "
            "ZDURATION REAL, ZORIGINATED INTEGER, ZANSWERED INTEGER)",
        ],
        [("INSERT INTO ZCALLRECORD VALUES (1,'+61 400 555 666',699000000,42,1,1)", ())],
    )
    file_id = _write_payload(root, "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata", calls)
    _insert(conn, file_id, "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata", 1,
            {"size": len(calls), "protection_class": 3,
             "relativePath": "Library/CallHistoryDB/CallHistory.storedata", "domain": "HomeDomain",
             "flags": 1})

    calendar = _make_sqlite(
        work / "Calendar.sqlitedb",
        [
            "CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT, "
            "start_date REAL, end_date REAL, all_day INTEGER)",
        ],
        [("INSERT INTO CalendarItem VALUES (1,'Dentist',699100000,699103600,0)", ())],
    )
    file_id = _write_payload(root, "HomeDomain", "Library/Calendar/Calendar.sqlitedb", calendar)
    _insert(conn, file_id, "HomeDomain", "Library/Calendar/Calendar.sqlitedb", 1,
            {"size": len(calendar), "protection_class": 3,
             "relativePath": "Library/Calendar/Calendar.sqlitedb", "domain": "HomeDomain", "flags": 1})

    history = _make_sqlite(
        work / "History.db",
        [
            "CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT, visit_count INTEGER)",
            "CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER, visit_time REAL)",
        ],
        [
            ("INSERT INTO history_items VALUES (1,'https://example.com/',5)", ()),
            ("INSERT INTO history_visits VALUES (1,1,699200000)", ()),
        ],
    )
    file_id = _write_payload(root, "HomeDomain", "Library/Safari/History.db", history)
    _insert(conn, file_id, "HomeDomain", "Library/Safari/History.db", 1,
            {"size": len(history), "protection_class": 3,
             "relativePath": "Library/Safari/History.db", "domain": "HomeDomain", "flags": 1})

    notestore = _make_sqlite(
        work / "NoteStore.sqlite",
        ["CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZSNIPPET TEXT, "
         "ZMODIFICATIONDATE1 REAL)"],
        [("INSERT INTO ZNOTE VALUES (1,'Shopping','milk, eggs',699300000)", ())],
    )
    file_id = _write_payload(root, "AppDomain-com.apple.mobilenotes",
                             "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite", notestore)
    _insert(conn, file_id, "AppDomain-com.apple.mobilenotes",
            "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite", 1,
            {"size": len(notestore), "protection_class": 3,
             "relativePath": "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite",
             "domain": "AppDomain-com.apple.mobilenotes", "flags": 1})

    photo = b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 32
    file_id = _write_payload(root, "CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG", photo)
    _insert(conn, file_id, "CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG", 1,
            {"size": len(photo), "mtime": now.timestamp(), "protection_class": 3,
             "relativePath": "Media/DCIM/100APPLE/IMG_0001.JPG", "domain": "CameraRollDomain",
             "flags": 1})

    note = b"plain text payload\n"
    file_id = _write_payload(root, "HomeDomain", "Library/prefs.txt", note)
    _insert(conn, file_id, "HomeDomain", "Library/prefs.txt", 1,
            {"size": len(note), "protection_class": 3, "relativePath": "Library/prefs.txt",
             "domain": "HomeDomain", "flags": 1})

    # Safety fixtures: traversal path and symlink record.
    evil = b"should never be written outside the export root\n"
    file_id = _write_payload(root, "HomeDomain", "../../etc/evil.txt", evil)
    _insert(conn, file_id, "HomeDomain", "../../etc/evil.txt", 1,
            {"size": len(evil), "protection_class": 3, "relativePath": "../../etc/evil.txt",
             "domain": "HomeDomain", "flags": 1})

    file_id = _write_payload(root, "HomeDomain", "Library/link", b"")
    _insert(conn, file_id, "HomeDomain", "Library/link", 4,
            {"size": 0, "relativePath": "Library/link", "domain": "HomeDomain", "flags": 4,
             "Target": "/private/var/mobile/secret"})

    conn.commit()
    conn.close()
    return root


# --- encrypted backup fixture ------------------------------------------------
# A real iOS encrypted backup uses the Apple keybag format: the passphrase-derived key
# unwraps a per-class key, the class key unwraps the Manifest.db key and every file key.
# These helpers reproduce that format exactly so unlocking is exercised against the real
# ``iphone_backup_decrypt`` implementation rather than a stub.
ENCRYPTED_PASSWORD = "correct horse battery staple"
_SALT = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14"
_DPSL = b"\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f\x20\x21\x22\x23\x24\x25\x26\x27\x28"
_DPIC = 1000
_ITER = 1000
_CLASS_KEYS = {3: bytes(range(32, 64)), 4: bytes(range(64, 96))}
_MANIFEST_KEY = bytes(range(96, 128))
_FILE_KEY = bytes(range(128, 160))


def _tlv(tag: bytes, data: bytes) -> bytes:
    import struct

    return tag + struct.pack(">L", len(data)) + data


def _aes_cbc_encrypt(key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padding = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return encryptor.update(data + bytes([padding]) * padding) + encryptor.finalize()


def _keybag(password: str) -> bytes:
    """Build a keybag whose class keys are wrapped with the passphrase-derived key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.keywrap import aes_key_wrap
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    round_one = PBKDF2HMAC(hashes.SHA256(), 32, _DPSL, _DPIC).derive(password.encode())
    passphrase_key = PBKDF2HMAC(hashes.SHA1(), 32, _SALT, _ITER).derive(round_one)
    blocks = [
        _tlv(b"VERS", (4).to_bytes(4, "big")),
        _tlv(b"TYPE", (1).to_bytes(4, "big")),
        _tlv(b"UUID", b"K" * 16),
        _tlv(b"HMCK", b"\x00" * 40),
        _tlv(b"WRAP", (2).to_bytes(4, "big")),
        _tlv(b"SALT", _SALT),
        _tlv(b"ITER", _ITER.to_bytes(4, "big")),
        _tlv(b"DPIC", _DPIC.to_bytes(4, "big")),
        _tlv(b"DPSL", _DPSL),
    ]
    for protection_class, class_key in _CLASS_KEYS.items():
        blocks.extend([
            _tlv(b"UUID", bytes([protection_class]) * 16),
            _tlv(b"CLAS", protection_class.to_bytes(4, "big")),
            _tlv(b"WRAP", (2).to_bytes(4, "big")),
            _tlv(b"KTYP", (0).to_bytes(4, "big")),
            _tlv(b"WPKY", aes_key_wrap(passphrase_key, class_key)),
        ])
    return b"".join(blocks)


def build_encrypted_backup(root: Path, password: str = ENCRYPTED_PASSWORD) -> Path:
    """Create an encrypted backup with real keybag, manifest and per-file encryption."""
    import struct

    from cryptography.hazmat.primitives.keywrap import aes_key_wrap

    build_backup(root)
    wrapped_file_key = struct.pack("<I", 3) + aes_key_wrap(_CLASS_KEYS[3], _FILE_KEY)

    conn = sqlite3.connect(root / "Manifest.db")
    try:
        rows = conn.execute("SELECT fileID, file FROM Files WHERE flags = 1").fetchall()
        for file_id, blob in rows:
            meta = plistlib.loads(blob)
            payload = root / file_id[:2] / file_id
            data = payload.read_bytes()
            if not data:
                continue
            meta["encryption_key"] = wrapped_file_key
            meta["size"] = len(data)
            conn.execute("UPDATE Files SET file = ? WHERE fileID = ?",
                         (plistlib.dumps(meta), file_id))
            payload.write_bytes(_aes_cbc_encrypt(_FILE_KEY, data))
        conn.commit()
    finally:
        conn.close()

    manifest_path = root / "Manifest.plist"
    manifest = plistlib.loads(manifest_path.read_bytes())
    manifest["IsEncrypted"] = True
    manifest["BackupKeyBag"] = _keybag(password)
    manifest["ManifestKey"] = struct.pack("<I", 4) + aes_key_wrap(_CLASS_KEYS[4],
                                                                  _MANIFEST_KEY)
    manifest_path.write_bytes(plistlib.dumps(manifest))

    database = root / "Manifest.db"
    database.write_bytes(_aes_cbc_encrypt(_MANIFEST_KEY, database.read_bytes()))
    return root


@pytest.fixture()
def backup_dir(tmp_path: Path) -> Path:
    return build_backup(tmp_path / "backup" / UDID)


@pytest.fixture()
def encrypted_backup_dir(tmp_path: Path) -> Path:
    return build_encrypted_backup(tmp_path / "encrypted" / UDID)


@pytest.fixture()
def vault(backup_dir: Path):
    from devicedock.backup import BackupVault, read_backup_info

    instance = BackupVault(read_backup_info(backup_dir))
    instance.unlock()
    yield instance
    instance.close()
