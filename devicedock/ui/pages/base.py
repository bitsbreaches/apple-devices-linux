# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Device Dock Contributors
"""Base class for the application's pages."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class Page(Gtk.Box):
    """A navigable section. Subclasses set ``title``/``icon_name`` and implement refresh."""

    title = "Page"
    icon_name = "window-symbolic"

    def __init__(self, state, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.window = window
        self.set_hexpand(True)
        self.set_vexpand(True)

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass
