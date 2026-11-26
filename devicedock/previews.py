# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Bounded preview preparation shared by phone and backup browsers."""

from __future__ import annotations

import hashlib
import html.parser
import json
import mimetypes
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .asyncutil import Job
from .backup.vault import BackupVault
from .models import FileRecord

TEXT_LIMIT = 256 * 1024
ARCHIVE_MEMBER_LIMIT = 2 * 1024 * 1024

#: Optional external preview helpers: executable -> what it improves.
PREVIEW_TOOLS = {
    "pdftoppm": "PDF first-page preview",
    "ffmpeg": "video thumbnails",
    "ffprobe": "media duration and tags",
}


def preview_tool_status() -> dict[str, bool]:
    """Which optional preview helpers are installed (missing ones degrade, never fail)."""
    return {name: shutil.which(name) is not None for name in PREVIEW_TOOLS}


@dataclass(frozen=True)
class PreviewDocument:
    kind: str
    path: Path | None = None
    text: str = ""
    message: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    thumbnail: Path | None = None


class PreviewCache:
    """Private, per-session plaintext cache; ``close`` removes its contents."""

    def __init__(self, parent: Path) -> None:
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        self.directory = Path(tempfile.mkdtemp(prefix="preview-", dir=parent))
        self.directory.chmod(0o700)

    def target(self, key: str, filename: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
        suffix = PurePosixPath(filename).suffix[:16]
        return self.directory / f"{digest}{suffix}"

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def stage_backup_file(cache: PreviewCache, vault: BackupVault, record: FileRecord,
                      job: Job | None = None) -> Path:
    if job is not None:
        job.check_cancelled()
    target = cache.target(f"backup:{vault.info.path}:{record.file_id}", record.relative_path)
    if target.is_file() and (record.size is None or target.stat().st_size == record.size):
        return target
    vault.copy_file(record, target, job=job)
    return target


def stage_phone_file(cache: PreviewCache, udid: str, remote_path: str,
                     bundle_id: str | None = None, job: Job | None = None,
                     expected_size: int | None = None, modified: str = "") -> Path:
    from .devices.media_files import copy_file_for_preview

    if job is not None:
        job.check_cancelled()
    key = f"phone:{udid}:{bundle_id or ''}:{remote_path}:{expected_size}:{modified}"
    target = cache.target(key, remote_path)
    if not target.is_file() or (expected_size is not None and target.stat().st_size != expected_size):
        copy_file_for_preview(udid, remote_path, target, bundle_id, job=job,
                              expected_size=expected_size)
    return target


def prepare_preview(path: Path, job: Job | None = None) -> PreviewDocument:
    """Prepare a small preview or a media path without reading large files into memory."""
    path = Path(path)
    suffix = path.suffix.casefold()
    mime, _encoding = mimetypes.guess_type(path.name)
    size = path.stat().st_size if path.is_file() else None
    metadata = (("Type", mime or (suffix[1:].upper() + " file" if suffix else "File")),
                ("Size", _format_size(size)))

    if mime and mime.startswith("image/") or suffix in {".dng", ".heic", ".heif"}:
        return PreviewDocument("image", path=path, metadata=metadata)
    if mime and (mime.startswith("video/") or mime.startswith("audio/")):
        thumbnail = _video_thumbnail(path, job) if mime.startswith("video/") else None
        extra = _media_metadata(path, job)
        return PreviewDocument("video" if mime.startswith("video/") else "audio",
                               path=path, metadata=metadata + extra, thumbnail=thumbnail)
    if suffix == ".pdf" or mime == "application/pdf":
        image = _pdf_first_page(path, job)
        if image:
            return PreviewDocument("pdf", path=path, metadata=metadata, thumbnail=image)
        return PreviewDocument("unsupported", path=path, metadata=metadata,
                               message="PDF preview needs the optional Poppler pdftoppm tool. Export the file to open it in a PDF viewer.")
    if suffix == ".docx":
        text = _docx_text(path)
        if text is not None:
            return PreviewDocument("text", path=path, text=text, metadata=metadata)
    if suffix == ".epub":
        text = _epub_text(path)
        if text is not None:
            return PreviewDocument("text", path=path, text=text, metadata=metadata)
    if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".plist", ".rtf"} or (mime and mime.startswith("text/")):
        try:
            with path.open("rb") as stream:
                data = stream.read(TEXT_LIMIT + 1)
        except OSError as exc:
            return PreviewDocument("unsupported", path=path, metadata=metadata,
                                   message=f"Could not read this file: {exc}")
        truncated = len(data) > TEXT_LIMIT
        text = data[:TEXT_LIMIT].decode("utf-8", errors="replace")
        if suffix in {".html", ".htm"}:
            text = _strip_html(text)
        elif suffix == ".rtf":
            text = _strip_rtf(text)
        if truncated:
            text += "\n\nPreview truncated at 256 KB. Export to read the rest."
        return PreviewDocument("text", path=path, text=text, metadata=metadata)
    return PreviewDocument("unsupported", path=path, metadata=metadata,
                           message=f"No inline preview is available for {mime or suffix or 'this file type'}. Export the original file to open it with another application.")


