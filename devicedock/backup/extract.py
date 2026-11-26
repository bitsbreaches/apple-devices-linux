# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Safe export of backup records to the local filesystem.

Every destination path is rebuilt from validated components, so a malicious or corrupted
manifest cannot write outside the chosen export directory (``../`` traversal, absolute
paths, NUL bytes, Windows reserved names). Symlinks are exported as descriptive text
instead of links, so nothing in the export tree can point back into the backup or at the
user's home directory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..asyncutil import Job
from ..fileops import unique_destination
from ..models import FileRecord
from .vault import BackupVault, PayloadMissingError

log = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f<>:\"|?*]")
_MAX_COMPONENT = 200


class UnsafePathError(ValueError):
    """A manifest path tried to escape the export root."""


def sanitize_component(name: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
    if not cleaned:
        cleaned = "_"
    if len(cleaned) > _MAX_COMPONENT:
        cleaned = cleaned[:_MAX_COMPONENT]
    return cleaned


def validated_relative(record: FileRecord) -> Path:
    """Return a traversal-safe relative path for ``record``."""
    raw = record.relative_path.replace("\\", "/")
    if raw.startswith("/"):
        raise UnsafePathError(f"absolute manifest path rejected: {raw!r}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError(f"path traversal rejected: {raw!r}")
        parts.append(sanitize_component(part))
    if not parts:
        parts = [sanitize_component(Path(record.file_id).name or "unnamed")]
    return Path(sanitize_component(record.domain or "unknown"), *parts)


def destination_for(root: Path, record: FileRecord) -> Path:
    root = Path(root)
    target = (root / validated_relative(record)).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise UnsafePathError(f"destination escapes export root: {target}")
    return target


@dataclass
class ExportResult:
    exported: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Manifest entries whose payload is not in the backup: reported separately from a
    #: genuine failure, because nothing about the export itself went wrong.
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bytes_written: int = 0

    @property
    def summary(self) -> str:
        parts = [f"{len(self.exported)} file(s) exported"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        if self.missing:
            parts.append(f"{len(self.missing)} not in this backup")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def export_records(
    vault: BackupVault,
    records: list[FileRecord],
    root: Path,
    job: Job | None = None,
) -> ExportResult:
    """Write ``records`` below ``root`` preserving domain/relativePath structure."""
    result = ExportResult()
    root = Path(root)
    total = max(1, len(records))
    for index, record in enumerate(records):
        if job is not None:
            job.check_cancelled()
            job.progress(index / total, f"Exporting {Path(record.relative_path).name}")
        try:
            target = destination_for(root, record)
        except UnsafePathError as exc:
            result.errors.append(str(exc))
            continue
        if record.is_directory:
            result.skipped.append(f"{record.domain}/{record.relative_path} (directory)")
            continue
        if record.is_symlink:
            target.parent.mkdir(parents=True, exist_ok=True)
            link_note = target.with_suffix(target.suffix + ".symlink.txt")
            link_note.write_text(
                f"Symlink in backup\nfrom: {record.domain}/{record.relative_path}\n"
                f"target: {record.symlink_target or 'unknown'}\n",
                encoding="utf-8",
            )
            result.exported.append(link_note)
            continue
        try:
            target = unique_destination(target, start=1)
            data = vault.read_file(record)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            result.bytes_written += len(data)
            result.exported.append(target)
        except PayloadMissingError:
            result.missing.append(f"{record.domain}/{record.relative_path}")
        except Exception as exc:  # noqa: BLE001 - per-file failure is reported
            log.warning("Export failed for %s/%s: %s", record.domain, record.relative_path, exc)
            result.errors.append(f"{record.domain}/{record.relative_path}: {exc}")
    if job is not None:
        job.progress(1.0, result.summary)
    return result


def preview_text(vault: BackupVault, record: FileRecord, limit: int = 65536) -> tuple[str, bool]:
    """Return ``(text, truncated)`` for a text preview of one record.

    Raises :class:`~devicedock.backup.vault.PayloadMissingError` when the payload is
    absent, so a missing file is never shown as an empty one.
    """
    data = vault.read_file(record)
    truncated = len(data) > limit
    chunk = data[:limit]
    try:
        return chunk.decode("utf-8"), truncated
    except UnicodeDecodeError:
        return chunk.decode("utf-8", errors="replace"), truncated
