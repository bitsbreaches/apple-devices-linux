# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Overview page: device status, backup summary and shortcuts."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from ...models import human_size
from ..widgets import (
    EmptyState,
    PageScaffold,
    Space,
    caption,
    icon_text_button,
    key_value_grid,
    panel,
    section_heading,
)
from .base import Page


class OverviewPage(Page):
    title = "Overview"
    icon_name = "go-home-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        scan = icon_text_button("Scan backups", "view-refresh-symbolic")
        scan.connect("clicked", lambda _b: self.state.scan_backups())
        explore = Gtk.Button(label="Explore backups")
        explore.add_css_class("suggested-action")
        explore.connect("clicked", lambda _b: self.window.show_page("explorer"))

        self.scaffold = PageScaffold(
            "Device Dock",
            "Inspect connected iOS devices and iTunes/Finder backups, entirely on this machine.",
            actions=[scan, explore],
        )
        self.append(self.scaffold)

        self.state.subscribe("devices", lambda *_: self.refresh())
        self.state.subscribe("backups", lambda *_: self.refresh())
        self.state.subscribe("vault", lambda *_: self.refresh())
        self.state.subscribe("settings", lambda *_: self.refresh())
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        self._clear(self.scaffold.body)
        self._render_device()
        self._render_backups()

    def _render_device(self) -> None:
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        block.append(section_heading("Connected device"))

        if self.state.device_error:
            block.append(panel(_message_row("dialog-warning-symbolic", self.state.device_error,
                                           "Devices cannot be listed until this is resolved.")))
        elif not self.state.devices:
            block.append(EmptyState(
                "phone-symbolic",
                "No device connected",
                "Connect an iPhone or iPad with a data cable, unlock it, and tap “Trust”. "
                "Backup inspection below works without a device.",
            ))
        else:
            for device in self.state.devices:
                mask = self.state.settings.mask_identifiers
                serial = device.serial_number or "—"
                if mask and len(serial) > 4:
                    serial = f"••••{serial[-4:]}"
                pairs = [
                    ("Name", device.display_name),
                    ("Model", device.model_name or device.product_type or "unknown"),
                    ("Firmware", device.firmware or "unknown"),
                    ("Serial", serial),
                    ("Connection", device.connection_type or "not reported by this tool version"),
                ]
                body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
                body.append(key_value_grid(pairs))
                actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
                backup_button = icon_text_button("Create backup…", "document-save-symbolic")
                backup_button.add_css_class("suggested-action")
                backup_button.set_sensitive(bool(self.state.tool_status.get("idevicebackup2")))
                backup_button.connect(
                    "clicked",
                    lambda _b, dev=device: self.window.pages["devices"]._choose_backup_kind(dev),
                )
                details_button = Gtk.Button(label="Device details")
                details_button.connect("clicked", lambda _b: self.window.show_page("devices"))
                actions.append(backup_button)
                actions.append(details_button)
                body.append(actions)
                block.append(panel(body))
        self.scaffold.add(block)

    def _render_backups(self) -> None:
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        block.append(section_heading("Backups"))

        backups = self.state.backups
        if not backups:
            block.append(EmptyState(
                "drive-harddisk-symbolic",
                "No backups found",
                "Use “Scan backups” after adding a folder in Settings, or create a backup "
                "from a connected device.",
            ))
            self.scaffold.add(block)
            return

        latest = max(backups, key=lambda b: b.last_backup.timestamp() if b.last_backup else 0)
        encrypted = sum(1 for b in backups if b.is_encrypted)
        pairs = [
            ("Backups found", str(len(backups))),
            ("Most recent", latest.display_name),
            ("Backup date", latest.last_backup_text),
            ("Encryption", f"{encrypted} encrypted · {len(backups) - encrypted} plain"),
        ]
        if latest.size_bytes:
            pairs.append(("Latest size", human_size(latest.size_bytes)))
        if self.state.active_backup is not None:
            pairs.append(("Open backup", self.state.active_backup.display_name))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        body.append(key_value_grid(pairs))
        body.append(caption(
            "Backups are only read here. Writing to a device happens on the Devices page, "
            "after an explicit confirmation."))
        list_button = Gtk.Button(label="Manage backups")
        list_button.set_halign(Gtk.Align.START)
        list_button.connect("clicked", lambda _b: self.window.show_page("backups"))
        body.append(list_button)
        block.append(panel(body))
        self.scaffold.add(block)

    @staticmethod
    def _clear(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt


def _message_row(icon_name: str, title: str, detail: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_valign(Gtk.Align.START)
    row.append(icon)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
    text.set_hexpand(True)
    heading = Gtk.Label(label=title, xalign=0, wrap=True)
    text.append(heading)
    text.append(caption(detail))
    row.append(text)
    return row