def _format_size(size: int | None) -> str:
    if size is None:
        return "Size unavailable"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _pdf_first_page(path: Path, job: Job | None) -> Path | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    if job is not None:
        job.check_cancelled()
    target = path.with_name(path.name + ".page-1.png")
    try:
        result = subprocess.run(
            [executable, "-f", "1", "-l", "1", "-scale-to", "1000", "-singlefile",
             "-png", str(path), str(target.with_suffix(""))],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return target if result.returncode == 0 and target.is_file() else None


def _video_thumbnail(path: Path, job: Job | None) -> Path | None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        return None
    if job is not None:
        job.check_cancelled()
    target = path.with_name(path.name + ".thumbnail.png")
    try:
        result = subprocess.run(
            [executable, "-nostdin", "-v", "error", "-threads", "1", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
             "-y", str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return target if result.returncode == 0 and target.is_file() else None


def _media_metadata(path: Path, job: Job | None) -> tuple[tuple[str, str], ...]:
    executable = shutil.which("ffprobe")
    if executable is None:
        return ()
    if job is not None:
        job.check_cancelled()
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries",
             "format=duration:format_tags=title,artist,album", "-of", "json", str(path)],
            capture_output=True, timeout=8, check=False,
        )
        if result.returncode != 0:
            return ()
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})
        rows = [(label, str(tags[key])) for key, label in
                (("title", "Title"), ("artist", "Artist"), ("album", "Album")) if tags.get(key)]
        try:
            duration = max(0, int(float(fmt.get("duration", 0))))
            rows.append(("Duration", f"{duration // 60}:{duration % 60:02d}"))
        except (TypeError, ValueError):
            pass
        return tuple(rows)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return ()


def _docx_text(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            member = archive.getinfo("word/document.xml")
            if member.file_size > ARCHIVE_MEMBER_LIMIT:
                return "Document text preview is too large. Export the file to read it."
            xml = archive.read(member)
        root = ET.fromstring(xml)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return None
    chunks = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
    return "\n".join(chunks)[:TEXT_LIMIT]


def _epub_text(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(node.attrib["full-path"] for node in container.iter()
                            if node.tag.endswith("rootfile") and "full-path" in node.attrib)
            package = ET.fromstring(archive.read(rootfile))
            manifest = {node.attrib["id"]: node.attrib["href"] for node in package.iter()
                        if node.tag.endswith("item") and "id" in node.attrib and "href" in node.attrib}
            spine = [node.attrib["idref"] for node in package.iter()
                     if node.tag.endswith("itemref") and "idref" in node.attrib]
            parser = _TextCollector()
            base = PurePosixPath(rootfile).parent
            for item_id in spine:
                if item_id not in manifest:
                    continue
                member = str(base / manifest[item_id])
                info = archive.getinfo(member)
                if info.file_size > ARCHIVE_MEMBER_LIMIT:
                    continue
                parser.feed(archive.read(info).decode("utf-8", errors="replace"))
                parser.feed("\n\n")
                if len(parser.text) >= TEXT_LIMIT:
                    break
    except (OSError, KeyError, StopIteration, zipfile.BadZipFile, ET.ParseError):
        return None
    return "".join(parser.text)[:TEXT_LIMIT]


class _TextCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text.append(data.strip())


def _strip_html(data: str) -> str:
    parser = _TextCollector()
    parser.feed(data)
    return "\n".join(parser.text)


def _strip_rtf(data: str) -> str:
    # Basic RTF control-word removal; this is a readable text fallback, not a layout renderer.
    import re

    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", data)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return text.replace("{", "").replace("}", "")

