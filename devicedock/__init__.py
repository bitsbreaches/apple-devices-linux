"""Device Dock — native Linux application for managing iPhones, files and local backups.

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 Device Dock Contributors

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, version 3 only.
It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License in LICENSE for details.
"""

__version__ = "1.2.0"

COPYRIGHT_YEAR = "2026"
COPYRIGHT_HOLDER = "Device Dock Contributors"
COPYRIGHT_NOTICE = f"Copyright © {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}."
LICENSE_NAME = "GNU General Public License v3.0 only (GPL-3.0-only)"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"

APP_ID = "io.github.bitsbreaches.DeviceDock"
APP_NAME = "Device Dock"
APP_SUMMARY = "Native Linux application for managing iPhones, files and local backups"


def _add_pycryptodome_compatibility() -> None:
    """Alias Debian's ``Cryptodome`` to ``Crypto`` for ``iphone_backup_decrypt``.

    Upstream PyCryptodome installs ``Crypto``, but Debian and Ubuntu ship the same library
    as ``Cryptodome``. ``iphone_backup_decrypt`` imports the upstream name, so without this
    alias every encrypted-backup operation fails on a distribution install.
    """
    import importlib
    import sys

    if "Crypto" in sys.modules:
        return
    try:
        importlib.import_module("Crypto")
    except ImportError:
        try:
            cryptodome = importlib.import_module("Cryptodome")
        except ImportError:
            return
        sys.modules["Crypto"] = cryptodome
        # The decryption library asks for Crypto.Hash.SHA1/SHA256 and
        # Crypto.Protocol.KDF. Load those aliases eagerly so their attributes exist even
        # though Cryptodome's own modules import each other under the Cryptodome name.
        for name in ("Crypto.Cipher.AES", "Crypto.Hash", "Crypto.Hash.SHA1",
                     "Crypto.Hash.SHA256", "Crypto.Protocol.KDF"):
            try:
                importlib.import_module(name)
            except ImportError:
                continue


def _migrate_legacy_directories() -> None:
    """Move data written by releases before the rename to the Device Dock directories.

    The move lives in :mod:`devicedock.config`, which resolves every path when imported;
    importing it here runs it once, before any other module reads a settings, cache, state
    or data path, whatever entry point started the process.
    """
    import importlib

    importlib.import_module(f"{__name__}.config")


_add_pycryptodome_compatibility()
_migrate_legacy_directories()
