#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""UI smoke test: drive every page in-process and capture screenshots.

Run with a graphical session available:

    .venv/bin/python tests/ui_smoke.py --backup-root /path/to/backups --out /tmp/ui-shots

The script builds a temporary settings file, opens a synthetic (or supplied) backup,
visits every page, and asserts that each page produced content. Screenshots are taken
with ImageMagick's ``import`` when it is present.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

# Configuration paths are resolved at import time, so the isolated XDG directories must
# exist before any application module is imported.
_parser = argparse.ArgumentParser()
_parser.add_argument("--backup-root", action="append",
                     help="Folder containing backup dirs; repeatable (default: synthetic)")
_parser.add_argument("--out", default=tempfile.mkdtemp(prefix="devicedock-ui-"))
_parser.add_argument("--password", help="Password for the first encrypted backup found")
ARGS = _parser.parse_args()

OUT = Path(ARGS.out)
OUT.mkdir(parents=True, exist_ok=True)
# Isolate every data location, including HOME: the application also scans the classic
# iTunes backup folders, and a real backup must never appear in a test run or screenshot.
for variable, subdirectory in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                               ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
    path = OUT / subdirectory
    path.mkdir(parents=True, exist_ok=True)
    os.environ[variable] = str(path)
_home = OUT / "home"
_home.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_home)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from devicedock import licensing  # noqa: E402
from devicedock.backup import BadPasswordError  # noqa: E402
from devicedock.config import Settings  # noqa: E402
from devicedock.ui.application import DeviceDockApplication  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"PASS  {message}")
    else:
        print(f"FAIL  {message}")
        failures.append(message)


def collect_labels(widget: object) -> list[str]:
    """Return every label text in a widget tree, so panel content can be asserted."""
    from gi.repository import Gtk

    texts: list[str] = []
    if isinstance(widget, Gtk.Label):
        texts.append(widget.get_text())
    child = widget.get_first_child() if isinstance(widget, Gtk.Widget) else None
    while child is not None:
        texts.extend(collect_labels(child))
        child = child.get_next_sibling()
    return texts


def _all_rows(group: object) -> list:
    """Every Adw.ActionRow in a preferences page, in order."""
    from gi.repository import Adw, Gtk

    rows = []
    child = group.get_first_child()
    while child is not None:
        if isinstance(child, Adw.ActionRow):
            rows.append(child)
        elif isinstance(child, Gtk.Widget):
            rows.extend(_all_rows(child))
        child = child.get_next_sibling()
    return rows


def _instance_running() -> bool:
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.GetNameOwner",
             "io.github.bitsbreaches.DeviceDock"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def pump(seconds: float = 0.8) -> None:
    context = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.02)


def window_id_for(pid: int) -> str | None:
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if '"Device Dock"' in line:
            return line.strip().split()[0]
    return None


def screenshot_root(path: Path) -> None:
    """Capture the whole screen (used for modal dialogs that own a separate window)."""
    try:
        subprocess.run(["import", "-window", "root", str(path)], check=False, timeout=20)
        print(f"NOTE  screenshot {path}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"NOTE  screenshot failed: {exc}")


def screenshot_window(path: Path, title: str) -> None:
    """Capture the window whose title matches ``title`` (e.g. the About dialog)."""
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"NOTE  cannot list windows: {exc}")
        return
    for line in out.splitlines():
        if f'"{title}"' in line:
            wid = line.strip().split()[0]
            try:
                subprocess.run(["import", "-window", wid, str(path)], check=False, timeout=20)
                print(f"NOTE  screenshot {path}")
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"NOTE  screenshot failed: {exc}")
            return
    print(f"NOTE  no window titled {title!r} for screenshot")


