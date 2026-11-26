# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Primary live file browser for a connected iPhone."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..phone_files import PhoneFilesBrowser
from ..widgets import EmptyState, PageScaffold, Space, caption
from .base import Page


class OnMyIPhonePage(Page):
    title = "On My iPhone"
    icon_name = "phone-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(state, window)
        self.device = None
        self.browser: PhoneFilesBrowser | None = None
        self.devices: list = []
        self._changing = False
        self.scaffold = PageScaffold(
            "On My iPhone",
            "Browse accessible files on a connected iPhone. iOS protects files that apps do not share.",
            scroll=False,
            max_width=1500,
        )
        self.append(self.scaffold)
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.device_picker = Gtk.DropDown(hexpand=False)
        self.device_picker.connect("notify::selected", self._device_selected)
        self.toolbar.append(caption("Connected device"))
        self.toolbar.append(self.device_picker)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh connected devices")
        refresh.connect("clicked", lambda *_: state.refresh_devices(refresh_details=True))
        self.toolbar.append(refresh)
        self.toolbar.set_margin_bottom(Space.S)
        self.scaffold.add(self.toolbar)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.content.set_vexpand(True)
        self.scaffold.add(self.content)
        self.state.subscribe("devices", lambda *_: self.refresh())
        self.window.connect("close-request", self._close_browser)
        self.refresh()

    def refresh(self) -> None:
        self.devices = list(self.state.devices)
        model = Gtk.StringList.new([
            f"{device.display_name} · {device.connection_type or 'connected'}"
            for device in self.devices
        ])
        self._changing = True
        self.device_picker.set_model(model)
        selected = next((index for index, device in enumerate(self.devices)
                         if self.device is not None and device.udid == self.device.udid), 0)
        self.device_picker.set_selected(selected if self.devices else Gtk.INVALID_LIST_POSITION)
        self._changing = False
        self._show_selected()

    def _device_selected(self, *_args) -> None:
        if not self._changing:
            self._show_selected()

    def select_device(self, device) -> None:
        index = next((i for i, item in enumerate(self.devices)
                      if item.udid == device.udid), None)
        if index is not None:
            self.device_picker.set_selected(index)

    def _close_browser(self, *_args) -> bool:
        if self.browser is not None:
            return self.browser.close_browser()
        return False

    def _show_selected(self) -> None:
        index = self.device_picker.get_selected()
        device = self.devices[index] if index < len(self.devices) else None
        if device is not None and self.browser is not None and self.device is not None \
                and device.udid == self.device.udid:
            return
        if self.browser is not None:
            self.browser.close_browser()
            self.content.remove(self.browser)
            self.browser = None
        self.device = device
        while child := self.content.get_first_child():
            self.content.remove(child)
        if device is None:
            if self.state.device_error:
                message = self.state.device_error
            else:
                message = "Connect and unlock an iPhone, tap Trust This Computer, then refresh."
            self.content.append(EmptyState("phone-symbolic", "No iPhone available", message))
            return
        self.browser = PhoneFilesBrowser(device)
        self.content.append(self.browser)

