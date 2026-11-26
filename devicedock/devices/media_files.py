# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Browse and transfer user files through libimobiledevice's ifuse filesystem."""

from __future__ import annotations

import os
import csv
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from ..fileops import unique_destination

PHOTO = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff"}
VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
MUSIC = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aif", ".aiff", ".caf"}
PODCAST = {".m4b", ".mp3", ".m4a", ".aac", ".ogg"}
BOOK = {".epub", ".mobi", ".azw", ".azw3", ".ibooks"}
DOCUMENT = {
    ".pdf", ".txt", ".rtf", ".md", ".csv", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    *BOOK,
}
SUFFIX_KIND = {suffix: kind for kind, suffixes in (
    ("Photo", PHOTO), ("Video", VIDEO), ("Music", MUSIC | {".m4b"}),
    ("Document", DOCUMENT),
) for suffix in suffixes}
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]+$")
# ifuse 1.x prints a CSV header before the app rows. Both are valid bundle-ID-shaped
# tokens, so they must be skipped explicitly rather than treated as installed apps.
_APP_LIST_HEADER = {"CFBundleIdentifier", "CFBundleDisplayName"}
# Fallback labels for apps whose CFBundleDisplayName is missing or identical to the
# bundle identifier. A display name reported by ifuse always takes precedence.
APP_DISPLAY_NAMES = {
    "com.google.chrome.ios": "Chrome",
    "com.brave.ios.browser": "Brave",
    "com.adobe.reader": "Adobe Acrobat",
    "com.microsoft.msedge": "Microsoft Edge",
    "org.mozilla.ios.Firefox": "Firefox",
    "com.google.Drive": "Google Drive",
    "com.getdropbox.Dropbox": "Dropbox",
    "com.apple.iWork.Pages": "Pages",
    "com.apple.iWork.Numbers": "Numbers",
    "com.apple.iWork.Keynote": "Keynote",
    "com.microsoft.Office.Word": "Word",
    "com.microsoft.Office.Excel": "Excel",
    "com.microsoft.Office.Powerpoint": "PowerPoint",
    "com.apple.mobilenotes": "Notes",
    "com.apple.VoiceMemos": "Voice Memos",
    "org.videolan.vlc-ios": "VLC",
    # Only shown if iOS ever reports these through UIFileSharingEnabled; they are never
    # synthesised, because iCloud Drive and Files are system providers rather than
    # house_arrest file-sharing apps.
    "com.apple.CloudDocs": "iCloud Drive",
    "com.apple.DocumentsApp": "Files",
}
# Folder names shown to the user. DCIM remains the on-device path; only the label changes.
FOLDER_DISPLAY_NAMES = {
    "dcim": "Photos",
    "itunes_control": "Media",
    "documents": "Documents",
}

_HIDDEN_PHONE_DIRS = {
    ".trash", "caches", "library", "preferences", "tmp", "temporaryitems",
    "photodata", "mediaanalysis", "enhancedaudiosharedkeys", "airfair", "airlock",
    "espresso", "deferred",
}
_SUPPORT_SUFFIXES = {
    ".json", ".xml", ".plist", ".db", ".sqlite", ".sqlite3", ".log", ".conf",
    ".ini", ".lock", ".shm", ".wal", ".journal",
}
log = logging.getLogger(__name__)


def _ifuse_mount_args(executable: str, mountpoint: Path, udid: str,
                      bundle_id: str | None = None) -> list[str]:
    """Build arguments using ifuse's documented ``MOUNTPOINT [OPTIONS]`` order."""
    args = [executable, str(mountpoint), "--udid", udid]
    if bundle_id:
        args.extend(["--documents", bundle_id])
    return args


