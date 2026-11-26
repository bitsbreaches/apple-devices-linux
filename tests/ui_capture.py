#!/usr/bin/env python3

# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Capture screenshots of every page for UI review.

Runs the real application in an isolated configuration, visits each page, and writes PNGs
at the default window size and maximised. Device polling is disabled in this profile so a
concurrent device operation (for example a benchmark backup) is not disturbed.

Usage::

    DISPLAY=:0 .venv/bin/python tests/ui_capture.py \
        --out docs/ui-review/before --backup-root "/path/to/backups"

A backup must be unlocked for the Explorer/Categories pages to show content; pass
``--password`` (or point ``--backup-root`` at an unencrypted backup) to exercise them.
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

_parser = argparse.ArgumentParser()
_parser.add_argument("--out", required=True, help="Directory for the PNG files")
_parser.add_argument("--backup-root", action="append", help="Folder with backups; repeatable")
_parser.add_argument("--password", help="Backup password for the first encrypted backup")
_parser.add_argument("--prefix", default="", help="Filename prefix, e.g. 'before-'")
ARGS = _parser.parse_args()

OUT = Path(ARGS.out).expanduser().resolve()
OUT.mkdir(parents=True, exist_ok=True)
# Scratch state lives outside the screenshot directory so the output stays clean.
SCRATCH = Path(tempfile.mkdtemp(prefix="devicedock-capture-"))
# Isolate every data location, including HOME, so a real backup on this machine can never
# appear in a screenshot.
for variable, subdirectory in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                               ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
    path = SCRATCH / subdirectory
    path.mkdir(parents=True, exist_ok=True)
    os.environ[variable] = str(path)
_home = SCRATCH / "home"
_home.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_home)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from devicedock.config import Settings  # noqa: E402
from devicedock.ui.application import DeviceDockApplication  # noqa: E402

PAGES = ("overview", "devices", "backups", "explorer", "diagnostics", "feedback", "settings")


def pump(seconds: float = 0.6) -> None:
    context = GLib.MainContext.default()
    end = time.time() + seconds
    while time.time() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.02)


def window_id(title: str = "Device Dock") -> str | None:
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f'"{title}"' in line:
            return line.strip().split()[0]
    return None


def shot(name: str, title: str = "Device Dock") -> None:
    wid = window_id(title)
    if wid is None:
        print(f"NOTE  window {title!r} not found for {name}")
        return
    target = OUT / f"{ARGS.prefix}{name}.png"
    try:
        subprocess.run(["import", "-window", wid, str(target)], check=False, timeout=30)
        print(f"captured {target.name}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"NOTE  capture failed for {name}: {exc}")


def main() -> int:
    if _instance_running():
        print("FAIL  another Device Dock instance is running")
        return 1

    roots = [Path(root) for root in (ARGS.backup_root or [])]
    settings = Settings(
        backup_directories=[str(root) for root in roots] or Settings().backup_directories,
        export_directory=str(OUT / "exports"),
        # Keep the UI inert so a concurrent device operation is not disturbed.
        auto_refresh_seconds=3600,
    )
    settings.save()

    app = DeviceDockApplication()
    app.register(None)
    app.activate()
    pump(2.5)
    window = app.window
    if window is None:
        print("FAIL  no window")
        return 1

    for page in PAGES:
        window.show_page(page)
        pump(0.9)
        shot(page)

    # Dialogs and workflows.
    app._show_about()
    pump(1.2)
    shot("dialog-about", "About")
    if app._about_window is not None:
        app._about_window.close()
    pump(0.4)

    # Prefer a backup we can open without a password so the content pages are exercised.
    if ARGS.password:
        target = next((i for i in window.state.backups if i.is_encrypted), None)
    else:
        target = next((i for i in window.state.backups if not i.is_encrypted), None)
    if target is None and window.state.backups:
        target = window.state.backups[0]
    if target is not None:
        password = ARGS.password if target.is_encrypted else None
        unlocked: dict[str, object] = {}
        window.state.unlock(target, password,
                            on_success=lambda vault: unlocked.setdefault("vault", vault),
                            on_error=lambda exc: unlocked.setdefault("error", exc))
        pump(6.0)
        if "vault" in unlocked:
            window.show_page("explorer")
            pump(1.5)
            explorer = window.pages["explorer"]
            row = explorer.domain_list.get_row_at_index(0)
            if row is not None:
                explorer.domain_list.select_row(row)
                pump(1.2)
            shot("explorer-loaded")
        else:
            print(f"NOTE  backup not unlocked: {unlocked.get('error')}")

    # Maximised captures to check layout at larger sizes.
    window.maximize()
    pump(1.8)
    for page in ("overview", "backups", "explorer"):
        window.show_page(page)
        pump(1.0)
        shot(f"{page}-maximised")
    window.unmaximize()
    pump(0.6)

    # Narrow window to check responsive behaviour. GTK 4 resizes a mapped window through
    # set_default_size; the window manager may still enforce its own minimum size.
    window.unmaximize()
    pump(0.4)
    window.set_default_size(820, 700)
    pump(1.5)
    window.show_page("backups")
    pump(1.0)
    shot("backups-narrow")

    window.state.close_vault()
    window.destroy()
    pump(0.3)
    import shutil

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"screenshots in {OUT}")
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
