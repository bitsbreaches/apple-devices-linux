# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Data categories page: readable Contacts, Messages, Calls, Media, Notes, Calendar, Safari."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ...asyncutil import run_in_thread
from ...backup import export_category, load_category, user_file_categories
from .. import dialogs
from ..widgets import (
    DataTable,
    EmptyState,
    PageScaffold,
    RowItem,
    Space,
    caption,
    count_pill,
    icon_text_button,
    scrolled,
)
from .base import Page

log = logging.getLogger(__name__)


class CategoriesPage(Page):
    title = "Categories"
    icon_name = "view-list-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self.current_id: str | None = None
        self.current_result = None
        self.categories = {}

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._category_selected)
        export_button = icon_text_button("Export…", "document-save-symbolic")
        export_button.connect("clicked", lambda _b: self._export())
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload category")
        refresh_button.connect("clicked", lambda _b: self._load(self.current_id))

        self.scaffold = PageScaffold(
            "Data categories",
            "Readable views over the data that exists in the opened backup.",
            actions=[refresh_button, export_button],
            scroll=False,
            max_width=1350,
        )
        self.append(self.scaffold)

        self.title_label = Gtk.Label(xalign=0, wrap=True)
        self.title_label.add_css_class("title-3")
        self.description = Gtk.Label(xalign=0, wrap=True)
        self.description.add_css_class("dim-label")
        self.description.add_css_class("caption")
        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        self.count_label = count_pill("")
        self.footnote = caption("")

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        titles.set_hexpand(True)
        titles.append(self.title_label)
        titles.append(self.description)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        header.append(titles)
        header.append(self.count_label)

        self.table = DataTable(["Loading…"])
        self.empty = EmptyState(
            "view-list-symbolic", "Choose a category",
            "Select a category on the left. Categories that this backup does not contain, or "
            "whose schema is not recognised, are reported instead of shown as empty.")

        self.table_area = Gtk.Stack()
        self.table_area.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.table_area.set_vexpand(True)
        self.table_area.add_named(self.table, "table")
        self.table_area.add_named(self.empty, "empty")
        self.table_area.set_visible_child_name("empty")

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        right.set_margin_start(Space.L)
        right.append(header)
        right.append(self.banner)
        right.append(self.table_area)
        right.append(self.footnote)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(240)
        paned.set_wide_handle(True)
        paned.set_start_child(scrolled(self.listbox))
        paned.set_end_child(right)
        paned.set_vexpand(True)
        self.scaffold.add(paned)

        self.state.subscribe("vault", lambda *_: self._vault_changed())
        self._vault_changed()

    # ------------------------------------------------------------------
    def _vault_changed(self) -> None:
        if self.state.vault is None or not self.state.vault.unlocked:
            self.categories.clear()
            self.current_id = None
            while row := self.listbox.get_row_at_index(0):
                self.listbox.remove(row)
            self._set_banner("Open a backup first: Backups → Unlock.", "warning")
            self.table.clear()
            self.title_label.set_text("Data categories")
            self.description.set_text(
                "Readable views over the data that exists in the opened backup.")
            self.count_label.set_text("")
            self.footnote.set_text("")
            return
        self._set_banner("", "info")
        vault = self.state.vault

        def work(job):
            return user_file_categories(vault, job)

        def success(categories) -> None:
            selected_id = self.current_id
            self.categories = {category.id: category for category in categories}
            while row := self.listbox.get_row_at_index(0):
                self.listbox.remove(row)
            for category in categories:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
                for setter, value in ((box.set_margin_top, Space.S), (box.set_margin_bottom, Space.S),
                                      (box.set_margin_start, Space.M), (box.set_margin_end, Space.M)):
                    setter(value)
                if category.id.startswith("documents-location:"):
                    box.set_margin_start(Space.XL)
                icon = Gtk.Image.new_from_icon_name(category.icon)
                icon.set_pixel_size(16)
                label = Gtk.Label(label=category.title, xalign=0)
                label.set_wrap(True)
                box.append(icon)
                box.append(label)
                row.set_child(box)
                row.category_id = category.id
                self.listbox.append(row)
            selected = next((row for row in self._rows()
                             if row.category_id == selected_id), None)
            self.listbox.select_row(selected or self.listbox.get_row_at_index(0))
            if not categories:
                self.table_area.set_visible_child_name("empty")
                self.empty.set_title("No user files found")
                self.empty.set_description(
                    "This backup contains no browsable photos, music, videos, books, podcasts, or documents.")
                self.title_label.set_text("User files")
                self.description.set_text("Files are listed when they exist in the backup.")
                self.count_label.set_text("")
                self.footnote.set_text("")

        run_in_thread(work, name="category-discovery", on_success=success,
                      on_error=lambda exc: self._set_banner(str(exc), "error"))

    def _rows(self):
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            yield row
            row = self.listbox.get_row_at_index(row.get_index() + 1)

    def _category_selected(self, _listbox, row) -> None:
        if row is None:
            return
        self._load(row.category_id)

    def _load(self, category_id: str | None) -> None:
        if category_id is None or self.state.vault is None:
            return
        self.current_id = category_id
        category = self.categories.get(category_id)
        if category is None:
            return
        self.title_label.set_text(category.title)
        self.description.set_text(category.description)
        self.count_label.set_text("")
        self.footnote.set_text("Loading…")
        vault = self.state.vault

        def work(job):
            return load_category(category_id, vault, job, limit=2000)

        def success(result) -> None:
            self.current_result = result
            if not result.available:
                self._set_banner("", "info")
                self.empty.set_title("Not in this backup")
                self.empty.set_description(
                    result.error or f"This backup has no readable {category.title} data.")
                self.table.set_rows([])
                self.count_label.set_text("unavailable")
                self.footnote.set_text("")
                self.table_area.set_visible_child_name("empty")
                return
            self._set_banner(result.note, "info")
            self.table_area.set_visible_child_name("table")
            self.table.set_columns(result.columns)
            self.table.set_rows([RowItem(row) for row in result.rows])
            shown = len(result.rows)
            total = result.total or shown
            self.count_label.set_text(f"{total:,} record(s)")
            if result.truncated:
                self.footnote.set_text(
                    f"Showing the first {shown:,} of {total:,} records; export for all of them.")
            else:
                self.footnote.set_text(f"{shown:,} record(s) shown")

        run_in_thread(work, name="category", on_success=success,
                      on_error=lambda exc: self._set_banner(str(exc), "error"))

    def _export(self) -> None:
        result = self.current_result
        if result is None or not result.available:
            dialogs.show_info(self.window, "Nothing to export",
                              "Choose an available category with rows first.")
            return
        dialog = Gtk.FileDialog(title="Export category", initial_name=f"{result.category_id}.csv")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        for name, suffixes in (("CSV table", ["csv"]), ("JSON", ["json"]), ("HTML", ["html"])):
            file_filter = Gtk.FileFilter()
            file_filter.set_name(name)
            for suffix in suffixes:
                file_filter.add_suffix(suffix)
            filters.append(file_filter)
        dialog.set_filters(filters)
        dialog.save(self.window, None, self._export_chosen, result)

    def _export_chosen(self, dialog: Gtk.FileDialog, result, category_result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        if file is None or file.get_path() is None:
            return
        try:
            path = export_category(category_result, Path(file.get_path()))
        except Exception as exc:  # noqa: BLE001
            dialogs.show_error(self.window, "Export failed", str(exc))
            return
        dialogs.show_info(self.window, "Exported", str(path))

    def _set_banner(self, message: str, kind: str) -> None:
        self.banner.set_title(message or "")
        self.banner.set_revealed(bool(message))
        for name in ("error", "warning"):
            self.banner.remove_css_class(name)
        if kind in ("error", "warning"):
            self.banner.add_css_class(kind)
