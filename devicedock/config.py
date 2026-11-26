# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Application settings persisted as JSON under the XDG config directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR_NAME = "devicedock"
#: Directory name used by releases before the Device Dock rename.
LEGACY_APP_DIR_NAME = "appledevices"


def _xdg_base(variable: str, fallback: Path) -> Path:
    return Path(os.environ.get(variable, fallback))


def claim_directory(base: Path, name: str = APP_DIR_NAME) -> Path:
    """Return ``base/name``, taking over a directory created by the former name.

    The legacy directory is renamed rather than copied: renaming is atomic on one
    filesystem, so a large backup library is never duplicated and an interrupted upgrade
    leaves either the old or the new directory intact. If the rename is impossible (for
    example the two paths are on different filesystems), the legacy directory keeps being
    used so that settings, logs and backups never become unreachable.
    """
    target = base / name
    legacy = base / LEGACY_APP_DIR_NAME
    if target.exists() or not legacy.is_dir():
        return target
    try:
        legacy.rename(target)
    except OSError:
        return legacy
    return target


_CONFIG_BASE = _xdg_base("XDG_CONFIG_HOME", Path.home() / ".config")
_STATE_BASE = _xdg_base("XDG_STATE_HOME", Path.home() / ".local" / "state")
_CACHE_BASE = _xdg_base("XDG_CACHE_HOME", Path.home() / ".cache")
_DATA_BASE = _xdg_base("XDG_DATA_HOME", Path.home() / ".local" / "share")

APP_CONFIG_DIR = claim_directory(_CONFIG_BASE)
APP_STATE_DIR = claim_directory(_STATE_BASE)
APP_CACHE_DIR = claim_directory(_CACHE_BASE)
APP_DATA_DIR = claim_directory(_DATA_BASE)
SETTINGS_PATH = APP_CONFIG_DIR / "settings.json"

# The application's own backup library, created on install and on first run. It is the
# default destination for "Create backup" so a backup never needs a folder chooser.
DEFAULT_BACKUP_DIR = APP_DATA_DIR / "Backups"

# Locations that other tools (iTunes via Wine, Finder-style layouts) may already use.
# They are only offered when they exist, so the settings list never shows phantom rows.
_LEGACY_BACKUP_DIRS = (
    Path.home() / "Apple" / "MobileSync" / "Backup",
    Path.home() / "Library" / "MobileSync" / "Backup",
)


def _holds_backups(directory: Path) -> bool:
    """True when the folder is a backup itself or contains backup folders."""
    try:
        if (directory / "Manifest.plist").is_file() or (directory / "Manifest.db").is_file():
            return True
        return any(child.is_dir() and ((child / "Manifest.plist").is_file()
                                       or (child / "Manifest.db").is_file())
                   for child in directory.iterdir())
    except OSError:
        return False


def default_backup_directories() -> list[str]:
    """Return the default scan list: our own library first, then legacy folders with backups.

    A legacy folder is only offered when it actually holds a backup, so the settings list
    never shows a location that exists but is empty (or one that does not exist at all).
    """
    directories = [DEFAULT_BACKUP_DIR]
    directories.extend(path for path in _LEGACY_BACKUP_DIRS if _holds_backups(path))
    return [str(path) for path in directories]


def ensure_backup_directory(directory: str | Path) -> Path:
    """Create a backup directory if it does not exist and return it."""
    path = Path(directory).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_default_directories() -> list[str]:
    """Create the directories the application owns; returns any that could not be created."""
    problems: list[str] = []
    for directory in (APP_CONFIG_DIR, APP_STATE_DIR, APP_CACHE_DIR, DEFAULT_BACKUP_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            problems.append(str(directory))
    return problems


def free_space_text(directory: str | Path) -> str | None:
    """Human-readable free space of the filesystem holding ``directory``."""
    import shutil

    target = Path(directory).expanduser()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    return f"{usage.free / 1024 ** 3:.0f} GB free"


@dataclass
class Settings:
    backup_directories: list[str] = field(default_factory=default_backup_directories)
    backup_destination: str = str(DEFAULT_BACKUP_DIR)
    export_directory: str = str(Path.home() / "DeviceDockExports")
    theme: str = "system"  # system | light | dark
    store_passwords: bool = False
    mask_identifiers: bool = True
    log_level: str = "INFO"
    auto_refresh_seconds: int = 5

    # --- persistence -------------------------------------------------
    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        settings = cls(**{k: v for k, v in raw.items() if k in known})
        settings._migrate()
        return settings

    def _relocate_legacy_data(self, path: str) -> str:
        """Point a stored path inside the former data directory at the current one."""
        legacy = str(_DATA_BASE / LEGACY_APP_DIR_NAME)
        if path == legacy or path.startswith(legacy + os.sep):
            return str(APP_DATA_DIR) + path[len(legacy):]
        return path

    def _migrate(self) -> None:
        """Repair stored locations after the rename and drop defaults that do not exist."""
        self.backup_directories = [self._relocate_legacy_data(d)
                                   for d in self.backup_directories]
        if self.backup_destination:
            self.backup_destination = self._relocate_legacy_data(self.backup_destination)
        legacy = {str(p) for p in _LEGACY_BACKUP_DIRS}
        kept = [d for d in self.backup_directories if not (d in legacy and not Path(d).is_dir())]
        if str(DEFAULT_BACKUP_DIR) not in kept:
            kept.insert(0, str(DEFAULT_BACKUP_DIR))
        self.backup_directories = kept
        if not self.backup_destination:
            self.backup_destination = str(DEFAULT_BACKUP_DIR)

    def save(self, path: Path = SETTINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def add_backup_directory(self, directory: str) -> None:
        directory = str(Path(directory).expanduser())
        if directory not in self.backup_directories:
            self.backup_directories.append(directory)

    def remove_backup_directory(self, directory: str) -> None:
        if directory in self.backup_directories:
            self.backup_directories.remove(directory)

    @property
    def default_destination(self) -> Path:
        """Destination offered by "Create backup" before the user changes it."""
        return Path(self.backup_destination or DEFAULT_BACKUP_DIR).expanduser()
