# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Readable views over the iOS data that a backup actually contains.

Each category is parsed from the real payload databases (extracted to a private temporary
directory, opened read-only, removed afterwards). Schema differences between iOS releases
are handled explicitly: an unavailable table is reported as a limitation, never silently
replaced with placeholder data.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

from ..asyncutil import Job
from ..config import APP_CACHE_DIR
from ..models import human_size
from .manifest import ManifestQuery
from .vault import BackupVault
from .file_catalog import friendly_folder

log = logging.getLogger(__name__)

APPLE_EPOCH = datetime(2001, 1, 1)


class CategoryUnavailable(RuntimeError):
    """The data for this category is not present (or not supported) in this backup."""


@dataclass
class CategoryResult:
    category_id: str
    title: str
    available: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    note: str = ""
    error: str | None = None
    total: int = 0
    truncated: bool = False
    #: One manifest record per row, in the same order, when the category is file-backed.
    records: list = field(default_factory=list)

    def as_text(self) -> str:
        if not self.available:
            return self.error or self.note or "Not available in this backup."
        lines = ["\t".join(self.columns)]
        lines.extend("\t".join(row) for row in self.rows)
        return "\n".join(lines)


@dataclass
class Category:
    id: str
    title: str
    description: str
    icon: str
    parse: Callable[[BackupVault, Job | None, int], CategoryResult]


# --------------------------------------------------------------------------- helpers
def _cache_dir() -> Path:
    APP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    APP_CACHE_DIR.chmod(0o700)
    return APP_CACHE_DIR


def _format_timestamp(value: object) -> str:
    """Format an Apple timestamp (seconds since 2001, or nanoseconds since 2001)."""
    if value in (None, "", 0):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number <= 0:
        return "—"
    if number > 1e14:  # nanoseconds since 2001-01-01 (iOS 11+ message dates)
        number /= 1_000_000_000
    try:
        return (APPLE_EPOCH + timedelta(seconds=number)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, ValueError):
        return str(value)


@dataclass
class _Database:
    conn: sqlite3.Connection
    path: Path
    tempdir: Path

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tempdir, ignore_errors=True)


def _find_candidates(manifest: ManifestQuery, names: Iterable[str]) -> list[tuple[str, str]]:
    """Locate ``relativePath`` names anywhere in the manifest, best-known paths first."""
    found: list[tuple[str, str]] = []
    for name in names:
        for record in manifest.search(name, limit=50):
            if record.is_file and record.relative_path.endswith(name.lstrip("/")):
                found.append((record.domain, record.relative_path))
    # De-duplicate while keeping order.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _open_database(
    vault: BackupVault,
    names: Iterable[str],
    label: str,
) -> _Database:
    manifest = vault.manifest()
    unreadable: list[str] = []
    for domain, relative_path in _find_candidates(manifest, names):
        record = manifest.find(domain, relative_path)
        if record is None or not record.is_file:
            continue
        tempdir = Path(tempfile.mkdtemp(prefix="db-", dir=_cache_dir()))
        tempdir.chmod(0o700)
        target = tempdir / Path(relative_path).name
        try:
            vault.copy_file(record, target)
            for suffix in ("-wal", "-shm"):
                side = manifest.find(domain, relative_path + suffix)
                if side is not None and side.is_file:
                    vault.copy_file(side, target.with_name(target.name + suffix))
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return _Database(conn=conn, path=target, tempdir=tempdir)
        except sqlite3.DatabaseError as exc:
            shutil.rmtree(tempdir, ignore_errors=True)
            log.warning("%s database %s unreadable: %s", label, relative_path, exc)
            unreadable.append(f"{relative_path} ({exc})")
        except Exception:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise
    if unreadable:
        raise CategoryUnavailable(
            f"The {label} database is present but could not be read: " + "; ".join(unreadable)
        )
    raise CategoryUnavailable(f"{label} database was not found in this backup.")


