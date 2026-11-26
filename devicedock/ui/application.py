# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""GTK application bootstrap: settings, logging, actions and the main window."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, COPYRIGHT_NOTICE, LICENSE_NAME, LICENSE_URL, __version__
from ..config import Settings, ensure_default_directories
from ..logging_setup import setup_logging
from .state import AppState
from .window import MainWindow

log = logging.getLogger(__name__)

_CSS = """
/* Shared surfaces: one radius, one border weight, one padding scale. */
.card {
  background-color: @card_bg_color;
  border-radius: 12px;
  border: 1px solid alpha(currentColor, 0.08);
  padding: 16px;
}
.card > box { padding: 0; }

.kv-grid { padding: 2px 0; }
.kv-grid > label { min-height: 22px; }
.kv-grid > label:first-child { min-width: 132px; }

/* Tables: slightly denser rows than the default, aligned to the 4 px grid. */
.data-table { border-radius: 8px; }
.data-table > header { background-color: alpha(currentColor, 0.04); border-radius: 8px 8px 0 0; }
.data-table > listview { background: transparent; }

.navigation-sidebar row { border-radius: 8px; margin: 1px 6px; }

.banner.error { background-color: @error_bg_color; color: @error_fg_color; }
.banner.warning { background-color: @warning_bg_color; color: @warning_fg_color; }

/* Long paths and identifiers should not stretch layouts. */
.path-label { font-feature-settings: "tnum"; }

/* Low-emphasis fact line above a table. */
.status-strip { padding: 2px 2px 6px 2px; }

/* Secondary pane headings inside a paned page. */
.pane-title { font-weight: 700; }

.preview-header { padding: 10px 14px 6px 14px; }
.preview-header .title-5 { font-weight: 700; }

/* Breadcrumb of the explorer: compact, clickable segments. */
.breadcrumb button { padding: 2px 6px; min-height: 0; }
.breadcrumb separator { opacity: 0.45; }
"""


class DeviceDockApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = Settings.load()
        setup_logging(self.settings.log_level)
        self.state = AppState(self.settings)
        self.window: MainWindow | None = None
        self._about_window: Adw.AboutWindow | None = None

    # ------------------------------------------------------------------
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # The backup library must exist before "Create backup" offers it as a destination,
        # even when the application was started without running the installer.
        problems = ensure_default_directories()
        for path in problems:
            log.warning("Cannot create %s", path)
        self._install_css()
        self._add_actions()

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _add_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

        scan_action = Gio.SimpleAction.new("scan", None)
        scan_action.connect("activate", lambda *_: self.state.scan_backups())
        self.add_action(scan_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.scan", ["<Control>r"])

    def _show_about(self, *_args) -> None:
        about = Adw.AboutWindow(
            transient_for=self.window,
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Device Dock Contributors",
            version=__version__,
            copyright=COPYRIGHT_NOTICE,
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/bitsbreaches/device-dock",
            issue_url="https://github.com/bitsbreaches/device-dock/issues",
            comments=(
                f"This program is free software under the {LICENSE_NAME}.\n"
                f"{LICENSE_URL}\n\n"
                "Native GTK 4 application for inspecting iOS devices and iTunes/Finder "
                "backups. Device communication uses libimobiledevice; encrypted backups are "
                "decrypted with the iphone_backup_decrypt library. Backups are only read; a "
                "device is only written to (backup, file transfer, restore, factory reset) "
                "after an explicit confirmation.\n\n"
                "Third-party components and their licences are listed in "
                "THIRD_PARTY_NOTICES.md."
            ),
        )
        self._about_window = about
        about.present()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self, self.state)
            self.window.present()
            self.state.scan_backups()
            self.state.refresh_devices()
        else:
            self.window.present()

    def do_shutdown(self) -> None:
        # Removes any decrypted temporary manifest copy.
        self.state.close_vault()
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    return DeviceDockApplication().run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
