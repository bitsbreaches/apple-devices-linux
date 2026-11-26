# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backup-location defaults and migration."""

from __future__ import annotations

import json
from pathlib import Path

from devicedock import config


def test_default_destination_is_under_the_xdg_data_home() -> None:
    destination = config.DEFAULT_BACKUP_DIR
    assert destination.name == "Backups"
    assert destination.parent.name == "devicedock"
    assert ".local" in str(destination) or "XDG_DATA_HOME" in str(destination)


def test_missing_legacy_defaults_are_dropped_on_load(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    legacy = str(Path.home() / "Apple" / "MobileSync" / "Backup")
    settings_path.write_text(json.dumps({
        "backup_directories": [legacy, "/srv/keep-me"],
        "backup_destination": "",
    }), encoding="utf-8")
    settings = config.Settings.load(settings_path)
    assert str(config.DEFAULT_BACKUP_DIR) in settings.backup_directories
    assert "/srv/keep-me" in settings.backup_directories
    if not Path(legacy).is_dir():
        assert legacy not in settings.backup_directories
    assert settings.backup_destination == str(config.DEFAULT_BACKUP_DIR)


def test_ensure_backup_directory_creates_nested_paths(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "Backups"
    created = config.ensure_backup_directory(target)
    assert created.is_dir()


def test_free_space_text_reports_a_size(tmp_path: Path) -> None:
    text = config.free_space_text(tmp_path)
    assert text is not None and "GB free" in text


def test_default_destination_property_follows_the_setting(tmp_path: Path) -> None:
    settings = config.Settings(backup_destination=str(tmp_path / "elsewhere"))
    assert settings.default_destination == tmp_path / "elsewhere"


def test_claim_directory_moves_the_former_application_directory(tmp_path: Path) -> None:
    legacy = tmp_path / "appledevices"
    (legacy / "Backups" / "00008110-000A").mkdir(parents=True)
    (legacy / "Backups" / "00008110-000A" / "Manifest.plist").write_text("{}")
    (legacy / "settings.json").write_text('{"theme": "dark"}')

    claimed = config.claim_directory(tmp_path)

    assert claimed == tmp_path / "devicedock"
    assert not legacy.exists(), "the old directory must not be left behind"
    assert (claimed / "settings.json").read_text() == '{"theme": "dark"}'
    assert (claimed / "Backups" / "00008110-000A" / "Manifest.plist").is_file()


def test_claim_directory_is_repeatable_and_never_merges(tmp_path: Path) -> None:
    legacy = tmp_path / "appledevices"
    legacy.mkdir()
    (legacy / "old.txt").write_text("old", encoding="utf-8")
    current = tmp_path / "devicedock"
    current.mkdir()
    (current / "new.txt").write_text("new", encoding="utf-8")

    assert config.claim_directory(tmp_path) == current
    assert (legacy / "old.txt").read_text(encoding="utf-8") == "old"
    assert (current / "new.txt").read_text(encoding="utf-8") == "new"

    moved = tmp_path / "other"
    moved.mkdir()
    (moved / "appledevices").mkdir()
    assert config.claim_directory(moved) == moved / "devicedock"
    assert config.claim_directory(moved) == moved / "devicedock"


def test_stored_paths_in_the_former_data_directory_are_relocated(tmp_path: Path) -> None:
    legacy = config._DATA_BASE / config.LEGACY_APP_DIR_NAME
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "backup_directories": [str(legacy / "Backups"), "/srv/keep-me"],
        "backup_destination": str(legacy / "Backups"),
    }), encoding="utf-8")

    settings = config.Settings.load(settings_path)

    assert str(config.APP_DATA_DIR / "Backups") in settings.backup_directories
    assert "/srv/keep-me" in settings.backup_directories
    assert settings.backup_destination == str(config.APP_DATA_DIR / "Backups")
