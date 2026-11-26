# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Live, non-invasive file browser for connected iPhones."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..asyncutil import run_in_thread
from ..config import APP_CACHE_DIR
from ..devices import media_files
from ..models import human_size
from ..previews import PreviewCache, prepare_preview, stage_phone_file
from . import dialogs, selection
from .file_preview import FilePreviewPane
from .widgets import Space, caption, icon_text_button, scrolled


class PhoneFilesBrowser(Gtk.Box):
    """Reusable browser for the phone media area and shared app Documents."""

    FILTERS = ("Select file type…", "All files", "Videos", "Music",
               "Podcasts", "Books", "Documents")
    FILTER_KIND = {"All files": "All", "Videos": "Video",
                   "Music": "Music", "Podcasts": "Podcast", "Books": "Book",
                   "Documents": "Document"}

    def __init__(self, device) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.udid = device.udid
        # A successful ideviceinfo session during discovery already proves that Lockdown
        # accepted this host's pair record. Avoid opening a second service just to ask again.
        self._pairing_verified = not bool(device.error)
        self.shared_apps: list[tuple[str, str]] = []
        self.bundle_id: str | None = None
        self.path = "/"
        self.entries: list[media_files.PhoneFile] = []
        self.source_warning = ""
        self.preview_cache = PreviewCache(APP_CACHE_DIR)
        self._preview_path: str | None = None
        self._preview_job = None
        self._source_job = None
        self._source_generation = 0
        self._source_check_running = False
        self._listing_cache: dict[tuple[str, str, str | None], list[media_files.PhoneFile]] = {}
        self._listing_generation = 0
        self._error_dialog: Adw.MessageDialog | None = None
        self._error_signature: tuple[str, str] | None = None
        self.preview = FilePreviewPane()
        filter_model = Gtk.StringList.new(list(self.FILTERS))
        self.filter_dropdown = Gtk.DropDown(model=filter_model)
        self.filter_dropdown.connect("notify::selected", self._filter_changed)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Filter this folder…", hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self._filter_rows())
        self.up_button = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent folder")
        self.up_button.connect("clicked", lambda *_: self._go_up())
        self.refresh_button = Gtk.Button(label="Retry Connection")
        self.refresh_button.set_tooltip_text("Recheck prerequisites, pairing and connection, then refresh files")
        self.refresh_button.connect("clicked", lambda *_: self.retry_connection())
        self.location = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        self.location.set_hexpand(True)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        controls.set_margin_top(Space.M)
        controls.set_margin_start(Space.M)
        controls.set_margin_end(Space.M)
        controls.append(self.filter_dropdown)
        controls.append(self.up_button)
        controls.append(self.refresh_button)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE)
        self.listbox.add_css_class("boxed-list")
        # A click selects; a double-click opens. Single-click activation would make it
        # impossible to select a folder without navigating into it.
        self.listbox.set_activate_on_single_click(False)
        self.listbox.connect("row-activated", self._activate)
        self.listbox.connect("selected-rows-changed", self._selection_changed)
        # Selection is driven here rather than by GtkListBox: its MULTIPLE mode only ever
        # adds rows on a plain click, so a second click could not deselect anything.
        self._selection_anchor: str | None = None
        self._selection_keys: frozenset[str] = frozenset()
        self._applying_selection = False
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._row_pressed)
        self.listbox.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)
        self.empty = Gtk.Label(label="Connect and unlock the phone to browse files.",
                               wrap=True, vexpand=True, valign=Gtk.Align.CENTER)
        self.empty.add_css_class("dim-label")
        self.stack = Gtk.Stack()
        self.stack.add_named(scrolled(self.listbox), "files")
        self.stack.add_named(self.empty, "empty")
        self.stack.set_visible_child_name("files")
        browser_pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        browser_pane.set_position(520)
        browser_pane.set_wide_handle(True)
        browser_pane.set_shrink_end_child(False)
        browser_pane.set_start_child(self.stack)
        browser_pane.set_end_child(self.preview)
        browser_pane.set_vexpand(True)

        self.status = caption("Choose files to copy to this computer, or add files to the phone.")
        self.status.set_wrap(True)
        self.status.set_selectable(True)
        self.status.set_margin_start(Space.M)
        self.status.set_margin_end(Space.M)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        actions.set_margin_bottom(Space.M)
        actions.set_margin_start(Space.M)
        actions.set_margin_end(Space.M)
        self.download_button = icon_text_button("Copy selected to computer…", "document-save-symbolic")
        self.download_button.connect("clicked", self._choose_download_folder)
        self.upload_button = icon_text_button("Import Files…", "list-add-symbolic")
        self.upload_button.connect("clicked", self._choose_upload_files)
        self._set_import_enabled(False)
        actions.append(self.download_button)
        actions.append(self.upload_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        content.append(controls)
        content.append(self.location)
        content.append(self.search_entry)
        content.append(browser_pane)
        content.append(self.status)
        content.append(actions)
        self.append(content)
        self._load_sources()

    def _host_window(self):
        host = self.get_root()
        return host if isinstance(host, Gtk.Window) else None

    def _load_sources(self) -> None:
        if self._source_check_running:
            return
        self._source_check_running = True
        self.refresh_button.set_sensitive(False)
        self._source_generation += 1
        generation = self._source_generation

        def work(job):
            job.check_cancelled()
            problem = media_files.file_browser_preflight(
                self.udid, pairing_verified=self._pairing_verified)
            if problem:
                return [], problem, True
            job.check_cancelled()
            try:
                return media_files.shared_document_apps(self.udid), None, False
            except Exception as exc:  # media browsing remains useful if apps cannot be listed
                return [], str(exc), False

        def success(result) -> None:
            if generation != self._source_generation:
                return
            apps, error, preflight_failed = result
            if preflight_failed:
                self.source_warning = ""
                self.shared_apps = []
                self.bundle_id = None
                self._set_import_enabled(False)
                self.status.set_text(error)
                self.empty.set_text(error)
                self.stack.set_visible_child_name("empty")
                return
            self._pairing_verified = True
            self.shared_apps = apps
            self.source_warning = ""
            if error:
                self.source_warning = f"Some shared folders could not be checked. {error}"
            # Shared app folders are part of the Documents root listing, so a list cached
            # before app discovery finished would hide them (and leave Import disabled).
            self._listing_cache.clear()
            # Keep the category prompt visible until the user chooses what to browse.
            if self._selected_kind() is not None:
                self._load()

        def done() -> None:
            if generation == self._source_generation:
                self._source_check_running = False
                self.refresh_button.set_sensitive(True)

        _thread, self._source_job = run_in_thread(
            work, name="phone-file-sources", on_success=success,
            on_error=lambda exc: self._show_error("Could not inspect phone files", str(exc))
            if generation == self._source_generation else None,
            on_done=done,
        )

    def retry_connection(self) -> None:
        self._dismiss_error_dialog()
        self._listing_cache.clear()
        self._pairing_verified = False
        self.status.set_text("Checking iPhone, usbmuxd and FUSE…")
        self._load_sources()

    def preflight(self) -> str | None:
        """Expose the current connection check for page-level retry/status UI."""
        return media_files.file_browser_preflight(self.udid)

    def _selected_kind(self) -> str | None:
        selected = self.filter_dropdown.get_selected()
        if selected == 0:
            return None
        label = self.FILTERS[selected] if selected < len(self.FILTERS) else "All files"
        return self.FILTER_KIND[label]

    def _filter_changed(self, *_args) -> None:
        self.path = "/"
        self.bundle_id = None
        self._set_import_enabled(False)
        self._load()

    def _load(self) -> None:
        self.up_button.set_sensitive(self.path != "/")
        self._render_breadcrumbs()
        kind = self._selected_kind()
        self._listing_generation += 1
        generation = self._listing_generation
        if kind is None:
            self._preview_path = None
            if self._preview_job is not None:
                self._preview_job.cancel()
            self._clear_selection()
            self.listbox.remove_all()
            self.search_entry.set_text("")
            self.preview.clear("Select a file type to preview an item.")
            self.empty.set_text("Select a file type above to browse items on this iPhone.")
            self.stack.set_visible_child_name("empty")
            self.status.set_text("Select a file type above to browse items on this iPhone.")
            return
        self.status.set_text("Loading phone files…")
        udid, path, bundle_id = self.udid, self.path, self.bundle_id
        cache_key = (path, kind, bundle_id)

        def work(_job):
            entries = media_files.list_files(udid, path, kind, bundle_id)
            if kind == "Document" and path == "/" and bundle_id is None:
                # ifuse --list-apps only returns apps with File Sharing enabled, so every
                # entry is genuinely reachable through --documents. Listing them without an
                # extra mount per app keeps enumeration fast; these are real shared
                # folders rather than placeholders.
                entries.extend(media_files.shared_app_folders(
                    self.shared_apps,
                    [entry.name for entry in entries if entry.is_dir],
                ))
            return entries

        def success(entries) -> None:
            self._listing_cache[cache_key] = entries
            if generation != self._listing_generation:
                return
            self.entries = entries
            self._selection_anchor = None
            self._selection_keys = frozenset()
            self._applying_selection = True
            try:
                self.listbox.remove_all()
                for entry in entries:
                    self.listbox.append(self._file_row(entry))
            finally:
                self._applying_selection = False
            self.search_entry.set_text("")
            if entries:
                self.stack.set_visible_child_name("files")
                note = (f"{len(entries):,} item(s). Double-click a folder to open it; click to "
                        "select, Ctrl-click to add or remove, Shift-click for a range.")
                self.status.set_text(f"{note} {self.source_warning}".strip())
            else:
                if kind == "Document" and path == "/" and bundle_id is None:
                    message = (
                        "No accessible documents were found. Device Dock can show shared "
                        "files from apps that support File Sharing."
                    )
                elif kind == "Book":
                    message = "No readable book files were found on this iPhone."
                else:
                    message = "No matching files in this location."
                self.empty.set_text(message)
                self.stack.set_visible_child_name("empty")
                self.status.set_text(self.source_warning or message)

        if cache_key in self._listing_cache:
            success(self._listing_cache[cache_key])
            return
        run_in_thread(
            work, name="phone-file-list", on_success=success,
            on_error=lambda exc: self._show_error("Could not browse phone files", str(exc))
            if generation == self._listing_generation else None,
        )

    def _file_row(self, entry: media_files.PhoneFile) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.phone_file = entry
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        line.set_margin_top(Space.S)
        line.set_margin_bottom(Space.S)
        line.set_margin_start(Space.M)
        line.set_margin_end(Space.M)
        if entry.is_dir:
            line.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
        else:
            line.append(Gtk.Image.new_from_icon_name(self._icon(entry.kind)))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        label = self._display_name(entry)
        title = Gtk.Label(label=label, xalign=0, ellipsize=3)
        detail = Gtk.Label(
            label=("Folder" if entry.is_dir else
                   " · ".join(value for value in
                              (entry.kind, human_size(entry.size), entry.modified) if value)),
            xalign=0,
        )
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        labels.append(title)
        labels.append(detail)
        line.append(labels)
        if entry.is_dir:
            line.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_child(line)
        query = self.search_entry.get_text().strip().casefold()
        row.set_visible(not query or query in label.casefold()
                        or query in entry.kind.casefold() or query in entry.path.casefold())
        return row

    @staticmethod
    def _display_name(entry: media_files.PhoneFile) -> str:
        if entry.is_dir and not entry.is_source:
            return media_files.display_folder_name(entry.name)
        return entry.name

    @staticmethod
    def _icon(kind: str) -> str:
        return {"Photo": "image-x-generic-symbolic", "Video": "video-x-generic-symbolic",
                "Music": "audio-x-generic-symbolic", "Podcast": "audio-x-generic-symbolic",
                "Book": "x-office-book-symbolic", "Document": "x-office-document-symbolic"}.get(
                    kind, "text-x-generic-symbolic")

    def _activate(self, _listbox, row) -> None:
        entry = getattr(row, "phone_file", None)
        if entry is None:
            return
        if entry.is_source and entry.bundle_id:
            self.bundle_id = entry.bundle_id
            self.path = "/"
            self._set_import_enabled(True)
            self._load()
            return
        if entry.is_dir:
            self.path = entry.path
            self._load()
        else:
            self._preview_file(entry)

    # ------------------------------------------------------------------ selection
    @staticmethod
    def _row_key(row) -> str | None:
        entry = getattr(row, "phone_file", None)
        if entry is None:
            return None
        return f"{entry.bundle_id or ''}\0{entry.path}\0{entry.name if entry.is_source else ''}"

    def _ordered_keys(self) -> list[str]:
        keys: list[str] = []
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            key = self._row_key(row)
            if key is not None and row.get_visible():
                keys.append(key)
            row = self.listbox.get_row_at_index(row.get_index() + 1)
        return keys

    def _row_pressed(self, gesture, n_press: int, _x: float, y: float) -> bool:
        """Apply click / Ctrl+click / Shift+click rules; a plain double-click opens the row."""
        row = self.listbox.get_row_at_y(int(y))
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        state = gesture.get_current_event_state()
        modified = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
        # Two quick Ctrl+clicks on the same row mean "add, then remove", not "open": only an
        # unmodified double-click activates.
        if n_press >= 2 and not modified:
            if row is not None:
                self._activate(self.listbox, row)
            return True
        mode = selection.mode_for(
            control=bool(state & Gdk.ModifierType.CONTROL_MASK),
            shift=bool(state & Gdk.ModifierType.SHIFT_MASK),
        )
        clicked = self._row_key(row) if row is not None else None
        selected, self._selection_anchor = selection.next_selection(
            self._ordered_keys(), self._selection_keys, self._selection_anchor, clicked, mode,
        )
        self._apply_selection(selected)
        return True

    def _key_pressed(self, _controller, keyval: int, _keycode: int, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._selection_anchor = None
            self._apply_selection(frozenset())
            return True
        if keyval in (Gdk.KEY_a, Gdk.KEY_A) and state & Gdk.ModifierType.CONTROL_MASK:
            self._apply_selection(frozenset(self._ordered_keys()))
            return True
        return False

    def _apply_selection(self, selected: frozenset[str]) -> None:
        keep = set(selected)
        self._applying_selection = True
        try:
            row = self.listbox.get_row_at_index(0)
            while row is not None:
                key = self._row_key(row)
                if key is not None:
                    if key in keep:
                        self.listbox.select_row(row)
                        keep.discard(key)
                    else:
                        self.listbox.unselect_row(row)
                row = self.listbox.get_row_at_index(row.get_index() + 1)
        finally:
            self._applying_selection = False
        self._selection_keys = frozenset(selected)
        self._selection_changed(self.listbox)

    def _selection_changed(self, _listbox) -> None:
        if self._applying_selection:
            return
        rows = self.listbox.get_selected_rows()
        self._selection_keys = frozenset(
            key for key in (self._row_key(row) for row in rows) if key
        )
        selected = self._selected_entries()
        if not selected:
            self.preview.clear("Select a file to preview it. Ctrl-click adds or removes one "
                               "item; Shift-click selects a range.")
            self.status.set_text(
                "Nothing selected. Click to select, Ctrl-click to add or remove, "
                "Shift-click for a range, double-click to open.")
            return
        if len(selected) > 1:
            self.status.set_text(f"{len(selected)} items selected. Copy selected to export them.")
            self.preview.clear("Multiple items selected. Select one file to preview it.")
            return
        entry = selected[0]
        if entry.is_dir or entry.is_source:
            self.status.set_text(f"{entry.name} · folder")
            self.preview.clear(f"Double-click “{entry.name}” to browse it.")
            return
        self.status.set_text(f"{entry.name} · {entry.kind} · {human_size(entry.size)}")
        self._preview_file(entry)

    def _selected_entries(self) -> list[media_files.PhoneFile]:
        return [row.phone_file for row in self.listbox.get_selected_rows()
                if getattr(row, "phone_file", None) is not None]

    def _clear_selection(self) -> None:
        self._selection_anchor = None
        self._apply_selection(frozenset())

    def _filter_rows(self) -> None:
        query = self.search_entry.get_text().strip().casefold()
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            entry = getattr(row, "phone_file", None)
            if entry is not None:
                label = self._display_name(entry)
                row.set_visible(not query or query in label.casefold()
                                or query in entry.kind.casefold() or query in entry.path.casefold())
            row = self.listbox.get_row_at_index(row.get_index() + 1)

    def _go_up(self) -> None:
        if self.path == "/":
            return
        self.path = str(Path(self.path).parent)
        if self.path == ".":
            self.path = "/"
        self._load()

    def _source_label(self) -> str:
        if self.bundle_id:
            return next((name for app_id, name in self.shared_apps if app_id == self.bundle_id),
                        "Shared files")
        return "iPhone"

    def _set_import_enabled(self, enabled: bool) -> None:
        """Import is only valid inside a shared app's Documents folder.

        The media area (DCIM, Books, app-managed folders) is read-only through AFC, so the
        button explains why it is unavailable instead of failing after a file is chosen.
        """
        self.upload_button.set_sensitive(enabled)
        if enabled:
            self.upload_button.set_tooltip_text(
                f"Copy files from this computer into {self._source_label()}'s Documents folder")
        else:
            self.upload_button.set_tooltip_text(
                "Open a shared app folder under Documents to import files into that app")

    @staticmethod
    def _friendly_path(path: str) -> str:
        parts = [part for part in Path(path).parts if part != "/"]
        if not parts:
            return "All files"
        return " / ".join(media_files.display_folder_name(part) for part in parts)

    def _render_breadcrumbs(self) -> None:
        while child := self.location.get_first_child():
            self.location.remove(child)
        source = Gtk.Label(label=self._source_label(), xalign=0)
        source.add_css_class("dim-label")
        self.location.append(source)
        button = Gtk.Button(label="All files")
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self._go_to_path("/"))
        self.location.append(button)
        parts = [part for part in Path(self.path).parts if part != "/"]
        prefix = ""
        for part in parts:
            prefix += "/" + part
            self.location.append(Gtk.Label(label="›"))
            crumb = Gtk.Button(label=media_files.display_folder_name(part))
            crumb.add_css_class("flat")
            target = prefix
            crumb.connect("clicked", lambda *_args, path=target: self._go_to_path(path))
            self.location.append(crumb)

    def _go_to_path(self, path: str) -> None:
        if path == "/" and self.bundle_id:
            self.bundle_id = None
            self._set_import_enabled(False)
        self.path = path
        self._load()

    def _preview_file(self, entry: media_files.PhoneFile) -> None:
        self._preview_path = entry.path
        self.preview.show_loading(entry.name)
        udid, remote_path, bundle_id = self.udid, entry.path, self.bundle_id
        cache = self.preview_cache

        def work(job):
            path = stage_phone_file(cache, udid, remote_path, bundle_id, job,
                                    expected_size=entry.size, modified=entry.modified)
            return prepare_preview(path, job)

        def success(document) -> None:
            if self._preview_path != remote_path:
                return
            technical = f"Accessible phone location: {remote_path}"
            if bundle_id:
                technical += f"\nShared app identifier: {bundle_id}"
            self.preview.show_document(entry.name, document, technical)

        if self._preview_job is not None:
            self._preview_job.cancel()
        _thread, self._preview_job = run_in_thread(
            work, name="prepare-phone-preview", on_success=success,
            on_error=lambda exc: self.preview.clear(f"Preview unavailable: {exc}")
            if self._preview_path == remote_path else None,
        )

    def _close_preview_cache(self, *_args) -> bool:
        return self.close_browser(*_args)

    def _choose_download_folder(self, *_args) -> None:
        if not self._selected_entries():
            self.status.set_text("Select one or more files or folders first.")
            return
        dialog = Gtk.FileDialog(title="Copy phone files to a folder")
        dialog.select_folder(self._host_window(), None, self._download_folder_chosen, None)

    def _download_folder_chosen(self, dialog: Gtk.FileDialog, result, _data) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        files = self._selected_entries()
        destination = Path(folder.get_path())
        udid, bundle_id = self.udid, self.bundle_id
        self.status.set_text(f"Copying {len(files)} file(s) from the phone…")

        progress = dialogs.ProgressDialog(self._host_window(), "Copying phone files", f"{len(files):,} file(s)")

        def work(job):
            return media_files.pull_items(udid, files, destination, bundle_id, job)

        def success(copied) -> None:
            self._clear_selection()
            self.status.set_text(f"Copied {len(copied):,} file(s) to {destination}")

        progress.start(work, on_success=success,
                       on_error=lambda exc: self._show_error("Phone copy failed", str(exc)),
                       name="phone-download")

    def _choose_upload_files(self, *_args) -> None:
        if not self.bundle_id:
            self.status.set_text(
                "Open a shared app folder under Documents to import files into that app. "
                "iOS media locations such as Photos are read-only here.")
            return
        dialog = Gtk.FileDialog(title=f"Import files into {self._source_label()}")
        dialog.open_multiple(self._host_window(), None, self._upload_files_chosen, None)

    def _upload_files_chosen(self, dialog: Gtk.FileDialog, result, _data) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())
                 if files.get_item(i).get_path()]
        if not paths:
            return
        udid, bundle_id, remote_folder = self.udid, self.bundle_id, self.path
        app_name = self._source_label()
        self.status.set_text(f"Importing {len(paths)} file(s) into {app_name}…")

        progress = dialogs.ProgressDialog(self._host_window(), f"Importing files into {app_name}",
                                          f"{len(paths):,} file(s)")

        def work(job):
            return media_files.push_files(udid, paths, remote_folder, bundle_id, job)

        def success(_result) -> None:
            self._listing_cache.clear()
            self.status.set_text(f"Imported {len(paths):,} file(s) into {app_name}")
            self._load()

        progress.start(work, on_success=success,
                       on_error=lambda exc: self._show_error("Import failed", str(exc)),
                       name="phone-upload")

    def _show_error(self, title: str, message: str) -> None:
        self.status.set_text(message)
        host = self._host_window()
        if host is None:
            return
        signature = (title, message)
        if self._error_dialog is not None and self._error_dialog.get_visible():
            if self._error_signature != signature:
                self._error_dialog.set_heading(title)
                self._error_dialog.set_body(message)
                self._error_signature = signature
            self._error_dialog.present()
            return
        dialog = dialogs.show_error(host, title, message)
        self._error_dialog = dialog
        self._error_signature = signature
        dialog.connect("response", self._error_dialog_closed)
        dialog.connect("close-request", self._error_dialog_closed)

    def _error_dialog_closed(self, dialog, *_args) -> bool:
        if dialog is self._error_dialog:
            self._error_dialog = None
            self._error_signature = None
        return False

    def _dismiss_error_dialog(self) -> None:
        dialog, self._error_dialog = self._error_dialog, None
        self._error_signature = None
        if dialog is not None:
            dialog.close()

    def close_browser(self, *_args) -> bool:
        self._dismiss_error_dialog()
        if self._source_job is not None:
            self._source_job.cancel()
        if self._preview_job is not None:
            self._preview_job.cancel()
        self.preview_cache.close()
        return False


class PhoneFilesWindow(Gtk.Window):
    """Standalone wrapper retained for callers that want a focused browser window."""

    def __init__(self, parent, device) -> None:
        super().__init__(transient_for=parent, modal=True, title=f"Files on {device.display_name}")
        self.set_default_size(1000, 700)
        self.set_size_request(820, 560)
        header = Adw.HeaderBar()
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_end(close)
        self.set_titlebar(header)
        self.browser = PhoneFilesBrowser(device)
        self.set_child(self.browser)
        self.connect("close-request", self.browser.close_browser)

