# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Licensing and ownership verification.

These tests enforce the open-source licensing requirements: the application ships the GNU
General Public License v3.0, no proprietary licence or EULA remains anywhere in the
distribution, third-party notices are preserved, and no personal identifiers are published.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from devicedock import (
    COPYRIGHT_HOLDER,
    COPYRIGHT_NOTICE,
    LICENSE_NAME,
    LICENSE_URL,
    __version__,
)
from devicedock import licensing

ROOT = Path(__file__).resolve().parent.parent

# Text that must not survive the open-source transition.
PROPRIETARY_MARKERS = [
    "All rights reserved",
    "PROPRIETARY AND CONFIDENTIAL",
    "End User Licence Agreement",
    "End User License Agreement",
    "EULA",
    "not open-source",
    "closed source",
]

TEXT_SUFFIXES = {".py", ".sh", ".md", ".yml", ".xml", ".svg", ".desktop", ".txt"}


def _flat(text: str) -> str:
    """Collapse whitespace so assertions survive markdown line wrapping."""
    return " ".join(text.split())


def _distribution_texts() -> list[Path]:
    """Files whose content is published with the application or in the repository."""
    paths = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(ROOT).parts)
        if parts & {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache"}:
            continue
        if path.name == "LICENSE" or path.suffix in TEXT_SUFFIXES:
            paths.append(path)
    return paths


# --------------------------------------------------------------------------- documents
def test_licence_documents_exist():
    assert (ROOT / "LICENSE").is_file(), "LICENSE must ship with the application"
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert not (ROOT / "EULA.md").exists(), "the proprietary EULA must not be published"


def test_license_file_is_the_gpl_v3_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in text
    assert "TERMS AND CONDITIONS" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "All rights reserved" not in text


def test_licensing_module_reads_both_documents():
    licence = licensing.license_text()
    assert "GNU GENERAL PUBLIC LICENSE" in licence
    assert "Version 3, 29 June 2007" in licence
    assert "THIRD-PARTY NOTICES" in licensing.notices_text()


def test_licensing_module_reports_missing_document(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_DOCK_DOCDIR", str(tmp_path))
    monkeypatch.setattr(licensing, "_candidates", lambda name: [tmp_path / name])
    with pytest.raises(licensing.DocumentNotFound):
        licensing.license_text()


def test_licensing_module_contains_no_enforcement_or_network_code():
    text = (ROOT / "devicedock" / "licensing.py").read_text(encoding="utf-8")
    for forbidden in ("requests", "urllib", "socket", "acceptance", "activation", "serial"):
        assert forbidden not in text, f"licensing.py must stay a document reader: {forbidden}"


# ------------------------------------------------------------------------- metadata
def test_metadata_declares_the_gpl_and_no_longer_the_eula():
    assert "GPL-3.0-only" in LICENSE_NAME
    assert COPYRIGHT_HOLDER == "Device Dock Contributors"
    assert COPYRIGHT_NOTICE.startswith("Copyright ©")
    assert LICENSE_URL.startswith("https://www.gnu.org/licenses/")
    assert __version__
    assert not hasattr(__import__("devicedock"), "PROPRIETARY_NOTICE")


def test_no_proprietary_markers_in_the_distribution():
    offenders = []
    for path in _distribution_texts():
        # The test suite names the removed terms on purpose, and third-party notices keep
        # their own copyright lines verbatim.
        if path.parent.name == "tests" or path.name == "THIRD_PARTY_NOTICES.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in PROPRIETARY_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not offenders, offenders


def test_no_personal_identifiers_or_contact_addresses():
    offenders = []
    for path in _distribution_texts():
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"[\w.+-]+@gmail\.com", text):
            offenders.append(f"{path.relative_to(ROOT)}: personal email")
        if re.search(r"/home/[a-z0-9_]+/|/media/[a-z0-9_]+/", text):
            offenders.append(f"{path.relative_to(ROOT)}: local path")
    assert not offenders, offenders


# ------------------------------------------------------------------- source headers
def test_source_files_carry_the_gpl_header():
    missing = []
    for path in sorted((ROOT / "devicedock").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "SPDX-License-Identifier: GPL-3.0-only" not in text:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, f"missing SPDX GPL header: {missing}"


def test_no_module_denies_the_gpl():
    for path in sorted((ROOT / "devicedock").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "not licensed under" not in text, path


# ----------------------------------------------------------------------- third parties
def test_third_party_notices_cover_all_declared_dependencies():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    declared = re.findall(r"^([A-Za-z0-9_.\-]+)(?:[<>=!~]|$)", requirements, re.MULTILINE)
    assert declared, "requirements.txt should declare dependencies"
    for name in declared:
        assert f"`{name}`" in text, f"{name} must appear in THIRD_PARTY_NOTICES.md"


@pytest.mark.parametrize("component,licence", [
    ("GTK 4", "LGPL-2.1-or-later"),
    ("libadwaita", "LGPL-2.1-or-later"),
    ("PyGObject", "LGPL-2.1-or-later"),
    ("libimobiledevice", "LGPL-2.1-or-later"),
    ("libusbmuxd", "LGPL-2.1-or-later"),
    ("libplist", "LGPL-2.1-or-later"),
    ("libsecret", "LGPL-2.1-or-later"),
    ("usbmuxd", "GPL-2.0-or-later"),
    ("iphone_backup_decrypt", "MIT"),
    ("pycryptodome", "BSD-2-Clause"),
    ("cryptography", "Apache-2.0"),
    ("cffi", "MIT-0"),
    ("pycparser", "BSD-3-Clause"),
])
def test_notices_state_component_licence(component, licence):
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert component in text, f"{component} must be listed"
    assert licence in text, f"{licence} must be recorded for {component}"


def test_notices_include_mit_text_and_third_party_copyright():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "The MIT License" in text
    assert "Copyright (c) 2019-2026 James Sharkey" in text
    assert "Redistributions of source code must retain the above copyright notice" in text


def test_notices_state_gpl_compatibility_without_relicensing():
    text = _flat((ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))
    assert "GPL-3.0" in text
    assert "separate system daemon" in text
    assert "Do not ship `usbmuxd` inside a distribution package" in text
    assert "replace" in text  # LGPL relink/replacement right documented
    assert "LGPL-2.1-or-later" in text


# ------------------------------------------------------------------------ distribution
def test_readme_documents_the_open_source_licence():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    flat = _flat(text)
    assert "GPL-3.0" in flat
    assert "LICENSE" in text and "THIRD_PARTY_NOTICES.md" in text
    assert "proprietary" not in flat.lower()


def test_packaging_includes_the_licence_documents():
    for script in ("packaging/install.sh", "packaging/build-tarball.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        assert "LICENSE" in text, f"{script} must ship the licence"
        assert "THIRD_PARTY_NOTICES.md" in text, f"{script} must ship the notices"
    desktop = (ROOT / "packaging/io.github.bitsbreaches.DeviceDock.desktop").read_text(
        encoding="utf-8")
    assert "Exec=" in desktop


def test_requirements_and_notices_agree_on_direct_dependencies():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "iphone_backup_decrypt" in text and "cryptography" in text
    assert "Development-only dependencies (not distributed)" in text