def file_browser_preflight(udid: str, *, pairing_verified: bool = False) -> str | None:
    """Return actionable setup/connection guidance, or ``None`` when ready to mount."""
    if shutil.which("ifuse") is None:
        return (
            "iPhone File Browsing Unavailable\n\n"
            "The required `ifuse` program is not installed. On Ubuntu or Debian, install the "
            "live-browsing prerequisites with:\n\n"
            "sudo apt install ifuse libimobiledevice-utils usbmuxd fuse3"
        )
    if not (shutil.which("fusermount3") or shutil.which("fusermount")):
        return "The FUSE mount helper is missing. Install it with `sudo apt install fuse3`, then retry."
    if not Path("/dev/fuse").exists():
        return "FUSE is unavailable because `/dev/fuse` does not exist. Install `fuse3` and check that the Linux FUSE module is enabled."
    if not os.access(tempfile.gettempdir(), os.W_OK | os.X_OK):
        return "The system temporary directory is not writable by this user, so a private iPhone mount cannot be created."

    from . import libimobile

    if not libimobile.usbmuxd_socket_present():
        return "The usbmuxd service is not available. Start it with `sudo systemctl start usbmuxd`, then retry."
    try:
        devices = libimobile.list_device_ids()
    except Exception as exc:
        detail = str(exc).strip()
        return f"Could not contact usbmuxd to detect the iPhone. Check that usbmuxd is running and accessible. {detail}"
    if udid not in devices:
        return "This iPhone is no longer visible to usbmuxd. Reconnect it, unlock it, and retry."

    if pairing_verified:
        return None
    check = libimobile.validate_pairing(udid)
    if check.state is libimobile.PairingState.TRUSTED:
        return None
    if check.state is libimobile.PairingState.PAIRING_REQUIRED:
        return (
            "This iPhone has no valid pairing with this computer. Unlock it and approve "
            "“Trust This Computer” on the phone, then retry. Device Dock will not create "
            "a pairing automatically."
        )
    if check.state is libimobile.PairingState.LOCKED:
        return "The iPhone is locked. Unlock it with its passcode, then retry file browsing."
    if check.state is libimobile.PairingState.UNAVAILABLE:
        return f"The iPhone's Lockdown service is unavailable. Reconnect and unlock the phone, then retry. {check.detail}"
    return f"Could not validate the existing iPhone pairing. No pairing change was made. {check.detail}"


def _app_name(bundle_id: str, display_name: str = "") -> str:
    """Prefer the app's own name, then a known label, then the bundle identifier."""
    name = (display_name or "").strip()
    if name and name != bundle_id:
        return name
    return APP_DISPLAY_NAMES.get(bundle_id) or bundle_id


def display_folder_name(name: str) -> str:
    """User-facing label for an on-device folder (DCIM is presented as Photos)."""
    return FOLDER_DISPLAY_NAMES.get((name or "").casefold(), name)


@dataclass(frozen=True)
class PhoneFile:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    kind: str = ""
    bundle_id: str | None = None
    is_source: bool = False
    modified: str = ""