def screenshot(path: Path) -> None:
    wid = window_id_for(os.getpid())
    if wid is None:
        print(f"NOTE  no window id for screenshot {path.name}")
        return
    try:
        subprocess.run(["import", "-window", wid, str(path)], check=False, timeout=20)
        print(f"NOTE  screenshot {path}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"NOTE  screenshot failed: {exc}")


def main() -> int:
    if ARGS.backup_root:
        backup_roots = [Path(root) for root in ARGS.backup_root]
    else:
        from conftest import build_backup

        synthetic = OUT / "backups"
        build_backup(synthetic / "0000811000000000000000AA")
        backup_roots = [synthetic]

    settings = Settings(backup_directories=[str(root) for root in backup_roots],
                        export_directory=str(OUT / "exports"))
    settings.save()

    # The application is single-instance: if a copy is already running it owns the
    # well-known D-Bus name and activation would be forwarded to it instead of creating a
    # window here.
    if _instance_running():
        print("FAIL  another Device Dock instance is running; close it before the smoke test")
        return 1

    # Exactly one device-polling timer may exist, and polling must not add another.
    import devicedock.ui.window as window_module

    timers: list[tuple] = []
    real_timeout = window_module.GLib.timeout_add_seconds

    def counting_timeout(interval, callback, *args, **kwargs):
        timers.append((interval, callback))
        return real_timeout(interval, callback, *args, **kwargs)

    window_module.GLib.timeout_add_seconds = counting_timeout
    app = DeviceDockApplication()
    app.register(None)
    app.activate()
    pump(2.5)
    window = app.window
    check(window is not None, "main window created")
    check(len(timers) == 1, f"exactly one polling timer is registered ({len(timers)})")
    if timers:
        for _ in range(5):
            window._poll_devices()
            pump(0.1)
        check(len(timers) == 1,
              f"device polling does not add further timers ({len(timers)})")

    # Open source: there is no licence gate, and the GPL text ships with the application.
    check(window.get_sensitive() is True, "window is usable on first launch")
    check(not hasattr(app, "_eula_dialog"), "no licence acceptance dialog exists")
    check("GNU GENERAL PUBLIC LICENSE" in licensing.license_text(), "GPL text is available")
    check("THIRD-PARTY NOTICES" in licensing.notices_text(), "notices are available")

    state = app.state
    check(len(state.backups) >= 1, f"backups discovered ({len(state.backups)})")
    check(state.devices == [] or state.device_error is None, "device discovery did not crash")

    # About dialog must state the open-source licence.
    app._show_about()
    pump(1.5)
    screenshot_window(OUT / "about.png", "About")
    from devicedock import COPYRIGHT_NOTICE, LICENSE_NAME
    about = app._about_window
    check(about is not None, "About dialog opened")
    if about is not None:
        check(about.get_copyright() == COPYRIGHT_NOTICE,
              f"About dialog shows the copyright notice ({about.get_copyright()!r})")
        check(LICENSE_NAME in (about.get_comments() or ""),
              "About dialog states the GPL licence")
        check("Proprietary" not in (about.get_comments() or ""),
              "About dialog no longer claims proprietary licensing")
    if about is not None:
        about.close()
    pump(0.4)

    # Support Development must open and show both validated donation addresses.
    window.show_support()
    pump(1.5)
    dialog = window._support_dialog
    check(dialog is not None, "Support Development dialog opened")
    if dialog is not None:
        screenshot_window(OUT / "support.png", "Support Development")
        from devicedock import support
        texts = collect_labels(dialog.get_content())
        check(support.BITCOIN_ADDRESS in texts, "support dialog shows the Bitcoin address")
        check(support.TRON_USDT_ADDRESS in texts, "support dialog shows the USDT-TRC20 address")
        check(not any("could not be displayed" in text for text in texts),
              "support dialog shows no configuration error")
        check(any("entirely optional" in text for text in texts),
              "support dialog states donations are optional")
        dialog.close()
    pump(0.4)

    for key in ("overview", "phone", "devices", "backups", "explorer", "categories",
                "diagnostics", "feedback", "settings"):
        window.show_page(key)
        pump(0.6)
        page = window.pages[key]
        check(page.get_parent() is not None, f"page {key} attached and rendered")
        screenshot(OUT / f"{key}.png")

    # Feedback must disclose and prefill only the technical values it lists.
    from devicedock import feedback as feedback_module

    feedback_page = window.pages["feedback"]
    window.show_page("feedback")
    pump(0.4)
    check(feedback_page.switch.get_active(),
          "feedback includes technical information by default")
    prefilled = feedback_page._current_link()
    check(prefilled.startswith(feedback_module.BASE_URL + "?"),
          "feedback link points at the configured form with prefilled fields")
    check(feedback_module.VERSION_FIELD in prefilled,
          "feedback link prefills the application version")
    if state.devices:
        check(feedback_module.MODEL_FIELD in prefilled,
              "feedback link prefills the connected device model")
    else:
        check(feedback_module.MODEL_FIELD not in prefilled,
              "feedback link omits the device model when no device is connected")
    feedback_page.switch.set_active(False)
    pump(0.2)
    check(feedback_page._current_link() == feedback_module.BASE_URL,
          "feedback link drops every prefill when technical information is disabled")
    feedback_page.switch.set_active(True)
    pump(0.2)

    # Create backup must offer the default destination without a folder chooser.
    devices_page = window.pages["devices"]
    devices_page.refresh()
    pump(0.5)
    # Capture the device once: the background poll can empty the list while the dialog runs.
    device = state.devices[0] if state.devices else None
    if device is not None:
        devices_page._choose_backup_kind(device)
        pump(1.2)
        setup = getattr(devices_page, "_setup_dialog", None)
        check(setup is not None, "Create backup opens a setup dialog")
        if setup is not None:
            screenshot_window(OUT / "backup-setup.png", setup.get_title() or "")
            print(f"NOTE  backup setup dialog window title: {setup.get_title()!r}")
            texts = collect_labels(setup.get_extra_child())
            default_path = str(state.settings.default_destination)
            expected = f"~/{state.settings.default_destination.relative_to(Path.home())}"
            check(any(default_path in text or expected in text for text in texts),
                  f"setup dialog defaults to {expected} (labels: {texts!r})")
            notes = getattr(devices_page, "_setup_notes", None)
            notes_text = notes.get_text() if notes is not None else ""
            check("locked" in notes_text.lower(),
                  f"setup dialog warns that a locked device blocks the backup ({notes_text!r})")
            setup.response("cancel")
            pump(0.3)

        # "Restore Device" must be reachable from the device card and gated by a typed
        # confirmation, which is what makes the erase feature safe to ship.
        from devicedock.devices.erase_engine import ErasePlan

        plan = ErasePlan(udid=device.udid, device_name=device.display_name)
        devices_page._confirm_reset(plan)
        pump(0.6)
        gate = getattr(devices_page, "_reset_dialog", None)
        check(gate is not None, "Restore Device opens a typed confirmation dialog")
        if gate is not None:
            screenshot_window(OUT / "factory-reset.png", gate.get_title() or "")
            check(not gate.get_response_enabled("erase"),
                  "Restore Device confirmation starts disabled")
            gate.response("cancel")
            pump(0.3)
    else:
        print("NOTE  no device attached; backup setup dialog not exercised")

    # "Refresh everything" must re-evaluate backup-location rows: a folder created after
    # the page was built has to stop saying "missing".
    settings_page = window.pages["settings"]
    toasts: list[str] = []
    original_toast = window.show_toast
    window.show_toast = lambda message: (toasts.append(message), original_toast(message))[1]
    fresh = OUT / "newly-created-backups"
    fresh.mkdir(parents=True, exist_ok=True)
    state.settings.add_backup_directory(str(fresh))
    state.settings.save()
    settings_page.refresh()
    pump(0.3)
    window.refresh_all()
    pump(0.6)
    row_texts = [row.get_subtitle() or "" for row in _all_rows(settings_page.page)]
    check(not any(text == "missing" for text in row_texts) or
          all("newly-created-backups" not in str(row.get_title()) or row.get_subtitle() != "missing"
              for row in _all_rows(settings_page.page)),
          "a newly created backup location is not reported as missing after Refresh everything")
    check(any("refreshed" in message.lower() for message in toasts),
          "Refresh everything reports what it did")

    # Unlock a readable backup through the same code path the UI uses. A plaintext backup is
    # preferred so the explorer and category views can be exercised without a password.
    backups_page = window.pages["backups"]
    backups_page.refresh()
    pump(0.4)
    check(backups_page.table.row_count >= 1, "backups table populated")

    plaintext = next((info for info in state.backups if not info.is_encrypted), None)
    target = plaintext or state.backups[0]
    password = ARGS.password if target.is_encrypted else None
    unlocked: dict[str, object] = {}

    state.unlock(target, password, on_success=lambda v: unlocked.setdefault("vault", v),
                 on_error=lambda e: unlocked.setdefault("error", e))
    pump(6.0)
    if target.is_encrypted and password is None:
        check("error" in unlocked, "encrypted backup without password reports an error")
    else:
        check("vault" in unlocked, f"backup unlocked ({unlocked.get('error', '')})")

    if "vault" in unlocked:
        window.show_page("explorer")
        pump(0.5)
        explorer = window.pages["explorer"]
        explorer.refresh()
        pump(2.5)
        check(explorer.category_list.get_row_at_index(0) is not None,
              "explorer lists user-file categories")
        if explorer.category_list.get_row_at_index(0) is not None:
            explorer.category_list.select_row(explorer.category_list.get_row_at_index(0))
            pump(1.5)
            check(explorer.table.row_count > 0, "explorer lists files in the first category")
        screenshot(OUT / "explorer-loaded.png")

        window.show_page("categories")
        pump(2.5)
        categories = window.pages["categories"]
        check(categories.listbox.get_row_at_index(0) is not None,
              "categories page lists the data present in the backup")
        if categories.listbox.get_row_at_index(0) is not None:
            categories.listbox.select_row(categories.listbox.get_row_at_index(0))
            pump(1.5)
            check(categories.table.row_count > 0, "a category loads its rows")
        screenshot(OUT / "categories-loaded.png")

        window.show_page("diagnostics")
        pump(0.5)
        screenshot(OUT / "diagnostics-loaded.png")

    # Wrong-password handling against a real encrypted keybag (when one is configured).
    encrypted = next((info for info in state.backups if info.is_encrypted), None)
    if encrypted is not None:
        wrong: dict[str, object] = {}
        state.unlock(encrypted, "wrong-password-used-by-the-smoke-test",
                     on_success=lambda v: wrong.setdefault("vault", v),
                     on_error=lambda e: wrong.setdefault("error", e))
        pump(8.0)
        check(isinstance(wrong.get("error"), BadPasswordError),
              "wrong password rejected by the backup keybag")
        window.show_page("backups")
        pump(0.6)
        screenshot(OUT / "backups-encrypted.png")
    else:
        print("NOTE  no encrypted backup configured; wrong-password path not exercised")

    app.state.close_vault()
    window.destroy()
    pump(0.3)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all UI smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
