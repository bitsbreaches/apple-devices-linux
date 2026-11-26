# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Invariants of the Debian package and its desktop integration.

The package itself is built by ``packaging/build-deb.sh``; these checks keep the static
metadata honest without requiring dpkg or network access.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from devicedock import APP_ID
from devicedock import licensing

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def _read(name: str) -> str:
    return (PACKAGING / name).read_text(encoding="utf-8")


def test_build_script_is_executable_and_builds_a_deb() -> None:
    script = PACKAGING / "build-deb.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "build-deb.sh must be executable"
    text = script.read_text(encoding="utf-8")
    assert "dpkg-deb --build" in text
    assert "${PACKAGE}_${VERSION}_amd64.deb" in text
    assert 'OUT="$SRC/dist"' in text


def test_build_script_builds_and_ships_a_standalone_payload() -> None:
    text = _read("build-deb.sh")
    assert "--standalone" in text, "Nuitka standalone mode bundles the runtime"
    assert "packaging/nuitka/device-dock.py" in text
    # Publishing the source means no source-exclusion check remains in the build.
    assert "proprietary Python source remains" not in text
    assert "EULA.md" not in text
    # Host typelibs win: only the GTK/libadwaita the distribution provides may be loaded.
    assert 'rm -rf "$RUNTIME/girepository"' in text
    # The bundle carries its own runtime, so no host Python version may be substituted.
    control = (PACKAGING / "deb" / "control").read_text(encoding="utf-8")
    assert "python3" not in control
    assert "@PYTHON_VERSION@" not in text