class PhoneMount(AbstractContextManager):
    """Temporarily mount the paired device's media or shared Documents directory."""

    def __init__(self, udid: str, bundle_id: str | None = None) -> None:
        self.udid = udid
        self.bundle_id = bundle_id
        self.tempdir: Path | None = None
        self.mountpoint: Path | None = None
        self.process: subprocess.Popen | None = None
        self._stderr = None

    def __enter__(self) -> Path:
        ifuse = shutil.which("ifuse")
        if ifuse is None:
            raise RuntimeError(
                "iPhone file browsing is unavailable because ifuse is not installed.\n\n"
                "On Ubuntu or Debian, install the prerequisites with:\n"
                "sudo apt install ifuse libimobiledevice-utils usbmuxd fuse3"
            )
        if not (shutil.which("fusermount3") or shutil.which("fusermount")):
            raise RuntimeError(
                "The FUSE unmount helper is missing. Install fuse3 with:\n"
                "sudo apt install fuse3"
            )
        try:
            self.tempdir = Path(tempfile.mkdtemp(prefix="devicedock-phone-"))
            self.tempdir.chmod(0o700)
            self.mountpoint = self.tempdir / "mount"
            self.mountpoint.mkdir(mode=0o700)
            self._stderr = (self.tempdir / "ifuse.log").open("wb")
        except OSError as exc:
            self.__exit__(None, None, None)
            raise RuntimeError(
                f"Could not create a private iPhone mount under {tempfile.gettempdir()}: "
                f"{exc.strerror or exc}. Check that this directory is writable by your user."
            ) from exc
        args = _ifuse_mount_args(ifuse, self.mountpoint, self.udid, self.bundle_id)
        try:
            self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                            stderr=self._stderr, close_fds=True)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    next(self.mountpoint.iterdir(), None)
                    if os.path.ismount(self.mountpoint):
                        return self.mountpoint
                except OSError:
                    pass
                # ifuse can daemonize after a successful FUSE mount. The mount is the
                # source of truth; an exited launcher is only an error when no mount exists.
                if self.process.poll() is not None:
                    raise self._mount_error()
                time.sleep(0.1)
            raise self._mount_error("Timed out waiting for the phone file system to mount.")
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _mount_error(self, prefix: str = "Could not mount the phone file system") -> RuntimeError:
        if self._stderr is not None:
            self._stderr.flush()
        detail = ""
        if self.tempdir is not None:
            try:
                detail = (self.tempdir / "ifuse.log").read_text(errors="replace").strip()
            except OSError:
                pass
        folded = detail.casefold()
        if any(token in folded for token in ("not paired", "invalid hostid", "pairing dialog", "pair record")):
            prefix = ("This iPhone reported no valid pairing with this computer. Unlock it and approve "
                      "“Trust This Computer” if the prompt appears. If you believe it is already paired, "
                      "run `idevicepair validate` to inspect the pairing before creating another one.")
        elif any(token in folded for token in ("locked", "passcode is set", "password protected")):
            prefix = "The iPhone is locked. Unlock it with its passcode, then retry file browsing."
        elif any(token in folded for token in ("trust this computer", "pairing dialog", "user denied")):
            prefix = "The iPhone is waiting for Trust This Computer approval. Approve the prompt on the phone once, then retry."
        elif "lockdown" in folded:
            prefix = "The iPhone's Lockdown service could not be reached. Reconnect and unlock the phone, then retry."
        elif any(token in folded for token in ("no device", "device not found", "disconnected", "not connected")):
            prefix = "The iPhone disconnected or is no longer visible to usbmuxd. Reconnect it, unlock it, and retry."
        elif any(token in folded for token in ("permission denied", "fuse device", "/dev/fuse", "fusermount")):
            prefix = ("FUSE could not mount the iPhone. Install `fuse3`, confirm `/dev/fuse` is available "
                      "to your user, and retry. Do not run Device Dock with sudo.")
            if detail:
                prefix += f"\n\nMount detail: {detail[-800:]}"
        elif detail:
            prefix += f": {detail[-1200:]}"
        else:
            prefix += ". Unlock the phone, confirm Trust This Computer, and check that FUSE is available."
        return RuntimeError(prefix)

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> bool:
        unmounted = True
        if self.mountpoint is not None and os.path.ismount(self.mountpoint):
            unmount = shutil.which("fusermount3") or shutil.which("fusermount")
            if unmount:
                try:
                    subprocess.run([unmount, "-u", str(self.mountpoint)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, check=False)
                    if os.path.ismount(self.mountpoint):
                        subprocess.run([unmount, "-uz", str(self.mountpoint)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10, check=False)
                except (OSError, subprocess.SubprocessError):
                    log.exception("Could not unmount the private iPhone mount")
            unmounted = not os.path.ismount(self.mountpoint)
        if self.process is not None:
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        if self._stderr is not None:
            self._stderr.close()
        if self.tempdir is not None and unmounted:
            shutil.rmtree(self.tempdir, ignore_errors=True)
        elif self.tempdir is not None:
            log.error("Leaving still-mounted iPhone mountpoint in place: %s", self.mountpoint)
        return False


def shared_document_apps(udid: str) -> list[tuple[str, str]]:
    ifuse = shutil.which("ifuse")
    if ifuse is None:
        raise RuntimeError(
            "Listing shared iPhone app documents requires ifuse. Install it with "
            "`sudo apt install ifuse libimobiledevice-utils usbmuxd fuse3`, then retry."
        )
    proc = subprocess.run([ifuse, "--udid", udid, "--list-apps"], capture_output=True,
                          text=True, timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "ifuse could not list shared apps")
    apps = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            fields = next(csv.reader([line], skipinitialspace=True))
        except (csv.Error, StopIteration):
            fields = []
        bundle_id = fields[0].strip() if fields else ""
        if (not _APP_ID.fullmatch(bundle_id) or bundle_id in _APP_LIST_HEADER
                or bundle_id in seen):
            continue
        name = fields[-1].strip() if len(fields) > 1 else ""
        apps.append((bundle_id, _app_name(bundle_id, name)))
        seen.add(bundle_id)
    return sorted(apps, key=lambda app: (app[1].casefold(), app[0]))


def shared_app_folders(apps: list[tuple[str, str]],
                       existing_names: list[str] | None = None) -> list[PhoneFile]:
    """Folders for apps that genuinely expose a shared Documents area.

    ``ifuse --list-apps`` only lists apps with ``UIFileSharingEnabled``, so each entry is
    reachable through ``--documents``. No other system locations (iCloud Drive, Files,
    Safari Downloads) are invented here.
    """
    seen = {(name or "").casefold() for name in existing_names or ()}
    folders = []
    for bundle_id, name in apps:
        label = _app_name(bundle_id, name)
        if label.casefold() in seen:
            continue
        seen.add(label.casefold())
        folders.append(PhoneFile(label, "/", True, kind="Folder", bundle_id=bundle_id,
                                 is_source=True))
    return folders


def _path(root: Path, path: str) -> Path:
    relative = Path(path.lstrip("/"))
    target = (root / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError("Phone path escapes the mounted user-file area")
    return target


def list_files(udid: str, path: str, kind: str = "All",
               bundle_id: str | None = None) -> list[PhoneFile]:
    with PhoneMount(udid, bundle_id) as root:
        folder = _path(root, path)
        entries = []
        # A category selected at its root means search the user area recursively. Media then
        # appears immediately instead of being hidden behind DCIM or app-created folders.
        # Documents retain their first folder level as navigation.
        if kind != "All":
            # Inside a Downloads folder, show every supported user file, not only documents.
            download_scope = kind == "Document" and any(
                part.casefold() == "downloads" for part in Path(path).parts
            )
            top_folders: dict[str, Path] = {}
            for parent, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = [name for name in dirs
                           if not name.startswith(".")
                           and name.casefold() not in _HIDDEN_PHONE_DIRS
                           and (kind in {"Music", "Podcast", "Book"}
                                or name.casefold() != "itunes_control")
                           and not (Path(parent) / name).is_symlink()]
                for name in files:
                    item = Path(parent) / name
                    if (item.is_symlink() or item.name.startswith(".")
                            or item.suffix.lower() in _SUPPORT_SUFFIXES):
                        continue
                    suffix = item.suffix.lower()
                    category = SUFFIX_KIND.get(suffix, "")
                    if suffix in PODCAST and "podcast" in item.as_posix().casefold():
                        category = "Podcast"
                    elif suffix in BOOK:
                        category = "Book"
                    relative = item.relative_to(folder)
                    top = relative.parts[0] if len(relative.parts) > 1 else None
                    # A real Downloads folder stays reachable under Documents even when it
                    # holds photos, videos or other supported downloads. It is only listed
                    # when such a folder actually exists and contains supported files.
                    downloads_folder = (kind == "Document" and top is not None
                                        and top.casefold() == "downloads")
                    if not category or (category != kind and not download_scope
                                        and not downloads_folder):
                        continue
                    if kind == "Document" and top is not None:
                        top_folders[top] = folder / top
                    else:
                        stat = item.stat()
                        entries.append(PhoneFile(
                            item.name, "/" + item.relative_to(root).as_posix(), False,
                            stat.st_size, category, bundle_id, False,
                            _mtime_text(stat.st_mtime),
                        ))
            for name, item in top_folders.items():
                stat = item.stat()
                entries.append(PhoneFile(
                    name, "/" + item.relative_to(root).as_posix(), True, 0,
                    "Folder", bundle_id, False, _mtime_text(stat.st_mtime),
                ))
            return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))
        for item in folder.iterdir():
            try:
                is_dir = item.is_dir()
                suffix = item.suffix.lower()
                category = SUFFIX_KIND.get(suffix, "")
                if suffix in PODCAST and "podcast" in item.as_posix().casefold():
                    category = "Podcast"
                elif suffix in BOOK:
                    category = "Book"
                if is_dir:
                    if item.name.startswith(".") or item.name.casefold() in _HIDDEN_PHONE_DIRS:
                        continue
                    stat = item.stat()
                    entries.append(PhoneFile(item.name, "/" + item.relative_to(root).as_posix(),
                                             True, 0, "Folder", bundle_id, False,
                                             _mtime_text(stat.st_mtime)))
                elif not item.is_symlink() and suffix not in _SUPPORT_SUFFIXES:
                    stat = item.stat()
                    if not category:
                        continue
                    if kind != "All" and category != kind:
                        continue
                    size = stat.st_size
                    entries.append(PhoneFile(item.name, "/" + item.relative_to(root).as_posix(),
                                             False, size, category, bundle_id, False,
                                             _mtime_text(stat.st_mtime)))
            except OSError:
                continue
    return sorted(entries, key=lambda item: (not item.is_dir, item.name.casefold()))


def _mtime_text(timestamp: float) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def copy_file_for_preview(udid: str, path: str, destination: Path,
                         bundle_id: str | None = None, job=None,
                         expected_size: int | None = None) -> Path:
    """Stream one selected AFC file into the private preview cache."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with PhoneMount(udid, bundle_id) as root:
        source = _path(root, path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Only regular files can be previewed from the phone.")
        source_size = source.stat().st_size
        if expected_size is not None and source_size != expected_size:
            raise OSError("The phone file changed after it was listed. Refresh and try again.")
        temporary = destination.with_name(destination.name + ".part")
        copied = 0
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                while chunk := input_file.read(1024 * 1024):
                    if job is not None:
                        job.check_cancelled()
                    output_file.write(chunk)
                    copied += len(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
            if copied != source_size:
                raise OSError("The phone file changed while the preview was being read.")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return destination


def pull_items(udid: str, items: list[PhoneFile], destination: Path,
               bundle_id: str | None = None, job=None) -> list[Path]:
    """Copy selected files and folders, preserving folder structure without overwriting."""
    selected = items
    if not selected:
        raise ValueError("Select one or more files or folders to copy to this computer.")
    invalid = [file.name for file in selected
               if not file.is_dir
               and Path(file.name).suffix.lower() not in SUFFIX_KIND
               and (not bundle_id or Path(file.name).suffix.lower() in _SUPPORT_SUFFIXES)]
    if invalid:
        raise ValueError("The selection includes unsupported file types: " + ", ".join(invalid[:5]))
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    used: set[str] = set()
    completed_bytes = 0
    with PhoneMount(udid, bundle_id) as root:
        pending: list[tuple[Path, Path, str]] = []
        for file in selected:
            if job is not None:
                job.check_cancelled()
            source = _path(root, file.path)
            if file.is_dir:
                for parent, dirs, names in os.walk(source, followlinks=False):
                    dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
                    for name in names:
                        origin = Path(parent) / name
                        suffix = origin.suffix.lower()
                        if (origin.is_symlink() or suffix not in SUFFIX_KIND
                                and (not bundle_id or suffix in _SUPPORT_SUFFIXES)):
                            continue
                        relative = origin.relative_to(source)
                        pending.append((origin, Path(file.name) / relative, name))
            else:
                actual_size = source.stat().st_size
                if actual_size != file.size:
                    raise OSError(f"{file.name} changed after it was listed. Refresh and try again.")
                pending.append((source, Path(file.name), file.name))
        if not pending:
            raise ValueError("The selection contains no supported files to copy.")
        total_bytes = sum(source.stat().st_size for source, _relative, _name in pending)
        for index, (source, relative, display_name) in enumerate(pending, start=1):
            actual_size = source.stat().st_size
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target = unique_destination(target, used)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            current_bytes = 0
            try:
                temporary.parent.mkdir(parents=True, exist_ok=True)
                with source.open("rb") as input_file, temporary.open("xb") as output_file:
                    while chunk := input_file.read(1024 * 1024):
                        if job is not None:
                            job.check_cancelled()
                        output_file.write(chunk)
                        current_bytes += len(chunk)
                        if job is not None:
                            fraction = ((completed_bytes + current_bytes) / total_bytes
                                        if total_bytes else index / len(pending))
                            job.progress(fraction, f"Copying {display_name} ({current_bytes:,}/{actual_size:,} bytes)")
                    output_file.flush()
                    os.fsync(output_file.fileno())
                if current_bytes != actual_size:
                    raise OSError(f"Incomplete copy of {display_name} from the phone.")
                shutil.copystat(source, temporary, follow_symlinks=False)
                temporary.replace(target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            copied.append(target)
            used.add(target.as_posix().casefold())
            completed_bytes += current_bytes
    if job is not None:
        job.progress(1.0, f"Copied {len(copied):,} file(s)")
    return copied


def push_files(udid: str, files: list[Path], remote_folder: str,
               bundle_id: str | None = None, job=None) -> None:
    invalid = [path.name for path in files
               if path.suffix.lower() not in SUFFIX_KIND
               and (not bundle_id or path.suffix.lower() in _SUPPORT_SUFFIXES)]
    if invalid:
        raise ValueError("Only photos, videos, music and documents can be added to the phone: "
                         + ", ".join(invalid[:5]))
    with PhoneMount(udid, bundle_id) as root:
        folder = _path(root, remote_folder)
        total_bytes = sum(path.stat().st_size for path in files)
        completed_bytes = 0
        for index, path in enumerate(files, start=1):
            if job is not None:
                job.check_cancelled()
            target = unique_destination(folder / path.name)
            temporary = folder / f".{target.name}.{uuid.uuid4().hex}.part"
            current_bytes = 0
            try:
                with path.open("rb") as input_file, temporary.open("xb") as output_file:
                    while chunk := input_file.read(1024 * 1024):
                        if job is not None:
                            job.check_cancelled()
                        output_file.write(chunk)
                        current_bytes += len(chunk)
                        if job is not None:
                            fraction = ((completed_bytes + current_bytes) / total_bytes
                                        if total_bytes else index / len(files))
                            job.progress(fraction, f"Adding {path.name} to phone")
                    output_file.flush()
                    os.fsync(output_file.fileno())
                if current_bytes != path.stat().st_size:
                    raise OSError(f"{path.name} changed while being copied.")
                temporary.replace(target)
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            completed_bytes += current_bytes
    if job is not None:
        job.progress(1.0, f"Added {len(files):,} file(s) to the phone")

