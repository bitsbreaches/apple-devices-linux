# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""User-facing file catalog built from backup manifest records, not raw domains."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..asyncutil import Job
from ..fileops import unique_destination
from ..models import FileRecord
from .extract import ExportResult, sanitize_component
from .vault import PayloadMissingError
from .manifest import ManifestQuery

PHOTO = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".dng", ".tif", ".tiff", ".webp"}
VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mpeg", ".mpg"}
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".aif", ".aiff", ".caf", ".ogg", ".opus", ".wma", ".alac"}
BOOK = {".epub", ".mobi", ".azw", ".azw3", ".ibooks", ".m4b"}
DOCUMENT = {
    ".pdf", ".txt", ".rtf", ".rtfd", ".md", ".csv", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key", ".tex",
    ".zip", ".cbz", ".cbr",
}
SUPPORT = {".json", ".xml", ".plist", ".db", ".sqlite", ".sqlite3", ".log", ".conf", ".ini", ".yaml", ".yml"}
ANCHORS = {"documents", "downloads", "mobile documents", "clouddocs", "dcim", "books", "podcasts", "music"}
ORDER = ("photos", "videos", "music", "podcasts", "books", "documents", "downloads", "app-documents")
LABELS = {
    "photos": ("Photos", "camera-photo-symbolic"),
    "videos": ("Videos", "video-x-generic-symbolic"),
    "music": ("Music", "audio-x-generic-symbolic"),
    "podcasts": ("Podcasts", "audio-x-generic-symbolic"),
    "books": ("Books", "x-office-book-symbolic"),
    "documents": ("Documents", "x-office-document-symbolic"),
    "downloads": ("Downloads", "folder-download-symbolic"),
    "app-documents": ("Shared app documents", "folder-symbolic"),
}


@dataclass(frozen=True)
class CatalogFile:
    category_id: str
    name: str
    folder: str
    file_type: str
    size: int | None
    modified: str
    record: FileRecord


@dataclass(frozen=True)
class CatalogCategory:
    id: str
    title: str
    icon: str
    count: int


@dataclass
class UserFileCatalog:
    categories: list[CatalogCategory]
    files: dict[str, list[CatalogFile]]
    total_scanned: int


