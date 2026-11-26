# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from devicedock.devices import media_files
from devicedock.devices.media_files import PhoneFile, SUFFIX_KIND, _app_name, _path


def test_phone_shared_app_labels_are_not_a_fixed_app_list() -> None:
    assert _app_name("com.example.reader") == "com.example.reader"


def test_phone_file_types_cover_user_media_and_documents() -> None:
    assert SUFFIX_KIND[".jpg"] == "Photo"
    assert SUFFIX_KIND[".mp4"] == "Video"
    assert SUFFIX_KIND[".mp3"] == "Music"
    assert SUFFIX_KIND[".pdf"] == "Document"


def test_selected_photo_category_lists_nested_camera_roll_files(monkeypatch, tmp_path: Path) -> None:
    camera_roll = tmp_path / "DCIM" / "100APPLE"
    camera_roll.mkdir(parents=True)
    (camera_roll / "photo.jpg").write_bytes(b"image")
    (tmp_path / "Music").mkdir()
    (tmp_path / "Music" / "song.mp3").write_bytes(b"audio")

    class Mount:
        def __init__(self, _udid, _bundle_id=None):
            pass

        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mount)
    files = media_files.list_files("phone", "/", "Photo")
    assert [(file.name, file.path, file.kind) for file in files] == [
        ("photo.jpg", "/DCIM/100APPLE/photo.jpg", "Photo"),
    ]


def test_document_category_keeps_existing_folder_navigation(monkeypatch, tmp_path: Path) -> None:
    for folder in ("Adobe", "Chrome", "iCloud"):
        target = tmp_path / "Documents" / folder
        target.mkdir(parents=True)
        (target / f"{folder}.pdf").write_bytes(b"document")

    class Mount:
        def __init__(self, _udid, _bundle_id=None):
            pass

        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mount)
    root = media_files.list_files("phone", "/", "Document")
    assert [(file.name, file.path) for file in root] == [("Documents", "/Documents")]
    folders = media_files.list_files("phone", "/Documents", "Document")
    assert {file.name for file in folders if file.is_dir} == {"Adobe", "Chrome", "iCloud"}


def test_books_category_ignores_books_library_metadata(monkeypatch, tmp_path: Path) -> None:
    books = tmp_path / "Books"
    metadata = books / "MetadataStore"
    metadata.mkdir(parents=True)
    (metadata / "BookMetadataStore.sqlite-shm").write_bytes(b"internal")
    (books / ".Books.plist.lock").write_bytes(b"internal")
    (books / "Readable.epub").write_bytes(b"book")

    class Mount:
        def __init__(self, _udid, _bundle_id=None):
            pass

        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mount)
    files = media_files.list_files("phone", "/", "Book")
    assert [(file.name, file.path) for file in files] == [
        ("Readable.epub", "/Books/Readable.epub"),
    ]


def test_shared_document_app_parser_uses_ifuse_display_names(monkeypatch) -> None:
    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/ifuse")
    monkeypatch.setattr(media_files.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=(
                            'com.google.chrome.ios, "158.0", "Chrome"\n'
                            'com.adobe.reader, "24.0", "Adobe Acrobat"\n'), stderr=""))
    assert media_files.shared_document_apps("phone") == [
        ("com.adobe.reader", "Adobe Acrobat"),
        ("com.google.chrome.ios", "Chrome"),
    ]


def test_shared_document_app_parser_skips_the_ifuse_header(monkeypatch) -> None:
    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/ifuse")
    monkeypatch.setattr(media_files.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=(
                            '"CFBundleIdentifier","CFBundleVersion","CFBundleDisplayName"\n'
                            'com.brave.ios.browser, "1.70", ""\n'), stderr=""))
    # The header must not become a phantom folder, and a missing display name falls back
    # to the known app label rather than the bundle identifier.
    assert media_files.shared_document_apps("phone") == [
        ("com.brave.ios.browser", "Brave"),
    ]


def test_shared_app_folders_are_labelled_and_never_duplicate_media_folders() -> None:
    folders = media_files.shared_app_folders(
        [("com.google.chrome.ios", "Chrome"),
         ("com.google.chrome.ios", "Chrome"),
         ("com.adobe.reader", "Adobe Acrobat")],
        existing_names=["Chrome"],
    )
    assert [(folder.name, folder.bundle_id, folder.is_source) for folder in folders] == [
        ("Adobe Acrobat", "com.adobe.reader", True),
    ]


