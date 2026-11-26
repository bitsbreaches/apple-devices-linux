# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Backups page: discovery, metadata, password unlock, verification and reports."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ...asyncutil import run_in_thread
from ...backup import BadPasswordError, write_inventory_report
from ...devices.safety import destructive_preflight, refusal
from ...devices.restore_engine import (
    RestoreEngine,
    RestoreRejected,
    build_restore_plan,
)
from ...models import human_duration, human_size
from .. import dialogs
from ..widgets import (
    DataTable,
    EmptyState,
    PageScaffold,
    RowItem,
    Space,
    caption,
    icon_text_button,
    metadata_list,
    scrolled,
    section_heading,
    status_strip,
)
from .base import Page

log = logging.getLogger(__name__)


class BackupsPage(Page):
    title = "Backups"
    icon_name = "drive-harddisk-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self._selected = None
        self._password_dialog = None

        add_button = icon_text_button("Add folder…", "folder-new-symbolic")
        add_button.connect("clicked", lambda _b: self._add_folder())
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan backups")
        rescan.connect("clicked", lambda _b: self.state.scan_backups())

        self.scaffold = PageScaffold(
            "Backups",
            "Backups found in the folders this application scans. Nothing here is modified.",
            actions=[add_button, rescan],
            scroll=False,
            max_width=1400,
        )
        self.append(self.scaffold)

        self.summary_label = caption("")
        self.summary = status_strip(self.summary_label)
        self.table = DataTable(
            ["Device", "Firmware", "Encryption", "Last backup", "Size"],
            on_selection=self._on_selected,
        )

        list_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        list_pane.append(self.summary)
        list_pane.append(self.table)
        list_pane.set_hexpand(True)

        self.detail_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XL)
        self.detail_body.set_margin_top(Space.L)
        self.detail_body.set_margin_bottom(Space.XL)
        self.detail_body.set_margin_start(Space.S)
        self.detail_body.set_margin_end(Space.S)
        self.detail = scrolled(self.detail_body)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        paned.set_shrink_start_child(True)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_start_child(list_pane)
        paned.set_end_child(self.detail)
        paned.set_vexpand(True)
        paned.set_hexpand(True)
        self.paned = paned
        self.scaffold.add(paned)
        # Keep a readable detail pane at any window width instead of clipping it.

        self.state.subscribe("backups", lambda *_: self.refresh())
        self.state.subscribe("vault", lambda *_: self._render_detail())
        self.state.subscribe("settings", lambda *_: self.refresh())
        self.state.subscribe("refresh", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    # def _rebalance(self, *_args) -> None:
    #     width = self.get_width()
    #     if width > 0:
    #         self.paned.set_position(max(340, min(760, int(width * 0.52))))

    def refresh(self) -> None:
        self._update_summary()
        rows = []
        for info in self.state.backups:
            size = human_size(info.size_bytes) if info.size_bytes else "—"
            rows.append(RowItem([
                info.display_name,
                info.firmware or "—",
                "Encrypted" if info.is_encrypted else "None",
                info.last_backup_text,
                size,
            ], info))
        self.table.set_rows(rows)
        index = 0
        if self._selected is not None:
            for position, row in enumerate(rows):
                if row.payload is self._selected:
                    index = position
                    break
            else:
                self._selected = None
        if rows:
            self.table.select_row(index)
        self._render_detail()

    def _on_selected(self, item: RowItem | None) -> None:
        self._selected = item.payload if item is not None else None
        self._render_detail()

    def _update_summary(self) -> None:
        backups = self.state.backups
        total = sum(info.size_bytes or 0 for info in backups)
        encrypted = sum(1 for info in backups if info.is_encrypted)
        parts = [f"{len(backups)} backup(s)"]
        if total:
            parts.append(f"{human_size(total)} in total")
        if encrypted:
            parts.append(f"{encrypted} encrypted")
        parts.append(f"{len(self.state.settings.backup_directories)} folder(s) scanned")
        self.summary_label.set_text("   ·   ".join(parts))

    def _render_detail(self) -> None:
        self._clear(self.detail_body)
        info = self._selected
        if info is None:
            self.detail_body.append(EmptyState(
                "drive-harddisk-symbolic", "No backup selected",
                "Choose a backup in the list to see its details, unlock it, or export a report.",
            ))
            return

        unlocked = (self.state.vault is not None and self.state.active_backup is info
                    and self.state.vault.unlocked)
        if info.is_encrypted:
            status = "Unlocked" if unlocked else "Encrypted — locked"
        else:
            status = "Readable"

        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        name = Gtk.Label(label=info.display_name, xalign=0, wrap=True)
        name.add_css_class("title-2")
        heading.append(name)
        subtitle_bits = [info.model_name or info.product_type or "unknown model"]
        if info.firmware:
            subtitle_bits.append(info.firmware)
        subtitle = Gtk.Label(label=" · ".join(subtitle_bits), xalign=0, wrap=True)
        subtitle.add_css_class("dim-label")
        heading.append(subtitle)
        if info.last_backup_text:
            heading.append(caption(f"Last backup {info.last_backup_text}"))
        self.detail_body.append(heading)

        pairs = [
            ("Status", status),
            ("Encryption", "Enabled" if info.is_encrypted else "Off"),
            ("Size on disk", human_size(info.size_bytes) if info.size_bytes else "not measured"),
            ("Files", f"{info.file_count:,}" if info.file_count else "not measured"),
            ("Full backup", "Yes" if info.is_full_backup else "No"),
            ("Passcode", "Set when backed up" if info.was_passcode_set else "Not set"),
            ("Applications", str(info.applications) if info.applications else "—"),
            ("Manifest version", info.manifest_version or "—"),
        ]
        self.detail_body.append(self._actions(info, unlocked))
        self.detail_body.append(metadata_list(pairs, title="Backup"))

        location = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        location.append(section_heading("Location"))
        path_label = Gtk.Label(label=str(info.path), xalign=0, selectable=True, wrap=True)
        path_label.add_css_class("monospace")
        path_label.add_css_class("caption")
        path_label.add_css_class("dim-label")
        location.append(path_label)
        self.detail_body.append(location)

        if info.error:
            self.detail_body.append(caption(f"This backup could not be read: {info.error}"))
        elif info.is_encrypted and not unlocked:
            self.detail_body.append(caption(
                "This backup is encrypted. Unlocking unwraps the keybag in memory only; the "
                "backup on disk is never modified."))

    def _actions(self, info, unlocked: bool) -> Gtk.Widget:
        """Primary and secondary actions, in rows of at most two.

        A plain box is used rather than ``Gtk.FlowBox``: a flow box's children are wrapped in
        ``Gtk.FlowBoxChild`` containers that took the pointer press for themselves, so the
        buttons inside were unclickable.
        """
        buttons: list[tuple[Gtk.Widget, bool]] = []

        if info.is_encrypted and not unlocked:
            unlock = icon_text_button("Unlock…", "changes-prevent-symbolic")
            unlock.connect("clicked", lambda _b: self._begin_unlock(info))
            buttons.append((unlock, True))
            if self.state.password_for(info):
                forget = Gtk.Button(label="Forget saved password")
                forget.connect("clicked", lambda _b: self._forget(info))
                buttons.append((forget, False))
        elif unlocked:
            explore = icon_text_button("Explore backups", "folder-open-symbolic")
            explore.connect("clicked", lambda _b: self.window.show_page("explorer"))
            buttons.append((explore, True))
            verify = icon_text_button("Verify…", "emblem-ok-symbolic")
            verify.connect("clicked", lambda _b: self._verify())
            buttons.append((verify, False))
            report = icon_text_button("Export report…", "document-save-symbolic")
            report.connect("clicked", lambda _b: self._export_report())
            buttons.append((report, False))
            lock = icon_text_button("Lock", "changes-prevent-symbolic")
            lock.connect("clicked", lambda _b: (self.state.close_vault(), self._render_detail()))
            buttons.append((lock, False))
        else:
            open_button = icon_text_button("Open backup", "folder-open-symbolic")
            open_button.connect("clicked", lambda _b: self._unlock(info, None))
            buttons.append((open_button, True))

        if not info.size_bytes:
            measure = Gtk.Button(label="Measure size")
            measure.connect("clicked", lambda _b: self._measure(info))
            buttons.append((measure, False))

        device = self._restore_target(info)
        restore = icon_text_button("Restore to device…", "document-revert-symbolic")
        restore.connect("clicked", lambda _b: self._restore(info))
        if device is None:
            restore.set_sensitive(False)
            restore.set_tooltip_text(
                "Connect the iPhone this backup belongs to; restoring writes to the device.")
        else:
            restore.set_tooltip_text(f"Write this backup onto {device.display_name}")
        buttons.append((restore, False))

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        for index in range(0, len(buttons), 2):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            row.set_homogeneous(True)
            for widget, suggested in buttons[index:index + 2]:
                widget.set_hexpand(True)
                if suggested:
                    widget.add_css_class("suggested-action")
                row.append(widget)
            if len(buttons[index:index + 2]) == 1:
                spacer = Gtk.Box()
                spacer.set_hexpand(True)
                row.append(spacer)
            container.append(row)
        return container

    # ------------------------------------------------------------------
    def _begin_unlock(self, info, error: str | None = None) -> None:
        cached = self.state.password_for(info)
        if cached and error is None:
            self._unlock(info, cached)
            return
        self._close_password_dialog()
        self._password_dialog = dialogs.ask_password(
            self.window,
            f"Unlock “{info.display_name}”",
            "Enter the backup password chosen in iTunes/Finder. It is used only to unwrap the "
            "backup keys in memory.",
            lambda password: self._unlock(info, password),
            error=error,
        )

    def _close_password_dialog(self) -> None:
        dialog = getattr(self, "_password_dialog", None)
        if dialog is not None:
            dialog.close()
            self._password_dialog = None

    def _unlock(self, info, password: str | None) -> None:
        # Unlock is dispatched by the state so every page shares one vault; key derivation
        # (PBKDF2 over the backup keybag) runs on a worker thread.
        self.window.show_busy(f"Unlocking {info.display_name}…")

        def failed(exc: Exception) -> None:
            self.window.hide_busy()
            if isinstance(exc, BadPasswordError):
                self.state.forget_password(info)
                # One dialog, reopened with the reason: a rejected attempt must not leave a
                # separate "incorrect password" window behind.
                self._begin_unlock(
                    info,
                    error=("That password did not unlock this backup. "
                           "Enter the backup password again."),
                )
            else:
                dialogs.show_error(self.window, "Cannot open backup", str(exc))

        def success(_vault) -> None:
            self.window.hide_busy()
            self._close_password_dialog()
            self._render_detail()

        self.state.unlock(info, password, on_success=success, on_error=failed)

    def _forget(self, info) -> None:
        self.state.forget_password(info)
        dialogs.show_info(self.window, "Password forgotten",
                          "The stored password for this backup was removed from the keyring.")
        self._render_detail()

    def _restore_target(self, info):
        """The connected device this backup can be restored to, if any."""
        for device in self.state.devices:
            if device.udid.casefold() == (info.udid or "").casefold():
                return device
        return None

    def _restore(self, info) -> None:
        device = self._restore_target(info)
        if device is None:
            dialogs.show_error(
                self.window, "No matching device",
                f"This backup belongs to {info.display_name}, which is not connected. "
                "Connect the iPhone, unlock it and try again.")
            return
        # Pairing, Activation Lock and activation state are re-read from the device: a
        # cached record can be minutes old and iOS refuses the restore anyway.
        self.window.set_busy(f"Checking {device.display_name}…")

        def work(_job):
            return destructive_preflight(device.udid)

        def checked(state) -> None:
            self.window.hide_busy()
            reason = refusal(state, action="a backup restore")
            if reason is not None:
                dialogs.show_error(self.window, "This backup cannot be restored", reason)
                return
            try:
                plan = build_restore_plan(info.path, device.udid, device.product_type,
                                          device.product_version, state.activation_locked,
                                          state.activation_state)
            except RestoreRejected as exc:
                dialogs.show_error(self.window, "This backup cannot be restored", str(exc))
                return
            self._confirm_restore(info, device, plan)

        def failed(exc: Exception) -> None:
            self.window.hide_busy()
            dialogs.show_error(
                self.window, "Cannot read the device state",
                f"{device.display_name} did not report its pairing or Activation Lock state, "
                f"so nothing was written to it.\n\n{exc}")

        run_in_thread(work, name="restore-check", on_success=checked, on_error=failed)

    def _confirm_restore(self, info, device, plan) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self.window, modal=True,
            heading=f"Restore to {device.display_name}?",
            body=(f"Every file, setting and app data item on the device is replaced by the "
                  f"contents of this backup:\n{info.path}\n\n"
                  f"Backup from {plan.product_type or 'unknown model'} · "
                  f"iOS {plan.product_version or 'unknown'}"
                  + (" · encrypted" if plan.encrypted else "")
                  + "\n\nThe device may restart when the restore finishes. "
                    "This cannot be undone."),
        )
        confirm = Gtk.CheckButton(
            label=f"I understand that the data on {device.display_name} will be replaced")
        confirm.set_margin_top(Space.M)
        dialog.set_extra_child(confirm)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restore", "Restore")
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_enabled("restore", False)
        dialog.set_close_response("cancel")
        confirm.connect("toggled",
                        lambda box: dialog.set_response_enabled("restore", box.get_active()))

        def on_response(_dialog, response: str) -> None:
            if response != "restore":
                return
            password = self.state.password_for(info) if plan.encrypted else None
            if plan.encrypted and not password:
                dialogs.ask_password(
                    self.window, "Backup password",
                    "The backup is encrypted. Enter the password chosen when it was made.",
                    lambda value: self._run_restore(device, plan, value))
            else:
                self._run_restore(device, plan, password)

        dialog.connect("response", on_response)
        dialog.present()

    def _run_restore(self, device, plan, password: str | None) -> None:
        progress = dialogs.ProgressDialog(
            self.window, "Restoring backup", f"{plan.device_name} → {device.display_name}")

        def work(job):
            engine = RestoreEngine(device.udid, plan, password=password, job=job,
                                   on_progress=progress.report_progress)
            result = engine.run()
            if result.status == "cancelled":
                from ...asyncutil import OperationCancelled

                raise OperationCancelled("restore")
            return result

        def done(result) -> None:
            self.state.scan_backups()
            dialogs.show_info(
                self.window, "Restore finished",
                f"{device.display_name} was restored from:\n{plan.backup}\n\n"
                f"Duration {human_duration(result.seconds)}. The device may restart; "
                "leave it connected until it finishes.",
            )

        def failed(exc: Exception) -> None:
            hint = getattr(exc, "hint", None)
            if hint:
                dialogs.show_error(self.window, "Restore failed", f"{exc}\n\n{hint}")
            else:
                dialogs.show_error(self.window, "Restore failed", str(exc))

        progress.start(work, on_success=done, on_error=failed, name="device-restore")

    def _measure(self, info) -> None:
        progress = dialogs.ProgressDialog(self.window, "Measuring backup",
                                          "Counting files and adding up sizes")
        progress.start(lambda job: self.state.fill_backup_size(info, on_done=self.refresh),
                       name="measure-backup")

    def _verify(self) -> None:
        vault = self.state.vault
        if vault is None:
            return
        progress = dialogs.ProgressDialog(self.window, "Verifying backup",
                                          "Checking manifest entries and payload digests")

        def work(job):
            return vault.verify(job)

        def done(result) -> None:
            dialogs.show_info(
                self.window, "Verification finished",
                f"Manifest entries: {result['file_count']}\n"
                f"Payload files sampled: {result['sampled']}\n"
                f"Missing payloads: {result['missing']}\n"
                f"Digests checked: {result['digest_checked']} "
                f"(mismatches: {result['digest_mismatch']})\n"
                + ("\n".join(result["errors"][:5]) if result["errors"] else ""),
            )

        progress.start(work, on_success=done, name="verify-backup")

    def _export_report(self) -> None:
        info = self._selected
        vault = self.state.vault
        if info is None or vault is None:
            return
        default_name = f"backup-report-{info.udid[-8:] or 'device'}.html"
        dialog = Gtk.FileDialog(title="Save backup report", initial_name=default_name)
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filter_html = Gtk.FileFilter()
        filter_html.set_name("HTML report")
        filter_html.add_suffix("html")
        filters.append(filter_html)
        dialog.set_filters(filters)
        dialog.save(self.window, None, self._report_chosen, info, vault)

    def _report_chosen(self, dialog: Gtk.FileDialog, result, info, vault) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        if file is None or file.get_path() is None:
            return
        target = Path(file.get_path())
        try:
            write_inventory_report(info, vault.manifest(), target)
        except Exception as exc:  # noqa: BLE001
            dialogs.show_error(self.window, "Report failed", str(exc))
            return
        dialogs.show_info(self.window, "Report saved", str(target))

    def _add_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Add a backup folder")
        dialog.select_folder(self.window, None, self._folder_chosen)

    def _folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        path = folder.get_path()
        self.state.settings.add_backup_directory(path)
        self.state.settings.save()
        self.state.scan_backups()

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