def _query(conn: sqlite3.Connection, sql: str, params: tuple = (), limit: int = 0) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchmany(limit or 5000)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------------ parsers
def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _parse_contacts(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    db = _open_database(vault, ["AddressBook.sqlitedb"], "AddressBook")
    try:
        tables = _tables(db.conn)
        if "ABPerson" not in tables:
            raise CategoryUnavailable("AddressBook database has no ABPerson table.")
        person_columns = _table_columns(db.conn, "ABPerson")

        def column(name: str) -> str:
            return name if name in person_columns else f"NULL AS {name}"

        people = _query(
            db.conn,
            "SELECT ROWID, "
            + ", ".join(column(name) for name in ("First", "Last", "Organization", "Department", "JobTitle"))
            + " FROM ABPerson ORDER BY Last, First",
            limit=limit,
        )
        multi: dict[int, dict[int, list[str]]] = {}
        if "ABMultiValue" in tables:
            for row in db.conn.execute("SELECT record_id, property, value FROM ABMultiValue"):
                multi.setdefault(int(row["record_id"]), {}).setdefault(int(row["property"] or 0), []).append(
                    str(row["value"] or "")
                )
        rows: list[list[str]] = []
        for person in people:
            values = multi.get(int(person["ROWID"]), {})
            phones = values.get(3, [])
            emails = values.get(4, [])
            name = " ".join(part for part in (person["First"], person["Last"]) if part).strip()
            rows.append([
                name or str(person["Organization"] or "—"),
                "; ".join(phones) or "—",
                "; ".join(emails) or "—",
                str(person["Organization"] or "—"),
                str(person["JobTitle"] or "—"),
            ])
        total = db.conn.execute("SELECT count(*) FROM ABPerson").fetchone()[0]
        note = ""
        if not rows:
            note = "AddressBook contains no person records."
        return CategoryResult(
            category_id="contacts", title="Contacts", available=True,
            columns=["Name", "Phone", "Email", "Organisation", "Job title"],
            rows=rows, total=int(total), truncated=len(rows) >= limit, note=note,
        )
    finally:
        db.close()


def _parse_messages(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    db = _open_database(vault, ["sms.db"], "SMS")
    try:
        tables = _tables(db.conn)
        if "message" not in tables:
            raise CategoryUnavailable("sms.db has no message table.")
        has_handle = "handle" in tables
        sql = (
            "SELECT m.ROWID, m.text, m.date, m.is_from_me, m.service, "
            "       h.id AS sender "
            "FROM message m " + ("LEFT JOIN handle h ON m.handle_id = h.ROWID " if has_handle else "")
            + "ORDER BY m.date DESC"
        )
        rows: list[list[str]] = []
        for row in db.conn.execute(sql).fetchmany(limit):
            text = row["text"]
            direction = "Sent" if row["is_from_me"] else "Received"
            rows.append([
                _format_timestamp(row["date"]),
                direction,
                str(row["sender"] or "—") if has_handle else "—",
                str(row["service"] or "—"),
                (text if text is not None else "(body stored in attributedBody)"),
            ])
        total = db.conn.execute("SELECT count(*) FROM message").fetchone()[0]
        note = ""
        if rows and any(row[4].startswith("(body stored") for row in rows):
            note = ("Some messages store their body in a binary attributedBody blob; "
                    "those bodies are not decoded by this build.")
        return CategoryResult(
            category_id="messages", title="Messages", available=True,
            columns=["Date", "Direction", "Contact", "Service", "Text"],
            rows=rows, total=int(total), truncated=len(rows) >= limit, note=note,
        )
    finally:
        db.close()


def _parse_calls(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    db = _open_database(vault, ["CallHistory.storedata"], "Call history")
    try:
        table = next((t for t in _tables(db.conn) if t.upper().startswith("ZCALLRECORD")), None)
        if table is None:
            raise CategoryUnavailable("CallHistory database has no ZCALLRECORD table.")
        columns = _table_columns(db.conn, table)
        if not {"ZADDRESS", "ZDATE", "ZDURATION"} <= columns:
            raise CategoryUnavailable("Call history schema is not recognised on this iOS version.")
        originated = "ZORIGINATED" if "ZORIGINATED" in columns else "0 AS ZORIGINATED"
        answered = "ZANSWERED" if "ZANSWERED" in columns else "1 AS ZANSWERED"
        rows: list[list[str]] = []
        sql = (
            f"SELECT ZADDRESS, ZDATE, ZDURATION, {originated}, {answered} FROM {table} "
            "ORDER BY ZDATE DESC"
        )
        for row in db.conn.execute(sql).fetchmany(limit):
            rows.append([
                _format_timestamp(row["ZDATE"]),
                str(row["ZADDRESS"] or "—"),
                "Outgoing" if row["ZORIGINATED"] else "Incoming",
                "Answered" if row["ZANSWERED"] else "Missed",
                f"{int(row['ZDURATION'] or 0)} s",
            ])
        return CategoryResult(
            category_id="calls", title="Call history", available=True,
            columns=["Date", "Number", "Direction", "Result", "Duration"], rows=rows,
            total=len(rows), truncated=len(rows) >= limit,
        )
    finally:
        db.close()


def _first_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in columns:
            return name
    return None


def _protobuf_reader(data: bytes):
    """Yield ``(field_number, value)`` for a protobuf message; values are int or bytes."""
    index = 0
    length = len(data)
    while index < length:
        key = 0
        shift = 0
        while True:
            if index >= length:
                return
            byte = data[index]
            index += 1
            key |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        field, wire = key >> 3, key & 0x07
        if wire == 0:                      # varint
            value = 0
            shift = 0
            while True:
                if index >= length:
                    return
                byte = data[index]
                index += 1
                value |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            yield field, value
        elif wire == 2:                    # length-delimited
            size = 0
            shift = 0
            while True:
                if index >= length:
                    return
                byte = data[index]
                index += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
            if size < 0 or index + size > length:
                return
            yield field, data[index:index + size]
            index += size
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            return


def _looks_like_note_text(text: str) -> bool:
    """True when a decoded field reads as prose rather than protobuf framing."""
    if not text.strip():
        return False
    return all(char.isprintable() or char in "\n\r\t" for char in text)


def _note_text_candidates(data: bytes, depth: int = 0) -> list[str]:
    """Collect plausible text out of an Apple Notes body protobuf."""
    if depth > 6:
        return []
    found: list[str] = []
    for _field, value in _protobuf_reader(data):
        if not isinstance(value, bytes) or not value:
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            found.extend(_note_text_candidates(value, depth + 1))
            continue
        if _looks_like_note_text(text):
            found.append(text)
        else:
            # Control characters mean this field is an embedded message, not the note text.
            found.extend(_note_text_candidates(value, depth + 1))
    return found


def _decode_note_body(blob: bytes) -> str:
    """Best-effort plaintext of one note body.

    Apple stores the body as a gzipped protobuf; the note text is the longest printable
    UTF-8 string in it. Anything unexpected yields an empty string, so a note is still
    listed by title rather than disappearing.
    """
    if not blob:
        return ""
    import gzip

    try:
        raw = gzip.decompress(blob) if blob[:2] == b"\x1f\x8b" else blob
    except (OSError, EOFError):
        return ""
    candidates = _note_text_candidates(raw)
    if not candidates:
        return ""
    return max(candidates, key=len).strip()


def _note_bodies(conn: sqlite3.Connection) -> tuple[dict[int, bytes], dict[int, bytes]]:
    """Return body blobs keyed two ways, covering both NoteStore link layouts.

    Some releases link from ``ZICNOTEDATA.ZNOTE`` to ``ZNOTE.Z_PK``; others link from
    ``ZNOTE.ZNOTEDATA`` to ``ZICNOTEDATA.Z_PK``. Returning both maps lets the caller try
    each without guessing.
    """
    if "ZICNOTEDATA" not in _tables(conn):
        return {}, {}
    columns = _table_columns(conn, "ZICNOTEDATA")
    if "ZDATA" not in columns:
        return {}, {}
    link = _first_column(columns, ("ZNOTE", "ZNOTE1"))
    by_note: dict[int, bytes] = {}
    by_data: dict[int, bytes] = {}
    try:
        selection = "Z_PK, ZDATA" + (f", {link}" if link else "")
        for row in conn.execute(f"SELECT {selection} FROM ZICNOTEDATA"):
            if not row[1]:
                continue
            blob = bytes(row[1])
            by_data[int(row[0])] = blob
            if link is not None and row[2] is not None:
                by_note[int(row[2])] = blob
    except sqlite3.Error as exc:
        log.warning("Cannot read NoteStore bodies: %s", exc)
    return by_note, by_data


def _parse_notes(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    db = _open_database(vault, ["NoteStore.sqlite"], "Notes")
    try:
        tables = _tables(db.conn)
        if "ZNOTE" not in tables:
            raise CategoryUnavailable("NoteStore has no ZNOTE table.")
        columns = _table_columns(db.conn, "ZNOTE")
        title_column = _first_column(columns, ("ZTITLE1", "ZTITLE", "ZTITLE2"))
        date_column = _first_column(columns, ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE"))
        snippet_column = _first_column(columns, ("ZSNIPPET",))
        data_column = _first_column(columns, ("ZNOTEDATA",))

        selection = ["Z_PK"]
        selection += [name for name in (title_column, date_column, snippet_column,
                                        data_column) if name]
        order = f" ORDER BY {date_column} DESC" if date_column else ""
        # Only the columns this database actually has are queried: Apple has renamed and
        # dropped NoteStore fields across releases, and one missing name used to fail the
        # whole category.
        cursor = db.conn.execute(f"SELECT {', '.join(selection)} FROM ZNOTE{order}")
        by_note, by_data = _note_bodies(db.conn)

        rows: list[list[str]] = []
        while len(rows) < limit:
            row = cursor.fetchone()
            if row is None:
                break
            primary_key = int(row["Z_PK"])
            body_blob = by_note.get(primary_key, b"")
            if not body_blob and data_column and row[data_column] is not None:
                body_blob = by_data.get(int(row[data_column]), b"")
            body = _decode_note_body(body_blob)
            title = str(row[title_column] or "") if title_column else ""
            snippet = str(row[snippet_column] or "").strip() if snippet_column else ""
            if not title:
                title = body.splitlines()[0].strip() if body else "(untitled)"
            preview = snippet or (body.strip()[:200] if body else "")
            rows.append([
                title,
                _format_timestamp(row[date_column]) if date_column else "—",
                preview or "—",
            ])
        total = db.conn.execute("SELECT count(*) FROM ZNOTE").fetchone()[0]
        note = ("Bodies are decoded from the note store when readable; otherwise the stored "
                "snippet is shown.")
        return CategoryResult(
            category_id="notes", title="Notes", available=True,
            columns=["Title", "Modified", "Snippet"], rows=rows, total=int(total),
            truncated=len(rows) >= limit, note=note,
        )
    finally:
        db.close()


def _parse_calendar(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    db = _open_database(vault, ["Calendar.sqlitedb"], "Calendar")
    try:
        if "CalendarItem" not in _tables(db.conn):
            raise CategoryUnavailable("Calendar database has no CalendarItem table.")
        rows: list[list[str]] = []
        for row in db.conn.execute(
            "SELECT summary, start_date, end_date, all_day FROM CalendarItem "
            "ORDER BY start_date DESC"
        ).fetchmany(limit):
            rows.append([
                str(row["summary"] or "(no title)"),
                _format_timestamp(row["start_date"]),
                _format_timestamp(row["end_date"]),
                "Yes" if row["all_day"] else "No",
            ])
        total = db.conn.execute("SELECT count(*) FROM CalendarItem").fetchone()[0]
        return CategoryResult(
            category_id="calendar", title="Calendar", available=True,
            columns=["Summary", "Starts", "Ends", "All day"], rows=rows, total=int(total),
            truncated=len(rows) >= limit,
        )
    finally:
        db.close()


_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".dng", ".tiff"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mkv", ".webm"}
_MUSIC_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".aif", ".aiff", ".caf", ".ogg"}
_PODCAST_EXTENSIONS = _MUSIC_EXTENSIONS | {".m4b"}
_BOOK_EXTENSIONS = {".epub", ".mobi", ".azw", ".azw3", ".ibooks"}

#: Every media suffix the category parsers recognise, derived from the group sets above so
#: the two listings cannot drift apart.
_MEDIA_EXTENSIONS = _PHOTO_EXTENSIONS | _VIDEO_EXTENSIONS | _MUSIC_EXTENSIONS


def _parse_media(vault: BackupVault, job: Job | None, limit: int) -> CategoryResult:
    manifest = vault.manifest()
    rows: list[list[str]] = []
    records: list = []
    total = 0
    for record in manifest.files_in("CameraRollDomain", "Media"):
        if not record.is_file:
            continue
        suffix = Path(record.relative_path).suffix.lower()
        if suffix not in _MEDIA_EXTENSIONS:
            continue
        total += 1
        if len(rows) >= limit:
            continue
        kind = {"videos": "Video", "music": "Music"}.get(_media_group(record), "Photo")
        rows.append([
            Path(record.relative_path).name,
            kind,
            human_size(record.size),
            record.mtime_text,
            f"CameraRollDomain/{record.relative_path}",
        ])
        records.append(record)
    if total == 0:
        raise CategoryUnavailable("No photos, videos or music files are present in this backup.")
    return CategoryResult(
        category_id="media", title="Photos, videos & music", available=True,
        columns=["Name", "Kind", "Size", "Modified", "Manifest path"], rows=rows,
        records=records,
        total=total, truncated=total > len(rows),
        note="Listed from the backup manifest; use the Explorer to preview or export media.",
    )


def _media_group(record) -> str | None:
    suffix = Path(record.relative_path).suffix.lower()
    location = f"{record.domain}/{record.relative_path}".casefold()
    if suffix in _PHOTO_EXTENSIONS:
        return "photos"
    if suffix in _VIDEO_EXTENSIONS:
        return "videos"
    if suffix in _BOOK_EXTENSIONS or "/books/" in f"/{location}/":
        return "books"
    if suffix in _PODCAST_EXTENSIONS and ("podcast" in location or suffix == ".m4b"):
        return "podcasts"
    if suffix in _MUSIC_EXTENSIONS:
        return "music"
    return None


def _parse_media_group(vault: BackupVault, job: Job | None, limit: int,
                       group: str) -> CategoryResult:
    titles = {"photos": "Photos", "videos": "Videos", "music": "Music",
              "podcasts": "Podcasts", "books": "Books"}
    manifest = vault.manifest()
    rows: list[list[str]] = []
    records: list = []
    total = 0
    for record in manifest.iter_files(job=job):
        if not record.is_file or (record.domain.startswith("AppDomain-")
                                  and not _in_user_file_folder(record)):
            continue
        if _media_group(record) != group:
            continue
        total += 1
        if len(rows) >= limit:
            continue
        rows.append([
            Path(record.relative_path).name,
            friendly_folder(record),
            human_size(record.size), record.mtime_text,
        ])
        records.append(record)
    if total == 0:
        raise CategoryUnavailable(f"No {titles[group].lower()} are present in this backup.")
    return CategoryResult(
        category_id=group, title=titles[group], available=True,
        columns=["Name", "Folder", "Size", "Modified"], rows=rows,
        records=records, total=total, truncated=total > len(rows),
        note=f"{titles[group]} from user-accessible backup locations.",
    )


def _media_group_parser(group: str):
    return lambda vault, job, limit: _parse_media_group(vault, job, limit, group)


_DOCUMENT_EXTENSIONS = {
    ".pdf", ".txt", ".rtf", ".rtfd", ".md", ".csv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".pages", ".numbers", ".key", ".tex",
}
_DOCUMENT_SUPPORT_SUFFIXES = {".json", ".xml", ".plist", ".db", ".sqlite", ".sqlite3",
                              ".log", ".conf", ".ini", ".yaml", ".yml"}

# Paths that hold app support data rather than anything a user would open.
_DOCUMENT_SKIP_PARTS = ("/library/caches/", "/library/preferences/", "/tmp/", "/.trash/")


def _in_user_file_folder(record) -> bool:
    lowered = f"/{record.relative_path}".casefold()
    return any(marker in lowered for marker in
               ("/documents/", "/downloads/", "/mobile documents/", "/clouddocs/"))


def _is_document(record) -> bool:
    path = Path(record.relative_path)
    if not record.is_file or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return False
    lowered = f"/{record.relative_path}".casefold()
    if any(part in lowered for part in _DOCUMENT_SKIP_PARTS):
        return False
    if path.suffix.lower() in _DOCUMENT_SUPPORT_SUFFIXES:
        return False
    if path.suffix.lower() in (_PHOTO_EXTENSIONS | _VIDEO_EXTENSIONS | _MUSIC_EXTENSIONS | _BOOK_EXTENSIONS):
        return False
    user_folder = _in_user_file_folder(record)
    if record.domain.startswith("AppDomain-") and not user_folder:
        return False
    # In user-visible folders, expose all file types (archives and app-native formats
    # included) except known media and machine-readable support files. Elsewhere, only
    # recognise conventional documents to avoid leaking app databases into the browser.
    return user_folder or path.suffix.lower() in _DOCUMENT_EXTENSIONS


def _document_location(record) -> tuple[str, str]:
    """Return a stable grouping key and readable location derived from the manifest path."""
    parts = Path(record.relative_path).parts
    lowered = [part.casefold() for part in parts]
    anchors = {"documents", "downloads", "mobile documents", "clouddocs"}
    index = next((i for i, part in enumerate(lowered) if part in anchors), None)
    if index is None:
        index = max(0, len(parts) - 2)
    location_parts = parts[index:-1]
    domain = record.domain
    if domain.startswith("AppDomain-"):
        # Keep app containers grouped as shared documents. The bundle ID remains available
        # in the technical properties, not as the category's primary label.
        location_parts = ("Shared documents", *location_parts[1:])
    location = "/".join(location_parts) or Path(record.relative_path).parent.name or "Documents"
    source = "AppDocuments" if domain.startswith("AppDomain-") else domain
    key = f"{source}\0{'/'.join(parts[:-1])}"
    return key, location


def _document_category_id(key: str) -> str:
    return "documents-location:" + quote(key, safe="")


def _parse_documents(vault: BackupVault, job: Job | None, limit: int,
                     group: str | None = None) -> CategoryResult:
    """User documents, optionally limited to a location discovered in the manifest."""
    title = "Documents"
    manifest = vault.manifest()
    rows: list[list[str]] = []
    records: list = []
    total = 0
    for record in manifest.iter_files(job=job):
        if not _is_document(record):
            continue
        location_key, location_title = _document_location(record)
        if group is not None and location_key != group:
            continue
        total += 1
        if len(rows) >= limit:
            continue
        rows.append([
            Path(record.relative_path).name,
            location_title,
            Path(record.relative_path).suffix.lower().lstrip(".").upper() or "FILE",
            human_size(record.size),
            record.mtime_text,
        ])
        records.append(record)
    if total == 0:
        raise CategoryUnavailable(
            f"No user documents are present in {title}." if group else
            "No user documents are present in this backup.")
    return CategoryResult(
        category_id=_document_category_id(group) if group else "documents", title=location_title if group and rows else title, available=True,
        columns=["Name", "Folder", "Type", "Size", "Modified"], rows=rows,
        records=records, total=total, truncated=total > len(rows),
        note=f"{title} files from the locations present in this backup. App support data is hidden.",
    )


def _document_group_parser(group: str):
    return lambda vault, job, limit: _parse_documents(vault, job, limit, group)


CATEGORIES: list[Category] = [
    Category("contacts", "Contacts", "Names, phone numbers and e-mail addresses from AddressBook.",
             "avatar-default-symbolic", _parse_contacts),
    Category("messages", "Messages", "SMS and iMessage conversations from sms.db.",
             "mail-message-new-symbolic", _parse_messages),
    Category("calls", "Call history", "Incoming, outgoing and missed calls.",
             "call-start-symbolic", _parse_calls),
    Category("media", "Photos, videos & music", "Media files listed from the manifest.",
             "camera-photo-symbolic", _parse_media),
    Category("documents", "Documents", "PDFs, text, Office and e-book files with preview.",
             "x-office-document-symbolic", _parse_documents),
    Category("music", "Music", "Music files from the backup.",
             "audio-x-generic-symbolic", _media_group_parser("music")),
    Category("photos", "Photos", "Photo files from the backup.",
             "camera-photo-symbolic", _media_group_parser("photos")),
    Category("videos", "Videos", "Video files from the backup.",
             "video-x-generic-symbolic", _media_group_parser("videos")),
    Category("podcasts", "Podcasts", "Podcast files from the backup.",
             "audio-x-generic-symbolic", _media_group_parser("podcasts")),
    Category("books", "Books", "E-books from the backup.",
             "x-office-book-symbolic", _media_group_parser("books")),
    Category("notes", "Notes", "Titles and stored snippets from NoteStore.sqlite.",
             "accessories-text-editor-symbolic", _parse_notes),
    Category("calendar", "Calendar", "Calendar events with start and end times.",
             "x-office-calendar-symbolic", _parse_calendar),
]

CATEGORY_BY_ID = {category.id: category for category in CATEGORIES}
USER_FILE_CATEGORIES = [CATEGORY_BY_ID[name] for name in
                        ("documents", "music", "photos", "videos", "podcasts", "books")]


def user_file_categories(vault: BackupVault | None, job: Job | None = None) -> list[Category]:
    """Build browsable categories from the user files actually present in a backup."""
    if vault is None or not vault.unlocked:
        return []
    manifest = vault.manifest()
    present: set[str] = set()
    locations: dict[str, str] = {}
    for record in manifest.iter_files(job=job):
        if not record.is_file:
            continue
        media = (_media_group(record) if not record.domain.startswith("AppDomain-")
                 or _in_user_file_folder(record) else None)
        if media:
            present.add(media)
        if _is_document(record):
            key, title = _document_location(record)
            locations[key] = title
    categories: list[Category] = []
    for name in ("documents", "music", "photos", "videos", "podcasts", "books"):
        if name == "documents" and locations:
            categories.append(CATEGORY_BY_ID[name])
        elif name in present:
            categories.append(CATEGORY_BY_ID[name])
    categories.extend(
        Category(_document_category_id(key), title, f"Documents in {title}.",
                 "folder-symbolic", _document_group_parser(key))
        for key, title in sorted(locations.items(), key=lambda item: item[1].casefold())
    )
    return categories


def load(category_id: str, vault: BackupVault, job: Job | None = None, limit: int = 2000) -> CategoryResult:
    """Parse one category, converting expected failure modes into a result."""
    category = CATEGORY_BY_ID.get(category_id)
    if category is None and category_id.startswith("documents-location:"):
        category = next((item for item in user_file_categories(vault) if item.id == category_id), None)
        if category is None:
            raise KeyError(f"unknown category {category_id!r}")
    if category is None:
        raise KeyError(f"unknown category {category_id!r}")
    try:
        return category.parse(vault, job, limit)
    except CategoryUnavailable as exc:
        return CategoryResult(category_id=category.id, title=category.title, available=False,
                              error=str(exc))
    except sqlite3.Error as exc:
        log.warning("Category %s query failed: %s", category_id, exc)
        return CategoryResult(category_id=category.id, title=category.title, available=False,
                              error=f"Database error: {exc}")
    except Exception as exc:  # noqa: BLE001 - surfaced, never hidden
        log.error("Category %s failed: %s", category_id, exc, exc_info=True)
        return CategoryResult(category_id=category.id, title=category.title, available=False,
                              error=f"{type(exc).__name__}: {exc}")