def test_documents_lists_real_downloads_folder_with_non_document_files(monkeypatch,
                                                                       tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "photo.jpg").write_bytes(b"image")
    (downloads / "archive.zip").write_bytes(b"unsupported")

    class Mount:
        def __init__(self, _udid, _bundle_id=None):
            pass

        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mount)
    root = media_files.list_files("phone", "/", "Document")
    assert [file.name for file in root if file.is_dir] == ["Downloads"]
    inside = media_files.list_files("phone", "/Downloads", "Document")
    # Opening Downloads under Documents shows supported downloads of any user-file type.
    assert [(file.name, file.kind) for file in inside] == [("photo.jpg", "Photo")]


def test_documents_does_not_invent_icloud_or_empty_downloads(monkeypatch,
                                                             tmp_path: Path) -> None:
    chrome = tmp_path / "Documents" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "readme.pdf").write_bytes(b"pdf")
    (tmp_path / "Downloads").mkdir()

    class Mount:
        def __init__(self, _udid, _bundle_id=None):
            pass

        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mount)
    root = media_files.list_files("phone", "/", "Document")
    names = {file.name for file in root if file.is_dir}
    assert names == {"Documents"}
    assert "iCloud Drive" not in names
    assert "Downloads" not in names


def test_dcim_is_presented_as_photos() -> None:
    assert media_files.display_folder_name("DCIM") == "Photos"
    assert media_files.display_folder_name("dcim") == "Photos"
    assert media_files.display_folder_name("Documents") == "Documents"

    from devicedock.ui.phone_files import PhoneFilesBrowser

    dcim = PhoneFile("DCIM", "/DCIM", True, kind="Folder")
    app = PhoneFile("Chrome", "/", True, kind="Folder",
                    bundle_id="com.google.chrome.ios", is_source=True)
    assert PhoneFilesBrowser._display_name(dcim) == "Photos"
    assert PhoneFilesBrowser._display_name(app) == "Chrome"


def test_phone_filters_drop_the_redundant_photo_and_download_categories() -> None:
    from devicedock.ui.phone_files import PhoneFilesBrowser

    assert "Photos" not in PhoneFilesBrowser.FILTERS
    assert "Downloads" not in PhoneFilesBrowser.FILTERS
    assert PhoneFilesBrowser.FILTERS.count("Documents") == 1


def test_phone_path_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "phone"
    root.mkdir()
    assert _path(root, "/DCIM/photo.jpg") == root / "DCIM" / "photo.jpg"
    with pytest.raises(ValueError, match="escapes"):
        _path(root, "../../outside.txt")


def test_ifuse_mountpoint_precedes_options_for_supported_cli_syntax(tmp_path: Path) -> None:
    mountpoint = tmp_path / "mount"
    assert media_files._ifuse_mount_args("/usr/bin/ifuse", mountpoint, "phone") == [
        "/usr/bin/ifuse", str(mountpoint), "--udid", "phone",
    ]
    assert media_files._ifuse_mount_args("ifuse", mountpoint, "phone", "org.example.reader") == [
        "ifuse", str(mountpoint), "--udid", "phone", "--documents", "org.example.reader",
    ]


def test_phone_mount_accepts_successful_mount_after_ifuse_exits(monkeypatch) -> None:
    commands = []
    mount_states = iter((True, True, False, False))

    class ExitedProcess:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(media_files.shutil, "which", lambda name: {
        "ifuse": "/usr/bin/ifuse", "fusermount3": "/usr/bin/fusermount3",
    }.get(name))
    monkeypatch.setattr(media_files.subprocess, "Popen", lambda args, **_kwargs:
                        commands.append(args) or ExitedProcess())
    monkeypatch.setattr(media_files.os.path, "ismount", lambda _path: next(mount_states))
    monkeypatch.setattr(media_files.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0))

    mount = media_files.PhoneMount("phone-udid")
    root = mount.__enter__()
    assert root == mount.mountpoint
    assert commands == [["/usr/bin/ifuse", str(mount.mountpoint), "--udid", "phone-udid"]]
    mount.__exit__(None, None, None)


