# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Main application window: sidebar navigation, header bar, toasts and busy state."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gio, GLib, Gtk  # noqa: E402

from .. import APP_NAME, __version__
from .pages.backups import BackupsPage
from .pages.categories import CategoriesPage
from .pages.devices import DevicesPage
from .pages.diagnostics import DiagnosticsPage
from .pages.explorer import ExplorerPage
from .pages.feedback import FeedbackPage
from .pages.overview import OverviewPage
from .pages.phone import OnMyIPhonePage
from .pages.settings import SettingsPage
from .widgets import SIDEBAR_WIDTH, Space

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, state) -> None:
        super().__init__(application=application)
        self.state = state
        self.set_title(APP_NAME)
        self.set_default_size(1240, 820)
        # Adwaita warns when a window sets no minimum; these fit an 800×600 display while the
        # 760 px breakpoint switches the sidebar to an overlay.
        self.set_size_request(360, 480)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)

        self.pages = {
            "overview": OverviewPage(state, self),
            "phone": OnMyIPhonePage(state, self),
            "devices": DevicesPage(state, self),
            "backups": BackupsPage(state, self),
            "explorer": ExplorerPage(state, self),
            "categories": CategoriesPage(state, self),
            "diagnostics": DiagnosticsPage(state, self),
            "feedback": FeedbackPage(state, self),
            "settings": SettingsPage(state, self),
        }
        for key, page in self.pages.items():
            self.stack.add_named(page, key)

        # Navigation: primary pages, then a separate group for tools.
        self.sidebar = Gtk.ListBox()
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar.connect("row-selected", self._row_selected)
        primary = ("overview", "phone", "devices", "backups", "explorer")
        secondary = ("diagnostics", "feedback", "settings")
        for key in primary:
            page = self.pages[key]
            self.sidebar.append(self._sidebar_row(page.icon_name, page.title, key))
        separator_row = Gtk.ListBoxRow()
        separator_row.set_selectable(False)
        separator_row.set_activatable(False)
        separator_row.set_child(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL,
                                              margin_top=Space.S, margin_bottom=Space.S))
        self.sidebar.append(separator_row)
        for key in secondary:
            page = self.pages[key]
            self.sidebar.append(self._sidebar_row(page.icon_name, page.title, key))
        # Rows without a page key must not be treated as navigation targets.
        self._nav_rows = [row for row in self.sidebar
                          if getattr(row, "page_key", None) is not None]

        side_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        side_box.set_size_request(SIDEBAR_WIDTH, -1)
        brand = Gtk.Label(label=APP_NAME, xalign=0)
        brand.add_css_class("title-4")
        brand.set_margin_top(Space.M)
        brand.set_margin_bottom(Space.S)
        brand.set_margin_start(Space.L)
        side_box.append(brand)
        side_box.append(self.sidebar)

        support_row = Gtk.Button()
        support_row.add_css_class("flat")
        support_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        support_content.append(Gtk.Image.new_from_icon_name("emblem-favorite-symbolic"))
        support_content.append(Gtk.Label(label="Support Development", xalign=0))
        support_row.set_child(support_content)
        support_row.set_margin_start(Space.M)
        support_row.set_margin_end(Space.M)
        support_row.set_margin_bottom(Space.S)
        support_row.set_tooltip_text("Support ongoing development and maintenance")
        support_row.connect("clicked", lambda _b: self.show_support())
        side_box.append(support_row)

        version = Gtk.Label(label=f"Version {__version__}", xalign=0)
        version.add_css_class("dim-label")
        version.add_css_class("caption")
        version.set_margin_bottom(Space.M)
        version.set_margin_start(Space.L)
        side_box.append(version)

        # Responsive shell: the sidebar becomes an overlay below the breakpoint.
        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar(side_box)
        self.split_view.set_content(self.stack)
        self.split_view.set_min_sidebar_width(SIDEBAR_WIDTH)
        self.split_view.set_max_sidebar_width(SIDEBAR_WIDTH)
        self.split_view.set_show_sidebar(True)
        self.split_view.set_pin_sidebar(True)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760px"))
        breakpoint.add_setter(self.split_view, "collapsed", True)
        breakpoint.add_setter(self.split_view, "show-sidebar", False)
        self.add_breakpoint(breakpoint)

        # Header bar
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME, subtitle="iPhone files and backups")
        header.set_title_widget(self.window_title)
        self.sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                               tooltip_text="Show navigation", active=True)
        # The toggle mirrors the sidebar visibility; without SYNC_CREATE so the initial
        # ``active`` state cannot reset the sidebar to hidden.
        self.sidebar_toggle.bind_property("active", self.split_view, "show-sidebar",
                                          GObject.BindingFlags.BIDIRECTIONAL)
        # The toggle is only useful when the sidebar is an overlay.
        self.split_view.bind_property("collapsed", self.sidebar_toggle, "visible",
                                      GObject.BindingFlags.SYNC_CREATE)
        header.pack_start(self.sidebar_toggle)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh everything")
        refresh_button.connect("clicked", lambda _b: self.refresh_all())
        header.pack_end(refresh_button)
        header.pack_end(self._menu_button())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        # A minimised progress window leaves this banner behind: the status and a way back.
        self.job_banner = Adw.Banner(revealed=False)
        self.job_banner.set_button_label("Show progress")
        self.job_banner.connect("button-clicked", lambda _b: self._show_job_window())
        self._job_window = None
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.job_banner)
        content.append(self.split_view)
        toolbar.set_content(content)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        self._add_window_actions()
        self.apply_theme()
        self.sidebar.select_row(self._nav_rows[0])

        self.state.subscribe("devices", lambda *_: self._update_subtitle())
        self.state.subscribe("backups", lambda *_: self._update_subtitle())
        self.state.subscribe("vault", lambda *_: self._update_subtitle())
        self._update_subtitle()

        interval = max(2, int(self.state.settings.auto_refresh_seconds))
        GLib.timeout_add_seconds(interval, self._poll_devices)

    def _add_window_actions(self) -> None:
        actions = {
            "scan": lambda *_: self.state.scan_backups(),
            "support": lambda *_: self.show_support(),
            "licence": lambda *_: self.show_licence(),
            "notices": lambda *_: self.show_notices(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    # ------------------------------------------------------------------
    def _sidebar_row(self, icon_name: str, title: str, key: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        box.set_margin_top(Space.S)
        box.set_margin_bottom(Space.S)
        box.set_margin_start(Space.L)
        box.set_margin_end(Space.L)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        box.append(icon)
        box.append(Gtk.Label(label=title, xalign=0))
        row.set_child(box)
        row.page_key = key
        return row

    def _menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        menu.append("Scan backups", "win.scan")
        menu.append("Support Development…", "win.support")
        menu.append("Licence (GPL-3.0)", "win.licence")
        menu.append("Third-party notices", "win.notices")
        menu.append("About Device Dock", "app.about")
        menu.append("Quit", "app.quit")
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        button.set_tooltip_text("Main menu")
        return button

    def show_support(self) -> None:
        from .support_dialog import show_support_dialog

        self._support_dialog = show_support_dialog(self)

    def show_licence(self) -> None:
        from .. import licensing
        from . import dialogs

        dialogs.show_document(self, "Licence (GPL-3.0)", licensing.license_text())

    def show_notices(self) -> None:
        from .. import licensing
        from . import dialogs

        dialogs.show_document(self, "Third-party notices", licensing.notices_text())

    def _row_selected(self, _listbox, row) -> None:
        if row is None or getattr(row, "page_key", None) is None:
            return
        key = row.page_key
        self.stack.set_visible_child_name(key)
        page = self.pages[key]
        page.refresh()
        self.window_title.set_subtitle(page.title)

    def show_page(self, key: str) -> None:
        for row in self._nav_rows:
            if row.page_key == key:
                self.sidebar.select_row(row)
                break
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def refresh_all(self) -> None:
        """Rescan devices and backup folders, and let every page re-read the filesystem."""
        self.state.scan_backups()
        self.state.refresh_devices(refresh_details=True)
        self.state.emit("refresh")
        self.show_toast("Refreshed devices and backup locations")

    # ------------------------------------------------------------------
    def _poll_devices(self) -> bool:
        if self.get_visible() and self.state.tool_status.get("idevice_id"):
            self.state.refresh_devices()
        return GLib.SOURCE_CONTINUE

    def _update_subtitle(self) -> None:
        devices = len(self.state.devices)
        backups = len(self.state.backups)
        if devices:
            subtitle = f"{devices} device(s) · {backups} backup(s)"
        else:
            subtitle = f"No device connected · {backups} backup(s)"
        self.window_title.set_subtitle(subtitle)

    def set_busy(self, message: str) -> None:
        self.spinner.start()
        self.window_title.set_subtitle(message)

    def show_busy(self, message: str) -> None:
        self.set_busy(message)

    def hide_busy(self) -> None:
        self.spinner.stop()
        self._update_subtitle()

    # ------------------------------------------------------------------ background jobs
    def track_job_window(self, window) -> None:
        """Follow a progress window so its work can be watched from the header."""
        self._job_window = window
        self.job_banner.set_title("Working…")
        self.job_banner.set_revealed(True)

    def update_job_progress(self, text: str) -> None:
        if self.job_banner.get_revealed() and text:
            self.job_banner.set_title(text)

    def clear_job_window(self) -> None:
        self._job_window = None
        self.job_banner.set_revealed(False)

    def _show_job_window(self) -> None:
        window = self._job_window
        if window is None:
            self.job_banner.set_revealed(False)
            return
        window.present()

    def show_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def show_report(self, title: str, body: str) -> None:
        label = Gtk.Label(label=body, xalign=0, selectable=True, wrap=False)
        label.add_css_class("monospace")
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(label)
        scroller.set_min_content_height(240)
        scroller.set_min_content_width(520)
        dialog = Adw.MessageDialog(transient_for=self, modal=True, heading=title)
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.present()

    def apply_theme(self) -> None:
        style = Adw.StyleManager.get_default()
        theme = self.state.settings.theme
        if theme == "light":
            style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif theme == "dark":
            style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            style.set_color_scheme(Adw.ColorScheme.DEFAULT)
