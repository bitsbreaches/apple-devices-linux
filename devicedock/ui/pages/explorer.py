# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""User-facing backup browser with category navigation and bounded file previews."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ...asyncutil import run_in_thread
from ...backup.file_catalog import CatalogCategory, CatalogFile, UserFileCatalog, build_user_file_catalog, export_catalog_files
from ...backup.vault import PAYLOAD_MISSING, PAYLOAD_PRESENT, PayloadMissingError
from ...config import APP_CACHE_DIR
from ...models import human_size
from ...previews import PreviewCache, prepare_preview, stage_backup_file
from .. import dialogs
from ..file_preview import FilePreviewPane
from ..widgets import DataTable, PageScaffold, RowItem, Space, caption, icon_text_button, scrolled
from .base import Page

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogFolder:
    name: str
    path: str
    item_count: int


class ExplorerPage(Page):
    title = "Explore Backups"
    icon_name = "folder-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self.catalog: UserFileCatalog | None = None
        self.current_category: CatalogCategory | None = None
        self.current_folder = ""
        self.preview_cache: PreviewCache | None = None
        self._cache_vault = None
        self._preview_job = None

        self.banner = Adw.Banner(revealed=False)
        self.crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.crumbs.add_css_class("breadcrumb")
        self.crumbs.set_hexpand(True)
        self.up_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent folder")
        self.up_button.add_css_class("flat")
        self.up_button.connect("clicked", lambda *_: self._go_up())
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search files by name, type or folder…",
                                            hexpand=True)
        self.search_entry.connect("activate", self._search)
        sort_model = Gtk.StringList.new(["Name", "Type", "Size", "Modified"])
        self.sort_dropdown = Gtk.DropDown(model=sort_model)
        self.sort_dropdown.set_tooltip_text("Sort files")
        self.sort_dropdown.connect("notify::selected", lambda *_: self._render_current())
        self.export_button = icon_text_button("Export selected…", "document-save-symbolic")
        self.export_button.connect("clicked", lambda *_: self._export_selected())
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        nav.append(self.up_button)
        nav.append(self.crumbs)
        nav.append(self.search_entry)
        nav.append(self.sort_dropdown)
        nav.append(self.export_button)

        self.table = DataTable(
            ["Name", "Type", "Size", "Modified"],
            on_activate=self._activate,
            on_selection=self._selection_changed,
            multi_select=True,
        )
        self.preview = FilePreviewPane()
        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        middle.append(nav)
        middle.append(self.table)

        vertical = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        vertical.set_position(430)
        vertical.set_wide_handle(True)
        vertical.set_shrink_end_child(False)
        vertical.set_start_child(middle)
        vertical.set_end_child(self.preview)

        self.category_list = Gtk.ListBox()
        self.category_list.add_css_class("navigation-sidebar")
        self.category_list.connect("row-selected", self._category_selected)
        horizontal = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        horizontal.set_position(230)
        horizontal.set_shrink_start_child(False)
        horizontal.set_wide_handle(True)
        horizontal.set_start_child(scrolled(self.category_list))
        horizontal.set_end_child(vertical)
        horizontal.set_vexpand(True)

        self.scaffold = PageScaffold(
            "Explore Backups",
            "Browse photos, videos, music and documents from the unlocked backup.",
            scroll=False,
            max_width=1500,
        )
        self.append(self.scaffold)
        self.scaffold.add(self.banner)
        self.scaffold.add(horizontal)
        self.footer_label = caption("")
        self.scaffold.set_footer(self.footer_label)
        self.state.subscribe("vault", lambda *_: self.refresh())
        self.window.connect("close-request", self._close_preview_cache)
        self.refresh()

    def refresh(self) -> None:
        vault = self.state.vault
        if vault is None or not vault.unlocked:
            self._close_preview_cache()
            self.catalog = None
            self.current_category = None
            self.category_list.remove_all()
            self.table.clear()
            self.preview.clear("Unlock a backup to browse its user files.")
            self._render_crumbs()
            self.footer_label.set_text("No unlocked backup")
            self._set_banner("Open an unlocked backup to browse files.", "warning")
            return
        if self._cache_vault is not vault:
            self._close_preview_cache()
            self._cache_vault = vault
        self._set_banner("", "info")
        self._render_crumbs(loading=True)
        self.footer_label.set_text("Indexing files from the backup…")

        def work(job):
            return build_user_file_catalog(vault.manifest(), job)

        def success(catalog: UserFileCatalog) -> None:
            self.catalog = catalog
            previous_id = self.current_category.id if self.current_category else None
            self.current_category = None
            self.category_list.remove_all()
            selected = None
            for category in catalog.categories:
                row = Gtk.ListBoxRow()
                row.category_id = category.id
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
                box.set_margin_top(Space.S)
                box.set_margin_bottom(Space.S)
                box.set_margin_start(Space.M)
                box.set_margin_end(Space.M)
                icon = Gtk.Image.new_from_icon_name(category.icon)
                label = Gtk.Label(label=category.title, xalign=0, hexpand=True, wrap=True)
                count = Gtk.Label(label=f"{category.count:,}")
                count.add_css_class("dim-label")
                count.add_css_class("caption")
                box.append(icon)
                box.append(label)
                box.append(count)
                row.set_child(box)
                self.category_list.append(row)
                if category.id == previous_id:
                    selected = row
            self.footer_label.set_text(
                f"{catalog.total_scanned:,} backup records scanned · "
                f"{sum(item.count for item in catalog.categories):,} user-file entries")
            if not catalog.categories:
                self.table.clear()
                self.preview.clear("This backup contains no supported user files.")
                self._render_crumbs()
                self._set_banner("No browsable user files were found in this backup.", "info")
                return
            self._set_banner("", "info")
            self.category_list.select_row(selected or self.category_list.get_row_at_index(0))

        run_in_thread(work, name="index-backup-files", on_success=success,
                      on_error=lambda exc: self._set_banner(str(exc), "error"))

    def _category_selected(self, _listbox, row) -> None:
        if row is None or self.catalog is None:
            return
        self.current_category = next((item for item in self.catalog.categories
                                      if item.id == row.category_id), None)
        self.current_folder = ""
        self.search_entry.set_text("")
        self._render_current()

    def _render_current(self) -> None:
        category = self.current_category
        catalog = self.catalog
        if category is None or catalog is None:
            self.table.clear()
            return
        self._render_crumbs()
        files = catalog.files.get(category.id, [])
        folders: dict[str, int] = {}
        visible_files: list[CatalogFile] = []
        prefix = self.current_folder + "/" if self.current_folder else ""
        for entry in files:
            if self.current_folder:
                if entry.folder == self.current_folder:
                    visible_files.append(entry)
                    continue
                if not entry.folder.startswith(prefix):
                    continue
                remainder = entry.folder[len(prefix):]
            else:
                remainder = entry.folder
            child = remainder.split("/", 1)[0]
            if child:
                folders[child] = folders.get(child, 0) + 1

        folder_rows = [RowItem([name, "Folder", f"{count:,} item(s)", "—"],
                               CatalogFolder(name, f"{prefix}{name}".strip("/"), count))
                       for name, count in sorted(folders.items(), key=lambda item: item[0].casefold())]
        mode = self.sort_dropdown.get_selected()
        sorters = (
            lambda item: item.name.casefold(),
            lambda item: item.file_type.casefold(),
            lambda item: item.size if item.size is not None else -1,
            lambda item: item.record.mtime if item.record.mtime is not None else -1,
        )
        visible_files.sort(key=sorters[mode] if mode < len(sorters) else sorters[0])
        file_rows = [RowItem([entry.name, entry.file_type, human_size(entry.size), entry.modified], entry)
                     for entry in visible_files]
        rows = folder_rows + file_rows
        self.table.set_rows(rows)
        total_bytes = sum(entry.size or 0 for entry in visible_files)
        self.footer_label.set_text(
            f"{len(rows):,} item(s) · {len(folder_rows):,} folder(s) · "
            f"{human_size(total_bytes)} in visible files")
        self.preview.clear("Select a file to preview it. Use Ctrl or Shift to select several files for export.")

    def _activate(self, item: RowItem) -> None:
        if isinstance(item.payload, CatalogFolder):
            self.current_folder = item.payload.path
            self.search_entry.set_text("")
            self._render_current()
        elif isinstance(item.payload, CatalogFile):
            self._preview(item.payload)

    def _selection_changed(self, _item: RowItem | None) -> None:
        selected = self.table.selected_items()
        if len(selected) > 1:
            self.preview.clear(f"{len(selected)} files selected. Export them or select one file to preview.")
        elif selected and isinstance(selected[0].payload, CatalogFile):
            self._preview(selected[0].payload)
        elif selected and isinstance(selected[0].payload, CatalogFolder):
            folder = selected[0].payload
            self.preview.clear(f"{folder.name} · {folder.item_count:,} file(s). Activate to open this folder.")

    def _go_up(self) -> None:
        if not self.current_folder:
            return
        self.current_folder = str(Path(self.current_folder).parent)
        if self.current_folder == ".":
            self.current_folder = ""
        self._render_current()

    def _render_crumbs(self, *, loading: bool = False) -> None:
        child = self.crumbs.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.crumbs.remove(child)
            child = nxt
        self.up_button.set_sensitive(bool(self.current_folder))
        if loading:
            self._crumb_label("Indexing backup…")
            return
        if self.current_category is None:
            self._crumb_label("Choose a file category")
            return
        self._crumb_button(self.current_category.title, "")
        parts = [part for part in self.current_folder.split("/") if part]
        for index, part in enumerate(parts):
            separator = Gtk.Label(label="›")
            separator.add_css_class("dim-label")
            self.crumbs.append(separator)
            self._crumb_button(part, "/".join(parts[:index + 1]))

    def _crumb_button(self, text: str, path: str) -> None:
        button = Gtk.Button(label=text)
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self._go_to(path))
        self.crumbs.append(button)

    def _crumb_label(self, text: str) -> None:
        self.crumbs.append(caption(text))

    def _go_to(self, path: str) -> None:
        self.search_entry.set_text("")
        self.current_folder = path
        self._render_current()

    def _search(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip().casefold()
        if not query:
            self._render_current()
            return
        if self.catalog is None:
            return
        categories = [self.current_category.id] if self.current_category else list(self.catalog.files)
        matches = [item for category in categories for item in self.catalog.files.get(category, [])
                   if query in item.name.casefold() or query in item.file_type.casefold()
                   or query in item.folder.casefold()]
        rows = [RowItem([item.name, item.file_type, human_size(item.size), item.folder], item)
                for item in sorted(matches, key=lambda value: value.name.casefold())]
        self.table.set_rows(rows)
        self.footer_label.set_text(f"{len(rows):,} match(es) for “{entry.get_text().strip()}”")
        self._render_crumbs()
        self.preview.clear("Select a search result to preview it.")

    def _preview(self, entry: CatalogFile) -> None:
        vault = self.state.vault
        if vault is None:
            return
        if self.preview_cache is None:
            self.preview_cache = PreviewCache(APP_CACHE_DIR)
        cache = self.preview_cache
        self.preview.show_loading(entry.name)
        preview_file_id = entry.record.file_id
        self._preview_file_id = preview_file_id
        # Presence is checked before staging so a pruned payload is named as such instead of
        # being prepared as if it were an empty file.
        state = vault.payload_state(entry.record)
        manifest_size = entry.record.size
        state_text = {
            PAYLOAD_PRESENT: "present",
            PAYLOAD_MISSING: "missing from this backup",
            "empty": "zero bytes",
            "unreadable": "not readable",
            "not-a-file": "not a regular file",
        }.get(state, state)
        technical = (
            f"Domain: {entry.record.domain}\n"
            f"Relative path: {entry.record.relative_path}\n"
            f"Manifest ID: {entry.record.file_id}\n"
            f"Protection class: {entry.record.protection_class if entry.record.protection_class is not None else '—'}\n"
            f"Payload: {state_text}"
            + (f"\nManifest size: {manifest_size:,} bytes"
               if manifest_size is not None else "")
        )
        if state == PAYLOAD_MISSING:
            if self._preview_job is not None:
                self._preview_job.cancel()
            self.preview.clear(
                "The payload for this file is not present in this backup. iOS prunes some "
                "files, and an interrupted transfer can leave the manifest entry behind, so "
                "there is nothing to preview or export.")
            return

        def work(job):
            path = stage_backup_file(cache, vault, entry.record, job)
            return prepare_preview(path, job)

        def success(document) -> None:
            if self._preview_file_id != preview_file_id:
                return
            self.preview.show_document(entry.name, document, technical)

        def failed(exc: Exception) -> None:
            if self._preview_file_id != preview_file_id:
                return
            if isinstance(exc, PayloadMissingError):
                self.preview.clear("The payload for this file disappeared from the backup "
                                   "after it was listed. Refresh the category and try again.")
            else:
                self.preview.clear(f"Preview unavailable: {exc}")

        if self._preview_job is not None:
            self._preview_job.cancel()
        _thread, self._preview_job = run_in_thread(
            work, name="prepare-backup-preview", on_success=success, on_error=failed,
        )

    def _export_selected(self) -> None:
        selected = self.table.selected_items()
        if not selected:
            dialogs.show_info(self.window, "Nothing selected", "Select one or more files or folders to export.")
            return
        files: dict[str, CatalogFile] = {}
        for item in selected:
            payload = item.payload
            if isinstance(payload, CatalogFile):
                files[payload.record.file_id] = payload
            elif isinstance(payload, CatalogFolder) and self.current_category is not None:
                folder_prefix = payload.path + "/"
                for entry in self.catalog.files.get(self.current_category.id, []):
                    if entry.folder == payload.path or entry.folder.startswith(folder_prefix):
                        files[entry.record.file_id] = entry
        if not files:
            dialogs.show_info(self.window, "Nothing to export", "The selection has no exportable files.")
            return
        dialog = Gtk.FileDialog(title="Export selected user files")
        dialog.select_folder(self.window, None, self._export_folder_chosen, list(files.values()))

    def _export_folder_chosen(self, dialog: Gtk.FileDialog, result, entries) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        root = Path(folder.get_path())
        vault = self.state.vault
        if vault is None:
            return
        progress = dialogs.ProgressDialog(self.window, "Exporting files", f"{len(entries):,} file(s)")

        def work(job):
            return export_catalog_files(vault, entries, root, job)

        def done(export_result) -> None:
            message = export_result.summary
            if export_result.errors:
                message += "\n\n" + "\n".join(export_result.errors[:5])
            dialogs.show_info(self.window, "Export finished", message)

        progress.start(work, on_success=done, name="export-user-files")

    def _close_preview_cache(self, *_args) -> bool:
        if self._preview_job is not None:
            self._preview_job.cancel()
        if self.preview_cache is not None:
            self.preview_cache.close()
            self.preview_cache = None
        self._cache_vault = None
        return False

    def _set_banner(self, message: str, kind: str) -> None:
        self.banner.set_title(message or "")
        self.banner.set_revealed(bool(message))
        for name in ("error", "warning"):
            self.banner.remove_css_class(name)
        if kind in ("error", "warning"):
            self.banner.add_css_class(kind)

