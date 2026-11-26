# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Connected devices: live discovery, identity details and backup creation."""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ...asyncutil import run_in_thread
from ...config import free_space_text
from ...devices import libimobile
from ...devices.erase_engine import EraseEngine, EraseRejected, build_erase_plan
from ...devices.safety import destructive_preflight, refusal
from ...models import human_duration, human_size
from .. import dialogs
from ..widgets import (
    EmptyState,
    PageScaffold,
    Space,
    caption,
    icon_text_button,
    key_value_grid,
    panel,
)
from .base import Page


def format_duration(seconds: float) -> str:
    return human_duration(seconds)

#: Text the user must type before the device is asked to erase itself.
RESET_CONFIRMATION = "ERASE"

log = logging.getLogger(__name__)


class DevicesPage(Page):
    title = "Devices"
    icon_name = "phone-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        refresh = icon_text_button("Refresh", "view-refresh-symbolic")
        refresh.connect("clicked", lambda _b: self.state.refresh_devices(refresh_details=True))

        self.scaffold = PageScaffold(
            "Devices",
            "Devices visible to usbmuxd through libimobiledevice.",
            actions=[refresh],
            max_width=900,
        )
        self.append(self.scaffold)

        self.state.subscribe("devices", lambda *_: self.refresh())
        self.state.subscribe("settings", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._clear(self.scaffold.body)
        self.scaffold.add(self._tools_card())
        if self.state.device_error:
            self.scaffold.add(EmptyState(
                "dialog-warning-symbolic", "Device discovery unavailable",
                self.state.device_error))
            return
        if not self.state.devices:
            self.scaffold.add(EmptyState(
                "phone-symbolic", "No iOS device detected",
                "1. Connect the device with a data-capable cable (USB hubs and charge-only "
                "cables often fail).\n"
                "2. Unlock the device and tap “Trust This Computer”.\n"
                "3. Press Refresh.",
            ))
            return
        for device in self.state.devices:
            self.scaffold.add(self._device_card(device))

    def _tools_card(self) -> Gtk.Widget:
        status = self.state.tool_status
        missing = [name for name, ok in status.items() if not ok and name in libimobile.REQUIRED_TOOLS]
        pairs = []
        for name in ("idevice_id", "ideviceinfo", "idevicebackup2", "idevicepair"):
            pairs.append((name, "available" if status.get(name) else "missing"))
        pairs.append(("idevicebackup2 version", libimobile.idevicebackup2_version()))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        body.append(caption("Device communication runs through these system tools."))
        body.append(key_value_grid(pairs))
        if missing:
            warning = Gtk.Label(
                label=(f"Missing required tools: {', '.join(missing)}. "
                       "Install libimobiledevice-utils to enable device features."),
                xalign=0, wrap=True,
            )
            warning.add_css_class("error")
            body.append(warning)
        return panel(body)

    def _device_card(self, device) -> Gtk.Widget:
        mask = self.state.settings.mask_identifiers
        serial = device.serial_number or "—"
        imei = device.imei or "—"
        phone = device.phone_number or "—"
        if mask:
            serial = f"••••{serial[-4:]}" if len(serial) > 4 else serial
            imei = f"••••{imei[-4:]}" if len(imei) > 4 else imei
            phone = f"••••{phone[-4:]}" if len(phone) > 4 else phone
        pairs = [
            ("Model", device.model_name or device.product_type or "unknown"),
            ("Firmware", device.firmware or "unknown"),
            ("Serial", serial),
            ("Connection", device.connection_type or "unknown"),
            ("UDID", device.udid if not mask else f"…{device.udid[-8:]}"),
            ("Phone", phone),
            ("IMEI", imei),
        ]
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        body.append(key_value_grid(pairs))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        browse_button = icon_text_button("On My iPhone…", "folder-open-symbolic")
        browse_button.set_tooltip_text(
            "Open the live iPhone browser. Missing ifuse, FUSE or connection setup will be explained there.")
        browse_button.connect("clicked", lambda _b, dev=device: self._browse_phone_files(dev))
        actions.append(browse_button)
        backup_button = icon_text_button("Create backup…", "document-save-symbolic")
        backup_button.add_css_class("suggested-action")
        backup_button.set_sensitive(bool(self.state.tool_status.get("idevicebackup2")))
        backup_button.connect("clicked", lambda _b, dev=device: self._choose_backup_kind(dev))
        actions.append(backup_button)
        restore_button = icon_text_button("Restore backup…", "document-revert-symbolic")
        restore_button.set_sensitive(bool(self.state.backups))
        restore_button.set_tooltip_text(
            "Choose the backup to write onto this device on the Backups page")
        restore_button.connect("clicked", lambda _b: self._open_restore())
        actions.append(restore_button)
        reset_button = icon_text_button("Restore Device…", "edit-clear-all-symbolic")
        reset_button.add_css_class("destructive-action")
        reset_button.set_tooltip_text(
            "Erase all content and settings on this device, as “Erase All Content and "
            "Settings” in iOS does")
        reset_button.connect("clicked", lambda _b, dev=device: self._factory_reset(dev))
        actions.append(reset_button)
        if not self.state.tool_status.get("idevicebackup2"):
            actions.append(caption("idevicebackup2 is not installed — backup creation is disabled."))
        body.append(actions)
        body.append(caption(
            "Writing to the device only happens after an explicit confirmation: “Restore "
            "backup” replaces its data with a local backup, and “Restore Device” erases it "
            "back to factory settings."))
        return panel(body)

    # ------------------------------------------------------------------
    def _browse_phone_files(self, device) -> None:
        page = self.window.pages["phone"]
        page.select_device(device)
        self.window.show_page("phone")

    def _open_restore(self) -> None:
        self.window.show_page("backups")
        self.window.show_toast("Select the backup to restore, then choose “Restore to device…”")

    # ------------------------------------------------------------------ factory reset
    def _factory_reset(self, device) -> None:
        """Re-read pairing, Activation Lock and activation state, then ask for confirmation.

        Any of those can change between discovery and this click, so they are read from the
        device again instead of trusting the cached record. If the state cannot be read, the
        action is refused rather than attempted.
        """
        self.window.set_busy(f"Checking {device.display_name}…")

        def work(_job):
            return destructive_preflight(device.udid)

        def checked(state) -> None:
            self.window.hide_busy()
            reason = refusal(state, action="an erase")
            if reason is not None:
                dialogs.show_error(self.window, "Restore Device unavailable", reason)
                return
            try:
                plan = build_erase_plan(device.udid, device.display_name,
                                        device.product_type, device.product_version,
                                        activation_locked=state.activation_locked,
                                        activation_state=state.activation_state)
            except EraseRejected as exc:
                dialogs.show_error(self.window, "Restore Device unavailable", str(exc))
                return
            self._confirm_reset(plan)

        def failed(exc: Exception) -> None:
            self.window.hide_busy()
            dialogs.show_error(self.window, "Cannot read the device state",
                               f"{device.display_name} did not report its pairing or "
                               f"Activation Lock state, so nothing was sent to it.\n\n{exc}")

        run_in_thread(work, name="erase-check", on_success=checked, on_error=failed)

    def _confirm_reset(self, plan) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self.window, modal=True,
            heading=f"Restore {plan.device_name} to factory settings?",
            body=(f"Every photo, message, app and setting on {plan.device_name} is erased. "
                  "The device keeps its firmware, restarts at the “Hello” setup screen and "
                  "must then be set up again or restored from a backup.\n\n"
                  "This cannot be undone."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        entry = Gtk.Entry(placeholder_text=RESET_CONFIRMATION)
        box.append(caption(f"Type {RESET_CONFIRMATION} to confirm."))
        box.append(entry)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("erase", "Restore Device")
        dialog.set_response_appearance("erase", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_enabled("erase", False)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        entry.connect(
            "changed",
            lambda field: dialog.set_response_enabled(
                "erase",
                field.get_text().strip().casefold() == RESET_CONFIRMATION.casefold(),
            ),
        )

        def on_response(_dialog, response: str) -> None:
            if response == "erase":
                self._run_reset(plan)

        dialog.connect("response", on_response)
        self._reset_dialog = dialog
        dialog.present()

    def _run_reset(self, plan) -> None:
        progress = dialogs.ProgressDialog(
            self.window, "Restore Device", f"{plan.device_name} → factory settings")

        def work(job):
            return EraseEngine(plan, job=job, on_progress=progress.report_progress).run()

        def done(_result) -> None:
            self.state.refresh_devices(refresh_details=True)
            dialogs.show_info(
                self.window, "Restore Device started",
                f"{plan.device_name} is erasing all content and settings and will restart at "
                "the setup screen. Keep it connected until it restarts; it can then be set up "
                "as new or restored from a backup.",
            )

        def failed(exc: Exception) -> None:
            dialogs.show_error(self.window, "Restore Device failed", str(exc))

        progress.start(work, on_success=done, on_error=failed, name="device-erase")

    def _choose_backup_kind(self, device) -> None:
        """Offer the default destination, with an explicit way to change it."""
        settings = self.state.settings
        destination = [settings.default_destination]

        dialog = Adw.MessageDialog(
            transient_for=self.window, modal=True,
            heading=f"Back up “{device.display_name}”",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)

        address_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        address_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        address_text.set_hexpand(True)
        address_title = Gtk.Label(label="Destination", xalign=0)
        address_title.add_css_class("heading")
        path_label = Gtk.Label(label=self._short_path(destination[0]), xalign=0, wrap=True,
                               selectable=True, tooltip_text=str(destination[0]))
        path_label.add_css_class("monospace")
        path_label.add_css_class("caption")
        address_text.append(address_title)
        address_text.append(path_label)
        address_row.append(address_text)

        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        address_row.append(change)
        box.append(address_row)

        free_label = caption("")
        box.append(free_label)

        encryption_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        encryption_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        encryption_text.set_hexpand(True)
        encryption_title = Gtk.Label(label="Backup encryption", xalign=0)
        encryption_title.add_css_class("heading")
        self.encryption_label = Gtk.Label(label="Checking…", xalign=0, wrap=True)
        self.encryption_label.add_css_class("caption")
        self.encryption_label.add_css_class("dim-label")
        encryption_text.append(encryption_title)
        encryption_text.append(self.encryption_label)
        encryption_row.append(encryption_text)
        password_button = Gtk.Button(label="Backup password…", valign=Gtk.Align.CENTER)
        password_button.set_tooltip_text(
            "Set or change the password used to encrypt this iPhone's backups")
        password_button.connect("clicked", lambda _b: self._edit_backup_password(device))
        encryption_row.append(password_button)
        box.append(encryption_row)

        switch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        switch_row.append(Gtk.Label(label="Full backup", xalign=0, hexpand=True))
        switch = Gtk.Switch(active=False, valign=Gtk.Align.CENTER,
                            tooltip_text="Re-transfer every file instead of only changes")
        switch_row.append(switch)
        box.append(switch_row)
        box.append(caption(
            "Recommended: leave this off. Incremental backups transfer only what changed "
            "and resume an interrupted transfer."))

        warning = Gtk.Label(xalign=0, wrap=True)
        warning.add_css_class("caption")
        warning.add_css_class("dim-label")
        box.append(warning)

        self._backup_password = self.state_encryption_password(device)

        def refresh_encryption() -> None:
            state = libimobile.backup_encryption_state(device.udid)
            if state is True:
                self.encryption_label.set_text(
                    "On — the backup is encrypted and needs this password to be opened. "
                    "Store it safely before starting.")
            elif state is False:
                self.encryption_label.set_text(
                    "Off — the backup will not be encrypted. Set a password here if the "
                    "backup contains anything private.")
            else:
                self.encryption_label.set_text(
                    "Unknown — unlock the device to read its setting before starting.")

        def refresh_notes() -> None:
            path_label.set_text(self._short_path(destination[0]))
            path_label.set_tooltip_text(str(destination[0]))
            free = free_space_text(destination[0])
            free_label.set_text(free or "Destination not created yet")
            notes = ["Wake the iPhone and enter its passcode once when the backup asks for "
                     "it. After that the screen can lock again — the backup keeps running. "
                     "iOS refuses to start a backup while the device is locked (error 208)."]
            link = libimobile.usb_link_note(device.udid)
            if link:
                notes.append(link)
            if device.connection_type and "USB" not in (device.connection_type or ""):
                notes.append(f"Connection: {device.connection_type}.")
            warning.set_text("\n".join(notes))

        def on_change(_button) -> None:
            chooser = Gtk.FileDialog(title="Choose where backups are stored",
                                     initial_folder=Gio.File.new_for_path(str(destination[0])))
            chooser.select_folder(self.window, None, self._destination_chosen,
                                  (destination, refresh_notes))

        change.connect("clicked", on_change)
        refresh_notes()
        refresh_encryption()

        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("start", "Start backup")
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "start":
                return
            self.state.update_settings(backup_destination=str(destination[0]))
            self._start_backup(device, Path(destination[0]), switch.get_active(),
                               password=self._backup_password)

        dialog.connect("response", on_response)
        self._setup_dialog = dialog
        self._setup_notes = warning
        dialog.present()

    def _destination_chosen(self, dialog: Gtk.FileDialog, result, payload) -> None:
        destination, refresh_notes = payload
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None or folder.get_path() is None:
            return
        destination[0] = Path(folder.get_path())
        refresh_notes()

    def state_encryption_password(self, device) -> str | None:
        """Password already known for this device, if the user stored one."""
        cached = getattr(self, "_backup_password", None)
        if cached:
            return cached
        return self.state.password_for_udid(device.udid)

    def _edit_backup_password(self, device) -> None:
        """Set, change or remove the device's backup password before backing up."""
        state = libimobile.backup_encryption_state(device.udid)
        dialog = Adw.MessageDialog(
            transient_for=self.window, modal=True,
            heading="Backup password",
            body=("The iPhone must be unlocked. The password encrypts every future backup; "
                  "without it an encrypted backup cannot be opened."
                  if state is not False else
                  "Encryption is currently off. Setting a password turns it on; the iPhone "
                  "must be unlocked."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        first = Gtk.PasswordEntry(show_peek_icon=True)
        first.set_property("placeholder-text", "New backup password")
        second = Gtk.PasswordEntry(show_peek_icon=True)
        second.set_property("placeholder-text", "Repeat the password")
        box.append(first)
        box.append(second)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        if state is not False:
            dialog.add_response("disable", "Turn encryption off")
            dialog.set_response_appearance("disable", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "cancel":
                return
            password = None if response == "disable" else first.get_text()
            if response == "apply":
                if len(password or "") < 4:
                    dialogs.show_error(self.window, "Password too short",
                                       "Choose a backup password of at least four characters.")
                    return
                if password != second.get_text():
                    dialogs.show_error(self.window, "Passwords do not match",
                                       "Enter the same password twice.")
                    return
            progress = dialogs.ProgressDialog(self.window, "Backup password",
                                              "Talking to the device…")

            def work(_job):
                return libimobile.set_backup_encryption(device.udid,
                                                        response != "disable", password)

            def done(result) -> None:
                ok, message = result
                if ok:
                    self._backup_password = password
                    self.window.show_toast(message)
                    if password and self.state.settings.store_passwords:
                        self.state.remember_password_for_udid(device.udid, password)
                else:
                    dialogs.show_error(self.window, "The device refused the change", message)
                self.refresh()

            progress.start(work, on_success=done, name="backup-password")

        dialog.connect("response", on_response)
        dialog.present()

    def _start_backup(self, device, destination: Path, full: bool,
                      password: str | None = None) -> None:
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            dialogs.show_error(self.window, "Cannot create the backup folder",
                               f"{destination}\n\n{exc}")
            return
        progress = dialogs.ProgressDialog(
            self.window, "Creating backup",
            f"{device.display_name} → {destination}",
        )

        def work(job):
            return libimobile.create_backup(device.udid, destination, job, full=full,
                                            password=password,
                                            on_progress=progress.report_progress)

        def done(result) -> None:
            self.state.settings.add_backup_directory(str(destination))
            self.state.update_settings()
            self.state.add_backup_size_hint(result)
            self.state.scan_backups()
            kind = "Full" if result.full else "Incremental"
            rate = ""
            if result.duration_seconds > 0:
                megabytes = result.bytes_on_disk / (1024 * 1024)
                rate = f" · {megabytes / result.duration_seconds:.1f} MB/s average"
            dialogs.show_info(
                self.window, "Backup finished",
                f"{device.display_name} was backed up to:\n{destination}\n\n"
                f"{kind} backup · {result.file_count:,} files · "
                f"{human_size(result.bytes_on_disk)} · "
                f"{format_duration(result.duration_seconds)}{rate}",
            )

        def failed(exc: Exception) -> None:
            hint = getattr(exc, "hint", None)
            if hint:
                self._offer_retry(device, destination, full, str(exc), hint)
            else:
                dialogs.show_error(self.window, "Backup failed", str(exc))

        progress.start(work, on_success=done, on_error=failed, name="device-backup")

    def _offer_retry(self, device, destination: Path, full: bool, detail: str,
                     hint: str) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self.window, modal=True, heading="The device needs attention",
            body=hint,
        )
        dialog.set_extra_child(caption(detail))
        dialog.add_response("close", "Close")
        dialog.add_response("retry", "Retry backup")
        dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("retry")
        dialog.set_close_response("close")

        def on_response(_dialog, response: str) -> None:
            if response == "retry":
                self._start_backup(device, destination, full)

        dialog.connect("response", on_response)
        dialog.present()

    @staticmethod
    def _short_path(path: Path) -> str:
        """Show the path relative to $HOME so the dialog does not wrap unnecessarily."""
        try:
            return f"~/{path.expanduser().relative_to(Path.home())}"
        except ValueError:
            return str(path)

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
