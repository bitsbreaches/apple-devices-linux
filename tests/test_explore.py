# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from devicedock.backup import (
    USER_FILE_CATEGORIES,
    UnsafePathError,
    export_category,
    export_records,
    load_category,
    preview_text,
    user_file_categories,
    write_inventory_report,
)
from devicedock.backup.extract import destination_for, validated_relative
from devicedock.backup.vault import BackupVault


def test_children_and_read(vault: BackupVault) -> None:
    record = vault.manifest().find("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG")
    assert record is not None
    assert vault.read_file(record).startswith(b"\xff\xd8\xff")


def test_export_preserves_structure(vault: BackupVault, tmp_path: Path) -> None:
    manifest = vault.manifest()
    records = [
        manifest.find("HomeDomain", "Library/prefs.txt"),
        manifest.find("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG"),
    ]
    result = export_records(vault, [r for r in records if r], tmp_path / "out")
    assert len(result.exported) == 2
    assert (tmp_path / "out" / "HomeDomain" / "Library" / "prefs.txt").read_bytes() == b"plain text payload\n"
    assert (tmp_path / "out" / "CameraRollDomain" / "Media" / "DCIM" / "100APPLE" / "IMG_0001.JPG").exists()
    assert result.errors == []


def test_export_rejects_traversal(vault: BackupVault, tmp_path: Path) -> None:
    record = vault.manifest().find("HomeDomain", "../../etc/evil.txt")
    assert record is not None
    with pytest.raises(UnsafePathError):
        validated_relative(record)
    result = export_records(vault, [record], tmp_path / "out")
    assert result.exported == []
    assert result.errors and "traversal" in result.errors[0]
    assert not (tmp_path / "etc" / "evil.txt").exists()


def test_export_writes_symlink_note(vault: BackupVault, tmp_path: Path) -> None:
    record = vault.manifest().find("HomeDomain", "Library/link")
    assert record is not None
    result = export_records(vault, [record], tmp_path / "out")
    assert len(result.exported) == 1
    note = result.exported[0]
    assert note.name.endswith(".symlink.txt")
    assert "secret" in note.read_text()
    assert not note.is_symlink()


def test_preview_text(vault: BackupVault) -> None:
    record = vault.manifest().find("HomeDomain", "Library/prefs.txt")
    assert record is not None
    text, truncated = preview_text(vault, record, limit=5)
    assert text == "plain"
    assert truncated is True


def test_destination_for_stays_inside_root(tmp_path: Path) -> None:
    from devicedock.models import FileRecord

    record = FileRecord(file_id="a" * 40, domain="HomeDomain", relative_path="a/b/c.txt", flags=1)
    assert destination_for(tmp_path, record) == (tmp_path / "HomeDomain" / "a" / "b" / "c.txt").resolve()


def test_contacts_category(vault: BackupVault) -> None:
    result = load_category("contacts", vault)
    assert result.available, result.error
    assert result.columns[0] == "Name"
    assert ["Ada Lovelace", "+61 400 111 222", "ada@example.com", "Analytical", "Engineer"] in result.rows
    assert result.total == 2


def test_messages_category(vault: BackupVault) -> None:
    result = load_category("messages", vault)
    assert result.available, result.error
    assert len(result.rows) == 2
    assert result.rows[0][1] == "Sent"
    assert result.rows[0][4] == "Hi back"
    assert result.rows[1][4] == "Hello there"


def test_calls_category(vault: BackupVault) -> None:
    result = load_category("calls", vault)
    assert result.available, result.error
    assert result.rows[0][1] == "+61 400 555 666"
    assert result.rows[0][2] == "Outgoing"
    assert result.rows[0][4] == "42 s"


def test_calendar_category(vault: BackupVault) -> None:
    result = load_category("calendar", vault)
    assert result.available, result.error
    assert result.rows[0][0] == "Dentist"


def test_user_file_categories_focus_on_browsable_files() -> None:
    category_ids = [category.id for category in USER_FILE_CATEGORIES]
    assert category_ids == ["documents", "music", "photos", "videos", "podcasts", "books"]
    assert "apps" not in category_ids
    assert "safari" not in category_ids


def test_user_file_categories_are_discovered_from_manifest(vault: BackupVault) -> None:
    from devicedock.models import FileRecord

    class Manifest:
        def iter_files(self, job=None):
            yield from [
                FileRecord("a" * 40, "AppDomain-com.example.reader", "Documents/Manual.pdf", flags=1),
                FileRecord("b" * 40, "AppDomain-com.example.reader", "Library/Preferences/state.json", flags=1),
                FileRecord("c" * 40, "HomeDomain", "Documents/metadata.json", flags=1),
            ]

    original_manifest = vault.manifest
    vault.manifest = lambda: Manifest()  # type: ignore[method-assign]
    try:
        categories = user_file_categories(vault)
    finally:
        vault.manifest = original_manifest  # type: ignore[method-assign]
    titles = [category.title for category in categories]
    assert "Documents" in titles
    assert "Shared documents" in titles
    assert not any("chrome" in title.casefold() for title in titles)
    assert not any("state.json" in title for title in titles)


def test_notes_category(vault: BackupVault) -> None:
    result = load_category("notes", vault)
    assert result.available, result.error
    assert result.rows[0][0] == "Shopping"
    assert result.rows[0][2] == "milk, eggs"


def test_media_category(vault: BackupVault) -> None:
    result = load_category("media", vault)
    assert result.available, result.error
    assert result.rows[0][0] == "IMG_0001.JPG"
    assert result.rows[0][1] == "Photo"


def test_app_inventory_category_is_not_exposed() -> None:
    with pytest.raises(KeyError, match="apps"):
        load_category("apps", None)  # type: ignore[arg-type]


def test_export_category_formats(vault: BackupVault, tmp_path: Path) -> None:
    result = load_category("contacts", vault)
    csv_path = export_category(result, tmp_path / "contacts.csv")
    rows = list(csv.reader(csv_path.read_text().splitlines()))
    assert rows[0][0] == "Name"
    assert len(rows) == 3

    json_path = export_category(result, tmp_path / "contacts.json")
    data = json.loads(json_path.read_text())
    assert data["category"] == "Contacts"
    assert len(data["rows"]) == 2

    html_path = export_category(result, tmp_path / "contacts.html")
    assert "Ada Lovelace" in html_path.read_text()


def test_inventory_report(vault: BackupVault, tmp_path: Path) -> None:
    report = write_inventory_report(vault.info, vault.manifest(), tmp_path / "report.html")
    text = report.read_text()
    assert "Backup report: Test iPhone" in text
    assert "CameraRollDomain" in text
