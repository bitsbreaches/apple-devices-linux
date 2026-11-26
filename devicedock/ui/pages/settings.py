# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Settings page: backup locations, export folder, appearance, security and logging."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ... import secrets
from ...logging_setup import LOG_PATH
from .. import dialogs
from ..widgets import PageScaffold
from .base import Page

log = logging.getLogger(__name__)

THEMES = ["system", "light", "dark"]
THEME_LABELS = ["Follow system", "Light", "Dark"]
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class SettingsPage(Page):
    title = "Settings"
    icon_name = "emblem-system-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self._updating = False
        self._groups: list[Adw.PreferencesGroup] = []
        self.page = Adw.PreferencesPage()
        self.scaffold = PageScaffold(
            "Settings",
            "Backup locations, export defaults, appearance, security and diagnostics.",
            scroll=False,
            max_width=1000,
        )
        self.append(self.scaffold)
        self.scaffold.add(self.page)
        self.state.subscribe("settings", lambda *_: self.refresh())
        # "Refresh everything" and folder creation must re-evaluate exists/missing rows
        # even though no setting changed.
        self.state.subscribe("refresh", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._updating = True
        for group in self._groups:
            self.page.remove(group)
        self._groups.clear()
        self._updating = False

        for factory in (
            self._locations_group,
            self._export_group,
            self._appearance_group,
            self._security_group,
            self._logging_group,
            self._licence_group,
            self._about_group,
        ):
            group = factory()
            self._groups.append(group)
            self.page.add(group)

    def _locations_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Backup locations",
            description=("Folders scanned for backups. “Create backup” writes to the folder "
                         "marked Default; nothing else here is ever written to."),
        )
        destination = self.state.settings.default_destination
        for directory in self.state.settings.backup_directories:
            path = Path(directory).expanduser()
            exists = path.is_dir()
            is_default = path == destination
            row = Adw.ActionRow(title=str(path), subtitle="exists" if exists else "missing")
            if is_default:
                row.set_subtitle(("default · exists" if exists else "default · missing"))
                badge = Gtk.Label(label="Default")
                badge.add_css_class("caption")
                badge.add_css_class("dim-label")
                badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(badge)
            if not exists:
                create = Gtk.Button(label="Create", valign=Gtk.Align.CENTER)
                create.add_css_class("flat")
                create.set_tooltip_text(f"Create {path}")
                create.connect("clicked", lambda _b, target=path: self._create_directory(target))
                row.add_suffix(create)
            else:
                make_default = Gtk.Button(icon_name="emblem-default-symbolic",
                                          valign=Gtk.Align.CENTER)
                make_default.add_css_class("flat")
                make_default.set_tooltip_text("Use this folder for new backups")
                make_default.set_sensitive(not is_default)
                make_default.connect("clicked",
                                     lambda _b, target=path: self._make_default(target))
                row.add_suffix(make_default)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove from the scan list (the folder is kept)")
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, target=directory: self._remove_directory(target))
            row.add_suffix(remove)
            group.add(row)
        add_row = Adw.ActionRow(title="Add a folder…")
        add_button = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_button.add_css_class("flat")
        add_button.connect("clicked", lambda _b: self._add_directory())
        add_row.add_suffix(add_button)
        add_row.set_activatable_widget(add_button)
        group.add(add_row)
        return group

    def _create_directory(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            dialogs.show_error(self.window, "Cannot create the folder",
                               f"{path}\n\n{exc}")
            return
        self.state.scan_backups()
        self.refresh()
        self.window.show_toast(f"Created {path}")

    def _make_default(self, path: Path) -> None:
        self.state.update_settings(backup_destination=str(path))
        self.window.show_toast(f"New backups will be written to {path}")

    def _export_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Export")
        row = Adw.ActionRow(title="Default export folder",
                            subtitle=self.state.settings.export_directory)
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda _b: self._choose_export())
        row.add_suffix(choose)
        group.add(row)
        return group

    def _appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Appearance")
        combo = Adw.ComboRow(title="Theme", model=Gtk.StringList.new(THEME_LABELS))
        theme = self.state.settings.theme if self.state.settings.theme in THEMES else "system"
        combo.set_selected(THEMES.index(theme))

        def on_theme(row, _pspec):
            if self._updating:
                return
            self.state.update_settings(theme=THEMES[row.get_selected()])
            self.window.apply_theme()

        combo.connect("notify::selected", on_theme)
        group.add(combo)
        return group

    def _security_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Security",
            description="Passwords are never written to settings or logs.",
        )
        store = Adw.SwitchRow(
            title="Remember backup passwords",
            subtitle=("Store passwords in the system keyring (Secret Service). "
                      if secrets.available() else
                      "Secret Service is unavailable on this system — passwords stay in memory only."),
        )
        store.set_active(self.state.settings.store_passwords and secrets.available())
        store.set_sensitive(secrets.available())

        def on_store(row, _pspec):
            if self._updating:
                return
            self.state.update_settings(store_passwords=row.get_active())
            if not row.get_active():
                for info in self.state.backups:
                    secrets.clear_password(info.udid)

        store.connect("notify::active", on_store)
        group.add(store)

        mask = Adw.SwitchRow(title="Mask device identifiers",
                             subtitle="Hide serial numbers, IMEI and phone numbers in the interface.")
        mask.set_active(self.state.settings.mask_identifiers)

        def on_mask(row, _pspec):
            if self._updating:
                return
            self.state.update_settings(mask_identifiers=row.get_active())

        mask.connect("notify::active", on_mask)
        group.add(mask)

        forget = Adw.ActionRow(title="Forget all saved passwords",
                               subtitle="Remove every stored backup password from the keyring.")
        button = Gtk.Button(label="Forget", valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.set_sensitive(secrets.available())
        button.connect("clicked", lambda _b: self._forget_all())
        forget.add_suffix(button)
        group.add(forget)
        return group

    def _logging_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Diagnostics")
        combo = Adw.ComboRow(title="Log level", model=Gtk.StringList.new(LOG_LEVELS))
        level = self.state.settings.log_level.upper()
        combo.set_selected(LOG_LEVELS.index(level) if level in LOG_LEVELS else 1)

        def on_level(row, _pspec):
            if self._updating:
                return
            value = LOG_LEVELS[row.get_selected()]
            self.state.update_settings(log_level=value)
            logging.getLogger().setLevel(getattr(logging, value, logging.INFO))

        combo.connect("notify::selected", on_level)
        group.add(combo)

        log_row = Adw.ActionRow(title="Log file", subtitle=str(LOG_PATH))
        open_button = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Open the state folder")
        open_button.add_css_class("flat")
        open_button.connect("clicked", lambda _b: self._open_folder(LOG_PATH.parent))
        log_row.add_suffix(open_button)
        group.add(log_row)
        return group

    def _licence_group(self) -> Adw.PreferencesGroup:
        from ... import COPYRIGHT_NOTICE, LICENSE_NAME, LICENSE_URL, __version__
        from ... import licensing

        group = Adw.PreferencesGroup(
            title="Licence",
            description=("Free software under the GNU General Public License v3.0 only. "
                         "There is NO WARRANTY, to the extent permitted by law."),
        )
        group.add(Adw.ActionRow(title="Copyright", subtitle=COPYRIGHT_NOTICE))
        group.add(Adw.ActionRow(title="Licence type", subtitle=LICENSE_NAME))
        group.add(Adw.ActionRow(title="Licence text", subtitle=LICENSE_URL))
        group.add(Adw.ActionRow(title="Version", subtitle=__version__))

        licence_row = Adw.ActionRow(
            title="View GNU General Public License",
            subtitle="LICENSE · GPL-3.0-only",
        )
        licence_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        licence_button.connect("clicked", lambda _b: self._show_document(
            "GNU General Public License v3.0", licensing.license_text))
        licence_row.add_suffix(licence_button)
        licence_row.set_activatable_widget(licence_button)
        group.add(licence_row)

        notices_row = Adw.ActionRow(
            title="Third-party notices",
            subtitle="THIRD_PARTY_NOTICES.md · libraries, licences and attribution",
        )
        notices_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        notices_button.connect("clicked", lambda _b: self._show_document(
            "Third-party notices", licensing.notices_text))
        notices_row.add_suffix(notices_button)
        notices_row.set_activatable_widget(notices_button)
        group.add(notices_row)
        return group

    def _show_document(self, heading: str, loader) -> None:
        try:
            text = loader()
        except Exception as exc:  # noqa: BLE001 - missing distribution file is reported
            dialogs.show_error(self.window, "Document unavailable", str(exc))
            return
        dialogs.show_document(self.window, heading, text)

    def _about_group(self) -> Adw.PreferencesGroup:
        from ... import APP_SUMMARY, COPYRIGHT_NOTICE, __version__

        group = Adw.PreferencesGroup(title="About")
        group.add(Adw.ActionRow(
            title="Device Dock",
            subtitle=(f"Version {__version__} · {APP_SUMMARY} · native GTK 4 / libadwaita · "
                      "device access through libimobiledevice"),
        ))
        group.add(Adw.ActionRow(title="Copyright", subtitle=COPYRIGHT_NOTICE))
        group.add(Adw.ActionRow(
            title="Scope",
            subtitle=("Backups are only read. A connected device is written to — transfer, "
                      "restore or factory reset — only after an explicit confirmation."),
        ))
        return group

    # ------------------------------------------------------------------
    def _add_directory(self) -> None:
        dialog = Gtk.FileDialog(title="Add a backup folder")
        dialog.select_folder(self.window, None, self._directory_chosen)

    def _directory_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        self.state.settings.add_backup_directory(folder.get_path())
        self.state.update_settings()  # persists and notifies
        self.state.scan_backups()

    def _remove_directory(self, directory: str) -> None:
        self.state.settings.remove_backup_directory(directory)
        self.state.update_settings()
        self.state.scan_backups()

    def _choose_export(self) -> None:
        dialog = Gtk.FileDialog(title="Choose default export folder")
        dialog.select_folder(self.window, None, self._export_chosen)

    def _export_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        self.state.update_settings(export_directory=folder.get_path())

    def _forget_all(self) -> None:
        def do_forget() -> None:
            count = 0
            for info in self.state.backups:
                if secrets.clear_password(info.udid):
                    count += 1
                self.state.forget_password(info)
            dialogs.show_info(self.window, "Passwords removed",
                              f"{count} stored password(s) deleted from the keyring.")

        dialogs.confirm(self.window, "Forget all saved passwords?",
                        "Every stored backup password will be deleted from the system keyring.",
                        "Forget all", do_forget, destructive=True)

    def _open_folder(self, path: Path) -> None:
        try:
            Gtk.show_uri(self.window, path.as_uri(), 0)  # 0 == GDK_CURRENT_TIME
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot open %s: %s", path, exc)
