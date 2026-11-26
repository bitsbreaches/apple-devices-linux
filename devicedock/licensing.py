# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Distribution documents: the licence text and third-party notices.

The application is free software under the GNU General Public License, version 3 only.
This module only locates and reads the documents shipped with the distribution; it never
contacts a server and contains no licence enforcement.
"""

from __future__ import annotations

import os
from pathlib import Path

LICENSE_FILENAME = "LICENSE"
NOTICES_FILENAME = "THIRD_PARTY_NOTICES.md"


class DocumentNotFound(FileNotFoundError):
    """A distribution document could not be located next to the installed application."""


def _candidates(filename: str) -> list[Path]:
    """Ordered locations for a distribution document, repository checkout first."""
    here = Path(__file__).resolve()
    options: list[Path] = []

    docdir = os.environ.get("DEVICE_DOCK_DOCDIR")
    if docdir:
        options.append(Path(docdir) / filename)

    # Repository checkout / installed tree: <root>/devicedock/../<file>
    options.append(here.parent.parent / filename)
    # Packaged layout: <prefix>/lib/device-dock/documents/<file>. Runtime-required
    # documents live here because dpkg path-excludes (/etc/dpkg/dpkg.cfg.d/excludes on
    # Ubuntu cloud/minimal images) remove everything under /usr/share/doc except copyright.
    options.append(here.parent.parent / "documents" / filename)
    # Installed layout: <prefix>/share/devicedock/{devicedock,<file>}
    options.append(here.parent.parent.parent / filename)
    # Inside the package itself (packaging may copy it here).
    options.append(here.parent / filename)
    # Debian package: /usr/lib/device-dock/devicedock + /usr/share/doc/device-dock.
    options.append(Path("/usr/share/doc/device-dock") / filename)
    # System-wide documentation directory.
    options.append(Path("/usr/share/doc/devicedock") / filename)
    options.append(Path("/usr/local/share/doc/devicedock") / filename)
    return options


def find_document(filename: str) -> Path:
    """Return the first existing path for ``filename`` or raise :class:`DocumentNotFound`."""
    for candidate in _candidates(filename):
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _candidates(filename))
    raise DocumentNotFound(
        f"{filename} was not found. It is part of the distribution and must accompany the "
        f"application. Searched: {searched}"
    )


def read_document(filename: str) -> str:
    return find_document(filename).read_text(encoding="utf-8")


def license_text() -> str:
    """The GNU General Public License, version 3, shipped with this release."""
    return read_document(LICENSE_FILENAME)


def notices_text() -> str:
    """The third-party notices document shipped with this release."""
    return read_document(NOTICES_FILENAME)
