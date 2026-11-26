# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backup-password storage through the freedesktop Secret Service.

Passwords are only persisted when the user enables it in Settings. The Secret Service
(libsecret: GNOME Keyring, KWallet's Secret Service bridge, KeePassXC, …) keeps them in the
session keyring — never in the app's JSON settings file, never in the log.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_Secret = None
_SCHEMA = None
_LEGACY_SCHEMA = None
_SCHEMA_NAME = "io.github.bitsbreaches.DeviceDock.BackupPassword"
#: Schema used by releases before the Device Dock rename; entries are migrated on read.
_LEGACY_SCHEMA_NAME = "io.github.appledevices.BackupPassword"


def _load() -> bool:
    global _Secret, _SCHEMA, _LEGACY_SCHEMA
    if _Secret is not None:
        return True
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        _Secret = Secret
        attributes = {"backup": Secret.SchemaAttributeType.STRING}
        _SCHEMA = Secret.Schema.new(_SCHEMA_NAME, Secret.SchemaFlags.NONE, attributes)
        _LEGACY_SCHEMA = Secret.Schema.new(_LEGACY_SCHEMA_NAME, Secret.SchemaFlags.NONE,
                                           attributes)
        return True
    except (ImportError, ValueError) as exc:  # typelib missing
        log.info("Secret Service unavailable: %s", exc)
        return False


def _lookup(schema, backup_key: str) -> str | None:
    return _Secret.password_lookup_sync(schema, {"backup": backup_key}, None)


def _clear(schema, backup_key: str) -> bool:
    return bool(_Secret.password_clear_sync(schema, {"backup": backup_key}, None))


def available() -> bool:
    return _load()


def store_password(backup_key: str, password: str) -> bool:
    """Persist ``password`` for ``backup_key`` (usually the backup UDID)."""
    if not _load():
        return False
    try:
        return bool(
            _Secret.password_store_sync(
                _SCHEMA,
                {"backup": backup_key},
                _Secret.COLLECTION_DEFAULT,
                f"Device Dock backup password for {backup_key}",
                password,
                None,
            )
        )
    except Exception as exc:  # noqa: BLE001 - keyring may be locked/absent
        log.warning("Could not store password in Secret Service: %s", exc)
        return False


def lookup_password(backup_key: str) -> str | None:
    if not _load():
        return None
    try:
        password = _lookup(_SCHEMA, backup_key)
        if password is not None:
            return password
        # Passwords stored by a release before the rename are moved to the new schema so
        # they stay available without the user re-entering them.
        password = _lookup(_LEGACY_SCHEMA, backup_key)
        if password is not None and store_password(backup_key, password):
            _clear(_LEGACY_SCHEMA, backup_key)
        return password
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read password from Secret Service: %s", exc)
        return None


def clear_password(backup_key: str) -> bool:
    if not _load():
        return False
    try:
        cleared = _clear(_SCHEMA, backup_key)
        if _LEGACY_SCHEMA is not None:
            cleared = _clear(_LEGACY_SCHEMA, backup_key) or cleared
        return cleared
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not clear password from Secret Service: %s", exc)
        return False
