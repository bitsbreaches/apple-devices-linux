# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backup handling: discovery, manifest access, decryption, extraction and parsing."""

from .categories import (
    CATEGORIES,
    USER_FILE_CATEGORIES,
    CATEGORY_BY_ID,
    CategoryResult,
    load as load_category,
    user_file_categories,
)
from .discovery import discover, is_backup_directory, read_backup_info
from .extract import ExportResult, UnsafePathError, export_records, preview_text
from .file_catalog import CatalogCategory, CatalogFile, UserFileCatalog, build_user_file_catalog
from .manifest import ChildEntry, DomainStat, ManifestError, ManifestQuery
from .report import export_category, write_inventory_report
from .vault import (
    BackupLockedError,
    BackupVault,
    BadPasswordError,
    DecryptionUnavailableError,
)

__all__ = [
    "CATEGORIES",
    "USER_FILE_CATEGORIES",
    "CATEGORY_BY_ID",
    "BackupLockedError",
    "BackupVault",
    "BadPasswordError",
    "CategoryResult",
    "CatalogCategory",
    "CatalogFile",
    "ChildEntry",
    "DecryptionUnavailableError",
    "DomainStat",
    "ExportResult",
    "ManifestError",
    "ManifestQuery",
    "UnsafePathError",
    "UserFileCatalog",
    "build_user_file_catalog",
    "discover",
    "export_category",
    "export_records",
    "is_backup_directory",
    "load_category",
    "user_file_categories",
    "preview_text",
    "read_backup_info",
    "write_inventory_report",
]
