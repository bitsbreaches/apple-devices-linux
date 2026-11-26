# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
from __future__ import annotations

from devicedock.backup.file_catalog import build_user_file_catalog
from devicedock.models import FileRecord


class Manifest:
    def __init__(self, records):
        self.records = records

    def total_files(self):
        return len(self.records)

    def iter_files(self, job=None):
        yield from self.records


def test_catalog_finds_user_files_without_exposing_app_identifiers() -> None:
    records = [
        FileRecord("a" * 40, "CameraRollDomain", "Media/DCIM/100APPLE/photo.HEIC", 1,
                   size=512, mtime=10),
        FileRecord("b" * 40, "AppDomain-com.vendor.reader", "Documents/Manual.pdf", 1,
                   size=1024),
        FileRecord("c" * 40, "AppDomain-com.vendor.reader", "Library/Preferences/state.json", 1,
                   size=50),
        FileRecord("d" * 40, "HomeDomain", "Documents/Downloads/archive.zip", 1,
                   size=None),
    ]

    catalog = build_user_file_catalog(Manifest(records))

    assert [item.id for item in catalog.categories] == [
        "photos", "documents", "downloads", "app-documents",
    ]
    assert catalog.files["photos"][0].name == "photo.HEIC"
    assert catalog.files["photos"][0].folder == "Camera Roll/100APPLE"
    assert any(item.name == "Manual.pdf" for item in catalog.files["documents"])
    assert all("com.vendor.reader" not in item.folder for item in catalog.files["app-documents"])
    assert catalog.files["downloads"][0].size is None
    assert all("state.json" not in item.name for items in catalog.files.values() for item in items)