def test_missing_ifuse_preflight_returns_copyable_ubuntu_command(monkeypatch) -> None:
    monkeypatch.setattr(media_files.shutil, "which", lambda _name: None)
    message = media_files.file_browser_preflight("device-id")
    assert message is not None
    assert "iPhone File Browsing Unavailable" in message
    assert "sudo apt install ifuse libimobiledevice-utils usbmuxd fuse3" in message


def test_mount_error_distinguishes_unpaired_phone(tmp_path: Path) -> None:
    browser = media_files.PhoneMount("device-id")
    browser.tempdir = tmp_path
    (tmp_path / "ifuse.log").write_text("Invalid HostID: device is not paired")
    message = str(browser._mount_error())
    assert "no valid pairing" in message
    assert "idevicepair validate" in message
    assert "creating another one" in message


def test_mount_error_distinguishes_fuse_permissions(tmp_path: Path) -> None:
    browser = media_files.PhoneMount("device-id")
    browser.tempdir = tmp_path
    (tmp_path / "ifuse.log").write_text("fusermount3: permission denied on /dev/fuse")
    message = str(browser._mount_error())
    assert "FUSE could not mount" in message
    assert "Do not run Device Dock with sudo" in message


def test_preflight_distinguishes_no_device_from_missing_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(media_files.Path, "exists", lambda _self: True)
    monkeypatch.setattr(media_files.os, "access", lambda *_args: True)
    from devicedock.devices import libimobile
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: [])
    problem = media_files.file_browser_preflight("device-id")
    assert problem is not None
    assert "no longer visible" in problem


def test_preflight_reports_pairing_guidance(monkeypatch) -> None:
    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(media_files.Path, "exists", lambda _self: True)
    monkeypatch.setattr(media_files.os, "access", lambda *_args: True)
    from devicedock.devices import libimobile
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["device-id"])
    monkeypatch.setattr(libimobile, "_run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stderr="Invalid HostID", stdout=""))
    problem = media_files.file_browser_preflight("device-id")
    assert problem is not None
    assert "no valid pairing" in problem


def test_preflight_accepts_an_already_valid_pairing_without_extra_info_calls(monkeypatch) -> None:
    from devicedock.devices import libimobile

    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(media_files.Path, "exists", lambda _self: True)
    monkeypatch.setattr(media_files.os, "access", lambda *_args: True)
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["device-id"])
    checks = []
    monkeypatch.setattr(libimobile, "validate_pairing", lambda udid: checks.append(udid)
                        or libimobile.PairingCheck(libimobile.PairingState.TRUSTED))
    assert media_files.file_browser_preflight("device-id") is None
    assert checks == ["device-id"]


def test_preflight_reuses_successful_discovery_pairing_without_another_lockdown_session(monkeypatch) -> None:
    from devicedock.devices import libimobile

    monkeypatch.setattr(media_files.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(media_files.Path, "exists", lambda _self: True)
    monkeypatch.setattr(media_files.os, "access", lambda *_args: True)
    monkeypatch.setattr(libimobile, "usbmuxd_socket_present", lambda: True)
    monkeypatch.setattr(libimobile, "list_device_ids", lambda: ["device-id"])
    monkeypatch.setattr(libimobile, "validate_pairing", lambda *_args: (_ for _ in ()).throw(
        AssertionError("known-good pairing should not be checked again")))
    assert media_files.file_browser_preflight("device-id", pairing_verified=True) is None


def test_phone_file_is_a_simple_transfer_record() -> None:
    file = PhoneFile("song.mp3", "/Music/song.mp3", False, 42, "Music")
    assert file.kind == "Music"
    assert file.size == 42


def test_phone_folder_export_preserves_paths_and_never_overwrites(tmp_path, monkeypatch) -> None:
    phone = tmp_path / "mock-phone"
    (phone / "Downloads" / "nested").mkdir(parents=True)
    (phone / "Downloads" / "nested" / "guide.pdf").write_bytes(b"pdf-data")

    class Mounted:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return phone

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(media_files, "PhoneMount", Mounted)
    destination = tmp_path / "export"
    folder = PhoneFile("Downloads", "/Downloads", True, kind="Folder")
    first = media_files.pull_items("udid", [folder], destination)
    second = media_files.pull_items("udid", [folder], destination)
    assert first[0].relative_to(destination).as_posix() == "Downloads/nested/guide.pdf"
    assert second[0].relative_to(destination).as_posix() == "Downloads/nested/guide (2).pdf"
    assert first[0].read_bytes() == second[0].read_bytes() == b"pdf-data"