def test_decryption_library_is_compiled_into_the_standalone_runtime() -> None:
    """Loose source is invisible to Nuitka's import analysis and broke encrypted backups."""
    text = _read("build-deb.sh")
    assert '--include-package="$DECRYPT_PACKAGE"' in text
    assert "--include-package=Crypto" in text
    assert '--nofollow-import-to="$DECRYPT_PACKAGE"' not in text
    assert "DEVICE_DOCK_VENDOR_SDIST" not in text
    # The compiled binary must prove it can import the engine before a package is built.
    assert "--self-check" in text
    dockerfile = (PACKAGING / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "iphone_backup_decrypt==" in dockerfile


def test_build_runs_in_a_pinned_ubuntu_24_04_image() -> None:
    dockerfile = (PACKAGING / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ubuntu:24.04" in dockerfile
    assert "nuitka==" in dockerfile
    script = _read("build-deb.sh")
    assert "packaging/docker" in script
    assert "--volume" in script


def test_maintainer_scripts_are_valid_shell() -> None:
    import subprocess

    for name in ("postinst", "postrm"):
        path = PACKAGING / "deb" / name
        assert path.is_file(), f"{name} is part of the package"
        assert path.stat().st_mode & 0o111, f"{name} must be executable"
        result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_control_template_declares_runtime_dependencies() -> None:
    control = (PACKAGING / "deb" / "control").read_text(encoding="utf-8")
    for field in ("Package: device-dock", "Version: @VERSION@", "Architecture: amd64",
                  "Maintainer: @MAINTAINER@", "Installed-Size: @INSTALLED_SIZE@",
                  "Description:"):
        assert field in control, field

    depends = re.search(r"^Depends:(.*?)(?=^\S)", control, re.MULTILINE | re.DOTALL)
    recommends = re.search(r"^Recommends:(.*?)(?=^\S)", control, re.MULTILINE | re.DOTALL)
    assert depends is not None, "a Depends field is required"

    # Everything a documented feature needs at run time must be mandatory. The GTK and
    # libadwaita floors are real: Gtk.FileDialog, Gtk.ColumnView and
    # ListBox.set_activate_on_single_click need 4.10/4.8, Adw.OverlaySplitView needs 1.4.
    mandatory = {
        "gir1.2-gtk-4.0": "4.10",
        "gir1.2-adw-1": "1.4",
        "gir1.2-gdkpixbuf-2.0": None,
        "libgirepository-1.0-1": None,
        # Device communication and transport.
        "libimobiledevice-utils": None,
        "usbmuxd": None,
        # Live iPhone file browsing: ifuse mounts, fuse3 provides fusermount3 and /dev/fuse.
        "ifuse": None,
        "fuse3": None,
        # Playback of the formats iPhones produce (H.264/HEVC video, AAC audio).
        "gstreamer1.0-plugins-base": None,
        "gstreamer1.0-plugins-good": None,
        "gstreamer1.0-libav": None,
    }
    declared = depends.group(1)
    for package, version in mandatory.items():
        assert package in declared, f"{package} must be a hard dependency"
        if version:
            assert re.search(rf"{re.escape(package)} \(>= {re.escape(version)}\)", declared), \
                f"{package} must require >= {version}"

    # Feature extras degrade gracefully, so they are recommended rather than required.
    assert recommends is not None, "a Recommends field documents the optional extras"
    for package in ("gir1.2-secret-1", "poppler-utils", "ffmpeg", "heif-gdk-pixbuf"):
        assert package in recommends.group(1), package
        assert package not in declared, f"{package} is optional and must not be mandatory"

    # Unused build-time bindings must not be declared.
    assert "python3-gi-cairo" not in control, "the application does not use pycairo"
    # The package is free software: the licence and the source location are stated.
    assert "GPL-3.0-only" in control
    assert "device-dock" in control


def test_copyright_declares_gpl_licence_and_mit_component() -> None:
    text = (PACKAGING / "deb" / "copyright").read_text(encoding="utf-8")
    assert text.startswith("Format: https://www.debian.org/doc/packaging-manuals/"
                           "copyright-format/1.0/")
    assert "License: GPL-3.0-only" in text
    assert "License: Proprietary" not in text
    assert ("Files: usr/share/doc/device-dock/bundled-licenses/"
            "iphone_backup_decrypt.txt") in text
    assert "iphone_backup_decrypt" in text and "compiled into the binary" in text
    assert "License: MIT" in text
    assert "Permission is hereby granted" in text, "the MIT text must be reproduced"
    assert "EULA.md" not in text


def test_appstream_metadata_matches_the_application_id() -> None:
    path = PACKAGING / f"{APP_ID}.metainfo.xml"
    root = ET.parse(path).getroot()
    assert root.tag == "component"
    assert root.findtext("id") == APP_ID
    assert root.findtext("metadata_license") == "CC0-1.0"
    assert root.findtext("project_license") == "GPL-3.0-only"
    assert root.findtext("launchable") == f"{APP_ID}.desktop"
    assert root.find("releases/release") is not None, "a release entry is expected"


def test_icon_is_scalable_svg() -> None:
    path = PACKAGING / f"{APP_ID}.svg"
    root = ET.parse(path).getroot()
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 128 128"


def test_desktop_entry_uses_the_application_icon_and_launcher() -> None:
    text = _read(f"{APP_ID}.desktop")
    assert "Exec=@APPDIR@/run.sh" in text
    assert f"Icon={APP_ID}" in text
    # The build script rewrites both fields for the distribution package.
    script = _read("build-deb.sh")
    assert 's|@APPDIR@/run.sh|device-dock|g' in script
    assert "s|^Icon=.*|Icon=$APP_ID|" in script


def test_packaged_launcher_sets_the_documentation_directory() -> None:
    launcher = _read("device-dock")
    assert "DOCDIR=/usr/lib/device-dock/documents" in launcher
    assert "DEVICE_DOCK_DOCDIR" in launcher
    assert 'RUNTIME="$APPDIR/app"' in launcher
    assert 'exec "$RUNTIME/device-dock" "$@"' in launcher


def test_licence_documents_are_found_in_a_debian_install() -> None:
    candidates = [str(path) for path in licensing._candidates("LICENSE")]
    assert any(path.startswith("/usr/share/doc/device-dock/") for path in candidates), \
        candidates
    # dpkg path-excludes remove /usr/share/doc/* except copyright on Ubuntu cloud/minimal
    # images, so the documents the app must read at run time are also shipped beside it.
    assert any(path.endswith("/documents/LICENSE") for path in candidates), candidates
    script = _read("build-deb.sh")
    assert '"$APPDIR/documents/"' in script
    assert "usr/lib/device-dock/documents/LICENSE" in script
    launcher = _read("device-dock")
    assert "DOCDIR=/usr/lib/device-dock/documents" in launcher


def test_postinst_starts_usbmuxd_without_interrupting_anything() -> None:
    text = _read("deb/postinst")
    # Only a running systemd is serviced; containers and sysvinit must not see errors.
    assert "/run/systemd/system" in text
    assert "systemctl is-active --quiet usbmuxd.service" in text
    assert "systemctl enable --now usbmuxd.service" in text
    assert "systemctl start usbmuxd.service" in text
    # Administrator policy wins, and a live daemon is never restarted.
    assert "masked|masked-runtime" in text
    assert "systemctl restart" not in text
    assert "systemctl disable" not in text
    assert "systemctl stop" not in text
    assert "service usbmuxd" in text, "a non-systemd system gets a service fallback"


def test_removal_never_disables_the_shared_usbmuxd_service() -> None:
    text = _read("deb/postrm")
    assert "systemctl" not in text
    assert "usbmuxd" in text
    assert "deliberately left alone" in text


def test_package_transitions_from_the_former_name() -> None:
    control = (PACKAGING / "deb" / "control").read_text(encoding="utf-8")
    assert "Package: device-dock" in control
    # apt must remove the former package instead of leaving a second launcher behind.
    assert "Breaks: apple-devices (<< 1.2.0)" in control
    assert "Replaces: apple-devices (<< 1.2.0)" in control
    postinst = _read("deb/postinst")
    assert "io.github.appledevices.AppleDevices.desktop" in postinst


def test_raster_icons_exist_for_desktop_integration() -> None:
    for size in (48, 64, 128, 256, 512):
        path = PACKAGING / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        assert path.is_file(), f"{size}px icon missing"
        assert path.read_bytes().startswith(b"\x89PNG"), f"{size}px icon is not a PNG"
    script = _read("build-deb.sh")
    assert "icons/hicolor/${size}x${size}/apps" in script