def _parts(record: FileRecord) -> tuple[str, ...] | None:
    path = PurePosixPath(record.relative_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.parts


def _classify(record: FileRecord) -> tuple[str, ...]:
    if not record.is_file:
        return ()
    parts = _parts(record)
    if not parts:
        return ()
    folded = tuple(part.casefold() for part in parts)
    if any(part in {"library", "caches", "preferences", "tmp", ".trash", "ubf"} for part in folded):
        return ()
    suffix = PurePosixPath(record.relative_path).suffix.casefold()
    if suffix in SUPPORT:
        return ()
    app_domain = record.domain.startswith("AppDomain-")
    user_folder = any(part in ANCHORS for part in folded)
    camera_roll = record.domain == "CameraRollDomain"
    media_domain = record.domain in {"MediaDomain", "CameraRollDomain"}
    if app_domain and not user_folder:
        return ()
    if not (user_folder or camera_roll or media_domain):
        return ()

    categories: list[str] = []
    in_downloads = "downloads" in folded
    if suffix in PHOTO:
        categories.append("photos")
    elif suffix in VIDEO:
        categories.append("videos")
    elif suffix in BOOK or "books" in folded:
        categories.append("books")
    elif suffix in AUDIO:
        categories.append("music")
        if "podcast" in "/".join(folded) or suffix == ".m4b":
            categories.append("podcasts")
    elif suffix in DOCUMENT or (user_folder and not suffix):
        categories.append("documents")
    elif user_folder and suffix and suffix not in SUPPORT:
        # User-visible folders can contain app-native and archive formats unknown to this
        # build. Keep them discoverable and label their type from MIME/extension metadata.
        categories.append("documents")

    if in_downloads and categories:
        categories.append("downloads")
    if app_domain and categories:
        categories.append("app-documents")
    return tuple(dict.fromkeys(categories))


def _folder_display(record: FileRecord) -> str:
    parts = _parts(record) or ()
    folded = [part.casefold() for part in parts]
    anchor = next((index for index, part in enumerate(folded) if part in ANCHORS), None)
    if record.domain.startswith("AppDomain-"):
        tail = list(parts[anchor + 1:-1]) if anchor is not None else []
        return "/".join(("Shared documents", *tail))
    if record.domain == "CameraRollDomain":
        if "dcim" in folded:
            index = folded.index("dcim")
            album = parts[index + 1:-1]
            return "/".join(("Camera Roll", *album))
        return "Camera Roll"
    if anchor is not None:
        return "/".join(parts[anchor:-1]) or parts[anchor]
    return "Files"


def friendly_folder(record: FileRecord) -> str:
    """Public user-facing location label; internal domain/path stays in properties."""
    return _folder_display(record)


def _file_type(record: FileRecord) -> str:
    suffix = PurePosixPath(record.relative_path).suffix.casefold()
    if suffix:
        mime, _encoding = mimetypes.guess_type(record.relative_path)
        if mime:
            if mime.startswith("image/"):
                return "Image"
            if mime.startswith("video/"):
                return "Video"
            if mime.startswith("audio/"):
                return "Audio"
            if mime == "application/pdf":
                return "PDF document"
        return suffix[1:].upper() + " file"
    return "File"


def build_user_file_catalog(manifest: ManifestQuery, job: Job | None = None) -> UserFileCatalog:
    """Index browsable user files with bounded manifest reads and cancellable progress."""
    total = manifest.total_files()
    files: dict[str, list[CatalogFile]] = {category: [] for category in ORDER}
    scanned = 0
    for record in manifest.iter_files(job=job):
        scanned += 1
        for category in _classify(record):
            files[category].append(CatalogFile(
                category_id=category,
                name=PurePosixPath(record.relative_path).name or "Unnamed file",
                folder=_folder_display(record),
                file_type=_file_type(record),
                size=record.size,
                modified=record.mtime_text,
                record=record,
            ))
        if job is not None and (scanned % 1000 == 0 or scanned == total):
            job.progress(scanned / max(1, total), f"Indexing user files: {scanned:,}/{total:,}")
    categories = [CatalogCategory(category, *LABELS[category], len(files[category]))
                  for category in ORDER if files[category]]
    for entries in files.values():
        entries.sort(key=lambda item: (item.folder.casefold(), item.name.casefold(), item.record.file_id))
    return UserFileCatalog(categories, files, scanned)


def export_catalog_files(vault, entries: list[CatalogFile], root, job: Job | None = None) -> ExportResult:
    """Export selected user files into readable category/folder names, never domain paths."""
    from pathlib import Path

    result = ExportResult()
    root = Path(root).expanduser()
    resolved_root = root.resolve()
    for index, entry in enumerate(entries):
        if job is not None:
            job.check_cancelled()
            job.progress(index / max(1, len(entries)), f"Exporting {entry.name}")
        category_name = LABELS[entry.category_id][0]
        relative_parts = [sanitize_component(category_name)]
        relative_parts.extend(sanitize_component(part) for part in entry.folder.split("/") if part)
        relative_parts.append(sanitize_component(entry.name))
        target = root.joinpath(*relative_parts)
        if not target.resolve().is_relative_to(resolved_root):
            result.errors.append(f"Unsafe destination for {entry.name}.")
            continue
        try:
            target = unique_destination(target)
            vault.copy_file(entry.record, target, job=job)
            result.exported.append(target)
            result.bytes_written += target.stat().st_size
        except PayloadMissingError:
            result.missing.append(entry.name)
        except Exception as exc:  # noqa: BLE001 - report each item without logging its path
            result.errors.append(f"{entry.name}: {exc}")
    if job is not None:
        job.progress(1.0, result.summary)
    return result

