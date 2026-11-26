# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Notes category against real-world NoteStore schema variants.

Apple has changed the ``ZNOTE`` columns between releases. The parser used to select
``ZTITLE1, ZSNIPPET, ZMODIFICATIONDATE1`` unconditionally, so a store without ``ZSNIPPET``
failed the whole category. These tests pin the schema-adaptive behaviour and the body
decoder.
"""

from __future__ import annotations

import gzip
import hashlib
import plistlib
import sqlite3
from pathlib import Path

import pytest

from devicedock.backup import BackupVault, load_category, read_backup_info
from devicedock.backup.categories import _decode_note_body

DOMAIN = "AppDomainGroup-group.com.apple.notes"
PATH = "NoteStore.sqlite"


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def note_protobuf(text: str) -> bytes:
    """A gzipped body shaped like Apple's: Document → Note → note_text."""
    note = _field(2, text.encode("utf-8"))
    document = _field(3, note)
    return gzip.compress(document)


def _backup_with(notestore: bytes, tmp_path: Path) -> BackupVault:
    root = tmp_path / "backup" / "0000811000000000000000AA"
    root.mkdir(parents=True)
    file_id = hashlib.sha1(f"{DOMAIN}-{PATH}".encode()).hexdigest()
    payload = root / file_id[:2] / file_id
    payload.parent.mkdir(parents=True)
    payload.write_bytes(notestore)

    (root / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "Test"}))
    (root / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    (root / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    connection = sqlite3.connect(root / "Manifest.db")
    connection.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                       "relativePath TEXT, flags INTEGER, file BLOB)")
    connection.execute(
        "INSERT INTO Files VALUES (?, ?, ?, 1, ?)",
        (file_id, DOMAIN, PATH,
         plistlib.dumps({"size": len(notestore), "protection_class": 3})),
    )
    connection.commit()
    connection.close()

    vault = BackupVault(read_backup_info(root))
    vault.unlock()
    return vault


def _store(tmp_path: Path, statements: list[str], rows: list[tuple]) -> bytes:
    path = tmp_path / "NoteStore.sqlite"
    connection = sqlite3.connect(path)
    for statement in statements:
        connection.execute(statement)
    for sql, params in rows:
        connection.execute(sql, params)
    connection.commit()
    connection.close()
    return path.read_bytes()


def test_a_store_without_zsnippet_still_lists_notes(tmp_path: Path) -> None:
    """The regression: a missing ZSNIPPET column must not fail the category."""
    data = _store(tmp_path, [
        "CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, "
        "ZMODIFICATIONDATE1 REAL)",
    ], [("INSERT INTO ZNOTE VALUES (1,'Groceries',699300000)", ())])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert result.available, result.error
    assert result.rows[0][0] == "Groceries"


def test_a_store_using_alternative_date_columns_is_supported(tmp_path: Path) -> None:
    data = _store(tmp_path, [
        "CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, "
        "ZMODIFICATIONDATE REAL)",
    ], [("INSERT INTO ZNOTE VALUES (1,'Plan',699300000)", ())])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert result.available, result.error
    assert result.rows[0][0] == "Plan"
    assert result.rows[0][1] != "—"


def test_the_stored_snippet_is_preferred_when_present(tmp_path: Path) -> None:
    data = _store(tmp_path, [
        "CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZSNIPPET TEXT, "
        "ZMODIFICATIONDATE1 REAL)",
    ], [("INSERT INTO ZNOTE VALUES (1,'Shopping','milk, eggs',699300000)", ())])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert result.rows[0][2] == "milk, eggs"


def test_the_body_is_decoded_from_the_note_data_table(tmp_path: Path) -> None:
    data = _store(tmp_path, [
        "CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZNOTEDATA INTEGER, "
        "ZMODIFICATIONDATE1 REAL)",
        "CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB, ZNOTE INTEGER)",
    ], [
        ("INSERT INTO ZNOTE VALUES (7,NULL,1,699300000)", ()),
        ("INSERT INTO ZICNOTEDATA VALUES (1,?,7)", (note_protobuf("Buy oat milk today"),)),
    ])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert result.available, result.error
    assert result.rows[0][0] == "Buy oat milk today"
    assert "Buy oat milk today" in result.rows[0][2]


def test_an_unreadable_body_does_not_hide_the_note(tmp_path: Path) -> None:
    data = _store(tmp_path, [
        "CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, ZTITLE1 TEXT, ZNOTEDATA INTEGER, "
        "ZMODIFICATIONDATE1 REAL)",
        "CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB, ZNOTE INTEGER)",
    ], [
        ("INSERT INTO ZNOTE VALUES (7,'Kept',1,699300000)", ()),
        ("INSERT INTO ZICNOTEDATA VALUES (1,?,7)", (b"\x1f\x8bnot really gzip",)),
    ])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert result.available, result.error
    assert result.rows[0][0] == "Kept"


def test_a_store_without_znote_reports_unavailable(tmp_path: Path) -> None:
    data = _store(tmp_path, ["CREATE TABLE Other (Z_PK INTEGER PRIMARY KEY)"], [])
    vault = _backup_with(data, tmp_path)

    result = load_category("notes", vault)

    assert not result.available
    assert "ZNOTE" in (result.error or "")


@pytest.mark.parametrize("blob", [b"", b"plain text", b"\x1f\x8b", gzip.compress(b"")])
def test_body_decoder_never_raises(blob: bytes) -> None:
    assert isinstance(_decode_note_body(blob), str)


def test_body_decoder_ignores_binary_noise() -> None:
    binary = bytes(range(256)) * 4
    assert _decode_note_body(gzip.compress(binary)) == ""
