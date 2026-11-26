# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Read-only access to the backup manifest (``Manifest.db``).

The manifest is a SQLite database whose ``Files`` table maps a 40-character ``fileID`` to a
``domain``, a ``relativePath`` and a ``file`` blob holding size, timestamps, protection class,
wrapped per-file key and symlink target.

Apple stores that blob as an ``NSKeyedArchiver`` archive holding one ``MBFile`` object, whose
fields are capitalised (``Size``, ``LastModified``, ``ProtectionClass``, ``EncryptionKey``…).
Older backups use the same capitalised keys in a flat plist. Both forms are normalised here to
the lowercase names :class:`~devicedock.models.FileRecord` uses; reading only lowercase keys
silently yields size 0 and no per-file key for every real backup.

Encrypted backups (iOS 10.2 and later) store this database encrypted; a decrypted copy is
provided by :mod:`devicedock.backup.vault`.
"""

from __future__ import annotations

import logging
import plistlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..models import FileRecord

log = logging.getLogger(__name__)

MAX_CHILDREN = 20000

#: Seconds between the Apple reference date (2001-01-01) and the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

#: ``MBFile`` field name → the attribute name used by :class:`FileRecord`.
_METADATA_FIELDS = {
    "size": "Size",
    "mtime": "LastModified",
    "mode": "Mode",
    "protection_class": "ProtectionClass",
    "encryption_key": "EncryptionKey",
    "symlink_target": "Target",
    "digest": "Digest",
}



class ManifestError(RuntimeError):
    """Raised when the manifest is missing, unreadable or has an unexpected schema."""


@dataclass
class DomainStat:
    domain: str
    file_count: int
    total_size: int


@dataclass
class ChildEntry:
    """A node in the backup browser tree (file or synthesised directory)."""

    name: str
    is_dir: bool
    record: FileRecord | None
    child_count: int = 0
    total_size: int | None = 0

    @property
    def size(self) -> int | None:
        return self.total_size if self.is_dir else (self.record.size if self.record else None)


def _resolve_reference(objects: list, value: object) -> object:
    """Follow a ``$objects`` reference and unwrap the Foundation container types."""
    while isinstance(value, plistlib.UID):
        if not objects or value.data >= len(objects):
            return None
        value = objects[value.data]
    if isinstance(value, dict):
        if "NS.data" in value:            # NSMutableData / NSData
            return value["NS.data"]
        if "NS.time" in value:            # NSDate, seconds since the Apple reference date
            try:
                return APPLE_EPOCH_OFFSET + float(value["NS.time"])
            except (TypeError, ValueError):
                return None
    return value


def _archive_root(value: dict) -> tuple[dict, list]:
    """Return ``(MBFile object, $objects)`` for an ``NSKeyedArchiver`` payload."""
    objects = value.get("$objects")
    if not isinstance(objects, list):
        return {}, []
    top = value.get("$top")
    root_ref = top.get("root") if isinstance(top, dict) else None
    root = _resolve_reference(objects, root_ref)
    return (root if isinstance(root, dict) else {}), objects


def parse_file_blob(blob: bytes | None) -> dict:
    """Normalise a manifest ``file`` blob to the lowercase keys ``FileRecord`` uses.

    Accepts Apple's ``NSKeyedArchiver`` archive (what iOS writes today, encrypted or not), a
    flat plist with the same capitalised ``MBFile`` keys, and the older flat lowercase form.
    An unreadable blob yields ``{}`` so one corrupt row cannot break a whole listing.
    """
    if not blob:
        return {}
    try:
        raw = plistlib.loads(blob)
    except Exception as exc:  # noqa: BLE001 - report and continue with empty metadata
        log.warning("Malformed manifest file blob: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    objects: list = []
    if raw.get("$archiver") == "NSKeyedArchiver":
        raw, objects = _archive_root(raw)
        if not raw:
            return {}

    meta: dict[str, object] = {}
    for attribute, field in _METADATA_FIELDS.items():
        value = raw.get(field)
        if value is None:
            value = raw.get(attribute)      # legacy flat, lowercase
        if value is None:
            continue
        value = _resolve_reference(objects, value)
        if value is None or isinstance(value, (dict, list)):
            continue
        if attribute in ("size", "mode", "protection_class"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif attribute == "mtime":
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
        meta[attribute] = value
    return meta


def record_from_row(row: tuple) -> FileRecord:
    file_id, domain, relative_path, flags, blob = row
    meta = parse_file_blob(blob)
    return FileRecord(
        file_id=file_id,
        domain=domain or "",
        relative_path=relative_path or "",
        flags=int(flags or 0),
        size=int(meta["size"]) if meta.get("size") is not None else None,
        mtime=meta.get("mtime"),
        mode=meta.get("mode"),
        protection_class=meta.get("protection_class"),
        encryption_key=meta.get("encryption_key"),
        symlink_target=meta.get("symlink_target"),
        digest=meta.get("digest"),
    )


def _add_size(left: int | None, right: int | None) -> int | None:
    return left + right if left is not None and right is not None else None


class ManifestQuery:
    """Query helper over a plaintext ``Manifest.db`` (read-only)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise ManifestError(f"Manifest database not found: {self.db_path}")
        try:
            uri = f"file:{self.db_path}?mode=ro&immutable=1"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("SELECT count(*) FROM Files").fetchone()
        except sqlite3.DatabaseError as exc:
            raise ManifestError(
                "Manifest.db is not a readable SQLite database. "
                "Encrypted backups must be unlocked with their password first."
            ) from exc

    # --- schema ------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def total_files(self) -> int:
        return int(self._conn.execute("SELECT count(*) FROM Files").fetchone()[0])

    def domains_with_sizes(self) -> list[DomainStat]:
        """Per-domain counts and byte totals computed from parsed blobs.

        ``json_extract`` cannot read binary plists, so sizes are summed in Python.
        """
        stats: dict[str, DomainStat] = {}
        for row in self._conn.execute("SELECT domain, file, flags FROM Files"):
            domain = row[0] or ""
            stat = stats.get(domain)
            if stat is None:
                stat = stats[domain] = DomainStat(domain=domain, file_count=0, total_size=0)
            stat.file_count += 1
            if int(row[2] or 0) == 1:
                stat.total_size += int(parse_file_blob(row[1]).get("size") or 0)
        return [stats[key] for key in sorted(stats)]

    # --- browsing ----------------------------------------------------
    def children(self, domain: str, path: str = "") -> list[ChildEntry]:
        """Return the immediate children of ``path`` inside ``domain``."""
        prefix = path.strip("/")
        if prefix:
            low = f"{prefix}/"
            high = f"{prefix}0"  # '0' is the character after '/' in ASCII
            sql = (
                "SELECT fileID, domain, relativePath, flags, file FROM Files "
                "WHERE domain = ? AND relativePath >= ? AND relativePath < ? "
                "ORDER BY relativePath LIMIT ?"
            )
            params: tuple = (domain, low, high, MAX_CHILDREN)
        else:
            sql = (
                "SELECT fileID, domain, relativePath, flags, file FROM Files "
                "WHERE domain = ? ORDER BY relativePath LIMIT ?"
            )
            params = (domain, MAX_CHILDREN)

        nodes: dict[str, ChildEntry] = {}
        rows = self._conn.execute(sql, params).fetchall()
        for row in rows:
            record = record_from_row(row)
            remainder = record.relative_path[len(prefix) + 1 :] if prefix else record.relative_path
            if not remainder:
                continue
            name, sep, _rest = remainder.partition("/")
            node = nodes.get(name)
            if sep:
                if node is None:
                    nodes[name] = ChildEntry(name=name, is_dir=True, record=None, child_count=1,
                                             total_size=record.size)
                else:
                    node.child_count += 1
                    node.total_size = _add_size(node.total_size, record.size)
            elif node is None or node.is_dir:
                if node is not None:  # a directory entry also exists; keep its aggregate
                    node.child_count += 1
                    node.total_size = _add_size(node.total_size, record.size)
                else:
                    nodes[name] = ChildEntry(name=name, is_dir=record.is_directory, record=record,
                                             total_size=record.size)
            else:
                node.total_size = _add_size(node.total_size, record.size)
        return sorted(nodes.values(), key=lambda n: (not n.is_dir, n.name.lower()))

    def files_in(self, domain: str, path: str) -> list[FileRecord]:
        """All file rows at or below ``path`` (bounded)."""
        if path:
            sql = (
                "SELECT fileID, domain, relativePath, flags, file FROM Files "
                "WHERE domain = ? AND (relativePath = ? OR (relativePath >= ? AND relativePath < ?)) "
                "LIMIT ?"
            )
            params: tuple = (domain, path, f"{path}/", f"{path}0", MAX_CHILDREN)
        else:
            sql = (
                "SELECT fileID, domain, relativePath, flags, file FROM Files "
                "WHERE domain = ? LIMIT ?"
            )
            params = (domain, MAX_CHILDREN)
        rows = self._conn.execute(sql, params).fetchall()
        return [record_from_row(row) for row in rows]

    def iter_files(self, batch_size: int = 2000, job=None):
        """Yield every record in bounded keyset batches, ordered by hashed file ID."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        last_id = ""
        while True:
            if job is not None:
                job.check_cancelled()
            rows = self._conn.execute(
                "SELECT fileID, domain, relativePath, flags, file FROM Files "
                "WHERE fileID > ? ORDER BY fileID LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                last_id = str(row[0] or "")
                yield record_from_row(row)
            if len(rows) < batch_size:
                return

    def search(self, text: str, limit: int = 500) -> list[FileRecord]:
        pattern = f"%{text}%"
        rows = self._conn.execute(
            "SELECT fileID, domain, relativePath, flags, file FROM Files "
            "WHERE relativePath LIKE ? OR domain LIKE ? ORDER BY domain, relativePath LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [record_from_row(row) for row in rows]

    def record(self, file_id: str) -> FileRecord | None:
        row = self._conn.execute(
            "SELECT fileID, domain, relativePath, flags, file FROM Files WHERE fileID = ?",
            (file_id,),
        ).fetchone()
        return record_from_row(tuple(row)) if row else None

    def find(self, domain: str, relative_path: str) -> FileRecord | None:
        row = self._conn.execute(
            "SELECT fileID, domain, relativePath, flags, file FROM Files "
            "WHERE domain = ? AND relativePath = ? LIMIT 1",
            (domain, relative_path),
        ).fetchone()
        return record_from_row(tuple(row)) if row else None
