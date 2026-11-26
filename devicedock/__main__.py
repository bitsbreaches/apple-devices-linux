"""Command-line identification and the graphical entry point.

Supports non-graphical flags (``--version``, ``--licence``, ``--third-party-notices``,
``--self-check``) so a distribution can be inspected and diagnosed without a display.

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 Device Dock Contributors
"""

from __future__ import annotations

import sys

USAGE = """Usage: device-dock [OPTION]

  -h, --help                 show this help and exit
  -V, --version              show version, copyright and licence and exit
      --licence, --license   print the GNU General Public License v3.0 and exit
      --third-party-notices  print third-party notices and exit
      --self-check           report the bundled dependencies and usbmuxd status and exit
      --backup DIR           with --self-check: unlock DIR to test the bundled engine
                             (password from DEVICE_DOCK_SELF_CHECK_PASSWORD)

With no options, the graphical application starts.

Copyright © 2026 Device Dock Contributors.
This is free software under the GNU General Public License v3.0 only; there is NO WARRANTY.
"""


def _print_version() -> int:
    from . import (APP_ID, APP_NAME, COPYRIGHT_NOTICE, LICENSE_NAME,
                   LICENSE_URL, __version__)

    print(f"{APP_NAME} {__version__}")
    print(f"Application ID: {APP_ID}")
    print(COPYRIGHT_NOTICE)
    print(f"Licence: {LICENSE_NAME}")
    print(f"Licence text: {LICENSE_URL}")
    print("This is free software: you are free to change and redistribute it. "
          "There is NO WARRANTY, to the extent permitted by law.")
    return 0


def _self_check(args: list[str]) -> int:
    """Report whether the bundled dependencies and the device transport are usable.

    Runs without a display so a broken installation can be diagnosed from a terminal; the
    Debian package uses it to prove that encrypted backups work without any system Python
    package. A missing decryption dependency is an installation defect, so it exits 1;
    usbmuxd is reported but tolerated, because it is only needed when a device is attached.

    ``--backup DIR`` additionally attempts to open a backup, taking its password from
    ``DEVICE_DOCK_SELF_CHECK_PASSWORD`` so it never appears in the process list. Exit
    status 2 means the backup could not be unlocked (wrong password or damaged backup).
    """
    import importlib
    import os
    import platform

    from . import APP_NAME, __version__
    from .devices import libimobile

    print(f"{APP_NAME} {__version__} self-check")
    print(f"  Python: {platform.python_version()}")
    failures = 0
    for label, module, display in (
            ("Decryption engine", "iphone_backup_decrypt", "iphone_backup_decrypt"),
            ("Crypto backend", "Crypto", "pycryptodome"),
            ("Cryptography", "cryptography", "cryptography")):
        try:
            imported = importlib.import_module(module)
        except ImportError as exc:
            print(f"  {label}: UNAVAILABLE ({exc})")
            failures += 1
        else:
            version = getattr(imported, "__version__", "")
            print(f"  {label}: {display}{f' {version}' if version else ''}")

    missing_tools = libimobile.missing_required_tools()
    if missing_tools:
        print(f"  libimobiledevice: MISSING ({', '.join(missing_tools)})")
    else:
        print("  libimobiledevice: idevice_id and ideviceinfo found")
    message = libimobile.usbmuxd_unavailable_message()
    if message is None:
        print("  usbmuxd: running")
    else:
        print(f"  usbmuxd: NOT AVAILABLE — {message}")

    if "--backup" in args:
        position = args.index("--backup")
        if position + 1 >= len(args):
            print("error: --backup needs a backup directory", file=sys.stderr)
            return 2
        status = _self_check_backup(args[position + 1],
                                    os.environ.get("DEVICE_DOCK_SELF_CHECK_PASSWORD"))
        if status:
            return status

    if failures:
        print("One or more bundled dependencies are missing; reinstall the device-dock "
              "package.")
        return 1
    return 0


def _self_check_backup(path: str, password: str | None) -> int:
    """Open a backup through the bundled engine; return 0 on success, 2 on failure."""
    from pathlib import Path

    from .backup import BackupVault, BadPasswordError, read_backup_info

    try:
        info = read_backup_info(Path(path))
        vault = BackupVault(info)
        try:
            vault.unlock(password)
            print(f"  Backup: {path}")
            print(f"  Encrypted: {'yes' if info.is_encrypted else 'no'}")
            print(f"  Unlocked: {vault.manifest().total_files()} files")
        finally:
            vault.close()
    except BadPasswordError:
        print(f"  Backup: {path}")
        print("  Password: REJECTED (incorrect backup password)")
        return 2
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        print(f"  Backup: {path}")
        print(f"  Backup error: {exc}")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv[1:])

    if "-h" in args or "--help" in args:
        print(USAGE, end="")
        return 0
    if "-V" in args or "--version" in args:
        return _print_version()
    if "--licence" in args or "--license" in args:
        from . import licensing

        print(licensing.license_text(), end="")
        return 0
    if "--third-party-notices" in args:
        from . import licensing

        print(licensing.notices_text(), end="")
        return 0
    if "--self-check" in args:
        return _self_check(args)

    from .ui.application import main as gui_main

    return gui_main(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
